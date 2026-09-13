"""Decide which creditors are worth putting in front of the sales team.

The rules are the ones agreed with the business:

  * under $5,000 exposure           -> drop (too small to signal a trade book)
  * existing NCI policyholder       -> drop (already a client)
  * not an arm's length trade supplier -> drop (statutory, employee, financier,
    landlord, adviser, insurer, related party)
  * a person rather than a business -> drop (an individual creditor in an
    insolvency is an employee, a director loan or a private lender; there is
    no receivables ledger to insure)
  * already in Pipedrive            -> KEEP, and flag for a note on the
    existing organisation rather than a duplicate prospect

Anything dropped is kept in the workbook on a separate tab with its reason, so
the exclusions can be audited and tuned rather than silently losing leads.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

from rapidfuzz import fuzz, process

from . import config
from .models import Prospect, normalise_name

# Fuzzy-match threshold for PolicyList / Pipedrive name matching. 92 is tight
# enough to avoid matching unrelated companies that share a common word, but
# loose enough to survive "Pty Ltd" and punctuation drift.
FUZZY_THRESHOLD = 92


def _compiled_rules() -> list[tuple[re.Pattern[str], str, str]]:
    """Compile config/exclusions.yml into (pattern, category, reason) triples."""
    rules: list[tuple[re.Pattern[str], str, str]] = []
    for category, block in config.exclusions().items():
        reason = block.get("reason", category)
        for raw in block.get("patterns", []):
            # Bare words get word boundaries so "agl" does not match "eagle".
            pattern = raw if re.search(r"[\\^$]", raw) else rf"\b{re.escape(raw)}\b"
            rules.append((re.compile(pattern, re.IGNORECASE), category, reason))
    return rules


_RULES = None


def non_trade_reason(creditor_name: str) -> tuple[str, str] | None:
    """Return (category, reason) if this name is not a trade supplier."""
    global _RULES
    if _RULES is None:
        _RULES = _compiled_rules()
    for pattern, category, reason in _RULES:
        if pattern.search(creditor_name or ""):
            return category, reason
    return None



# --- Person or business -----------------------------------------------------
#
# Individual creditors were about a quarter of the first qualified list, two of
# them over $400,000. The documents carry no signal that separates them - one
# row in 452 had the "Withheld due to privacy legislation" address - so the
# only evidence is the name. config/individuals.yml holds the vocabulary and
# the reasoning; this is deliberately one-sided, treating a name as a person
# only when it carries no business signal at all.

_ACRONYM = re.compile(r"^[A-Z]{2,}$")
_INITIAL = re.compile(r"^[A-Z]\.?$")
_WORDLIKE = re.compile(r"^[A-Za-z][A-Za-z'\-]*$")
# "Barry Daniels and Laura Daniels C/-" - the care-of marker is address text
# that bled into the name column.
_CARE_OF = re.compile(r"\bc/[-o].*$", re.IGNORECASE)
_JOINED = re.compile(r"\s+and\s+", re.IGNORECASE)

_PEOPLE_CFG: dict[str, Any] | None = None


def _people_config() -> dict[str, Any]:
    global _PEOPLE_CFG
    if _PEOPLE_CFG is None:
        raw = config.individuals()
        _PEOPLE_CFG = {
            "titles": {t.lower() for t in raw.get("titles", [])},
            "business_words": {w.lower() for w in raw.get("business_words", [])},
            "keep": {normalise_name(k) for k in raw.get("keep", [])},
        }
    return _PEOPLE_CFG


def looks_like_a_person(name: str, _depth: int = 0) -> bool:
    """Is this creditor a natural person rather than a business?"""
    cfg = _people_config()
    if normalise_name(name) in cfg["keep"]:
        return False

    text = _CARE_OF.sub("", name or "").strip(" .,-")
    if not text or any(ch.isdigit() for ch in text) or "&" in text:
        return False

    tokens = text.split()
    if tokens and tokens[0].lower().strip(".") in cfg["titles"]:
        return True

    # Two people on one row: "Barry Daniels and Laura Daniels". Only one level
    # deep, so a business name containing "and" cannot recurse indefinitely.
    if _depth == 0:
        parts = [p for p in _JOINED.split(text) if p.strip()]
        if len(parts) > 1:
            return all(looks_like_a_person(part, 1) for part in parts)

    if not 2 <= len(tokens) <= 4:
        return False
    for token in tokens:
        bare = token.strip(".,")
        if _INITIAL.match(bare):
            continue
        if not _WORDLIKE.match(bare) or _ACRONYM.match(bare):
            return False
        if not bare[:1].isupper() or bare.lower() in cfg["business_words"]:
            return False
    return True


def policylist_match(name: str, policy_keys: dict[str, str]) -> str | None:
    """Fuzzy-match a prospect against existing NCI policyholders.

    `policy_keys` maps normalised name -> the original policy company name.
    """
    if not policy_keys:
        return None
    key = normalise_name(name)
    if not key:
        return None
    if key in policy_keys:
        return policy_keys[key]
    hit = process.extractOne(
        key, policy_keys.keys(), scorer=fuzz.token_sort_ratio,
        score_cutoff=FUZZY_THRESHOLD,
    )
    if hit:
        return policy_keys[hit[0]]

    # A creditor listing names the client the way its accounts clerk does:
    # "Studworks" for STUDWORKS PROFILE SYSTEMS PTY LTD. The fuzzy score for
    # a short name against a long one is low, so a distinctive name that
    # opens a policyholder's name is also a match. "Distinctive" is the
    # guard: a single generic word ("Pharmacy") is never enough.
    if _distinctive(key):
        prefix = key + " "
        starts = [k for k in policy_keys if k.startswith(prefix)]
        if len(starts) == 1:
            return policy_keys[starts[0]]
    return None


# Minimum length for a one-word name to count as distinctive in the prefix
# rule above. "pharmacy", "building", "plumbing" all fall under it.
PREFIX_MIN_CHARS = 9


def _distinctive(key: str) -> bool:
    tokens = key.split()
    if len(tokens) >= 2:
        return len(key) >= PREFIX_MIN_CHARS
    return len(key) >= PREFIX_MIN_CHARS and not key.isdigit()


def score(prospect: Prospect) -> int:
    """Priority score, 0-100.

    Two signals drive it, and the repeat signal is deliberately the stronger
    one: a supplier that turns up as a creditor in several administrations is
    losing money right now across its ledger, which is the clearest buying
    trigger this pipeline can observe. A single large debt is good; three
    debts is a conversation.
    """
    cfg = config.settings()["score"]
    repeat = (prospect.matter_count - 1) * cfg["repeat_matter_weight"]
    # log10 so a $2m exposure outranks $200k without swamping the repeat
    # signal. An unquantified exposure scores on the repeat signal alone
    # rather than being ranked as if it were zero.
    exposure = 0.0
    if prospect.exposure_known and prospect.total_exposure_aud > 0:
        exposure = math.log10(prospect.total_exposure_aud) * cfg["exposure_log_weight"]
    return int(min(cfg["max_score"], max(0, repeat + exposure)))


def apply(
    prospects: Iterable[Prospect],
    policy_keys: dict[str, str] | None = None,
) -> list[Prospect]:
    """Qualify every prospect in place and return them scored."""
    cfg = config.settings()["qualify"]
    policy_keys = policy_keys or {}
    out: list[Prospect] = []

    for prospect in prospects:
        prospect.score = score(prospect)

        # The floor can only be applied to a stated amount. Practitioners
        # routinely publish the creditor list with the ROCAP Amount column as
        # TBC, so testing an unstated exposure against the floor would drop
        # every creditor from those matters - which is most of the early
        # Worrells intake. Keep them and let the rep see the exposure is
        # not yet quantified.
        below_floor = (
            prospect.exposure_known
            and prospect.total_exposure_aud < cfg["min_exposure_aud"]
        )
        if below_floor:
            prospect.qualified = False
            prospect.disqualified_reason = (
                f"Exposure ${prospect.total_exposure_aud:,.0f} is under the "
                f"${cfg['min_exposure_aud']:,.0f} minimum"
            )
            out.append(prospect)
            continue

        if cfg["drop_non_trade"]:
            hit = non_trade_reason(prospect.display_name)
            if hit:
                prospect.qualified = False
                prospect.disqualified_reason = hit[1]
                out.append(prospect)
                continue

        if cfg.get("drop_individuals", True) and looks_like_a_person(
            prospect.display_name
        ):
            prospect.qualified = False
            prospect.disqualified_reason = (
                "Individual, not a business - no receivables ledger to insure"
            )
            out.append(prospect)
            continue

        if cfg["drop_policylist_clients"]:
            match = policylist_match(prospect.display_name, policy_keys)
            if match:
                prospect.policylist_match = match
                prospect.qualified = False
                prospect.disqualified_reason = f"Existing NCI client ({match})"
                out.append(prospect)
                continue

        out.append(prospect)

    return sorted(out, key=lambda p: (not p.qualified, -p.score, -p.total_exposure_aud))
