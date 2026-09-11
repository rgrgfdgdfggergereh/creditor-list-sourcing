"""Decide which creditors are worth putting in front of the sales team.

The rules are the ones agreed with the business:

  * under $5,000 exposure           -> drop (too small to signal a trade book)
  * existing NCI policyholder       -> drop (already a client)
  * not an arm's length trade supplier -> drop (statutory, employee, financier,
    landlord, adviser, insurer, related party)
  * already in Pipedrive            -> KEEP, and flag for a note on the
    existing organisation rather than a duplicate prospect

Anything dropped is kept in the workbook on a separate tab with its reason, so
the exclusions can be audited and tuned rather than silently losing leads.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

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


def policylist_match(name: str, policy_keys: dict[str, str]) -> str | None:
    """Fuzzy-match a prospect against existing NCI policyholders.

    `policy_keys` maps normalised name -> the original policy company name.
    """
    if not policy_keys:
        return None
    key = normalise_name(name)
    if key in policy_keys:
        return policy_keys[key]
    hit = process.extractOne(
        key, policy_keys.keys(), scorer=fuzz.token_sort_ratio,
        score_cutoff=FUZZY_THRESHOLD,
    )
    return policy_keys[hit[0]] if hit else None


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
