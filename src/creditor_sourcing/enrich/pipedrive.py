"""Pipedrive lookup.

A prospect already in Pipedrive is NOT dropped. It is kept and flagged so the
run can add a note to the existing organisation - "this company is a creditor
in <insolvency>, owed $X" - which is useful to whoever already owns the
relationship. Creating a duplicate organisation would be worse than useless.

Two things this module has to get right, both learned against the live CRM:

* **A search hit is not a match.** Pipedrive's organisation search is a
  relevance search: "Melbourne Plaster" returns Creative Plastering Group,
  M & C Plaster Supplies and Plaster Now first. Taking the top hit would have
  written a creditor note onto the wrong company. Every candidate is put
  through the same "is this the same company?" test the PolicyList uses
  (`qualify.match_name`), plus an ABN/ACN hit on the organisation's custom
  fields, and only a candidate that passes is recorded.
* **Writing notes is a deliberate, separate, idempotent step.** `annotate`
  only reads; `push_notes` writes, is gated behind --push-notes, and skips an
  organisation that already carries this pipeline's note for the same
  insolvencies, so a weekly run does not post the same note every Monday.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

import requests

from .. import config, qualify
from ..models import Prospect, normalise_name

log = logging.getLogger(__name__)

API = "https://api.pipedrive.com/v1"
NOTE_MARKER = "Sourced automatically by the creditor-list-sourcing pipeline."
SEARCH_LIMIT = 10

_DIGITS = re.compile(r"\D+")


def _token() -> str | None:
    return config.secret("PIPEDRIVE_API_TOKEN")


def _get(path: str, token: str, **params: Any) -> dict[str, Any] | None:
    try:
        response = requests.get(
            f"{API}{path}", params={**params, "api_token": token}, timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Pipedrive GET %s failed: %s", path, exc)
        return None
    return response.json()


def search_organisations(name: str, token: str) -> list[dict[str, Any]]:
    """Every candidate organisation Pipedrive's search offers for `name`."""
    payload = _get(
        "/organizations/search", token,
        term=name, fields="name,custom_fields", limit=SEARCH_LIMIT,
    )
    if not payload:
        return []
    items = (payload.get("data") or {}).get("items") or []
    return [item["item"] for item in items if item.get("item")]


def _identifier_digits(value: Any) -> str:
    digits = _DIGITS.sub("", str(value or ""))
    return digits if len(digits) in (9, 11) else ""


def match_organisation(
    prospect: Prospect, candidates: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """The candidate that IS `prospect`, or None.

    An ABN or ACN on the organisation's custom fields is decisive when the
    prospect carries one. Otherwise the organisation's name, and any
    name-shaped custom field (Pipedrive holds trading names there), must pass
    `qualify.match_name`.
    """
    candidates = list(candidates)
    if not candidates:
        return None

    # An ABN is the ACN with a two-digit prefix, so an organisation that holds
    # only the ACN still identifies the same company.
    want = _identifier_digits(prospect.abn)
    wanted = {want, want[2:]} if len(want) == 11 else {want} if want else set()
    if wanted:
        for org in candidates:
            fields = org.get("custom_fields") or []
            if any(_identifier_digits(f) in wanted for f in fields if _identifier_digits(f)):
                return org

    by_key: dict[str, dict[str, Any]] = {}
    for org in candidates:
        names = [org.get("name") or ""]
        for field in org.get("custom_fields") or []:
            text = str(field or "")
            if "@" in text or _identifier_digits(text) or text.startswith(("http", "www")):
                continue
            names.append(text)
        for candidate in names:
            key = normalise_name(candidate)
            if key:
                by_key.setdefault(key, org)

    hit = qualify.match_name(prospect.display_name, by_key.keys())
    return by_key[hit] if hit else None


class _Users:
    """Owner ids -> names, fetched once per run."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._names: dict[int, str] | None = None

    def name(self, owner_id: Any) -> str | None:
        if owner_id is None:
            return None
        if self._names is None:
            payload = _get("/users", self._token) or {}
            self._names = {
                u["id"]: u.get("name") for u in payload.get("data") or [] if "id" in u
            }
        return self._names.get(owner_id) or str(owner_id)


def annotate(prospects: Iterable[Prospect], domain: str = "nci") -> int:
    """Tag each prospect with its Pipedrive organisation, if it has one.

    Read-only. Logs every match at INFO so a run without --push-notes shows
    exactly which organisations would receive a note.
    """
    token = _token()
    if not token:
        log.warning("PIPEDRIVE_API_TOKEN not set - skipping CRM cross-reference")
        return 0

    users = _Users(token)
    hits = rejected = 0
    for prospect in prospects:
        candidates = search_organisations(prospect.display_name, token)
        org = match_organisation(prospect, candidates)
        if not org:
            if candidates:
                rejected += 1
                log.debug(
                    "Pipedrive: %r is not %s", prospect.display_name,
                    ", ".join(repr(c.get("name")) for c in candidates[:3]),
                )
            continue
        owner = org.get("owner") or {}
        prospect.pipedrive_org_id = org.get("id")
        prospect.pipedrive_org_name = org.get("name")
        prospect.pipedrive_owner = owner.get("name") or users.name(owner.get("id"))
        prospect.pipedrive_url = f"https://{domain}.pipedrive.com/organization/{org.get('id')}"
        hits += 1
        log.info(
            "Pipedrive: %r is organisation %s (%s), owner %s - note would say: %s",
            prospect.display_name, org.get("id"), org.get("name"),
            prospect.pipedrive_owner, note_body(prospect).splitlines()[1],
        )
    log.info(
        "Pipedrive: %d prospects already in the CRM; %d search hits rejected as a "
        "different company", hits, rejected,
    )
    return hits


def note_body(prospect: Prospect) -> str:
    """The note added to an existing Pipedrive organisation."""
    lines = [
        "Creditor exposure identified from external administration filings.",
        f"Total owed across {prospect.matter_count} "
        f"insolvenc{'y' if prospect.matter_count == 1 else 'ies'}: "
        + (f"${prospect.total_exposure_aud:,.2f}"
           if prospect.exposure_known else "not yet quantified (TBC)"),
        "",
    ]
    for matter in prospect.matters:
        amount = matter.get("amount_aud") or 0.0
        shown = f"${amount:,.2f}" if matter.get("amount_known", True) and amount else "TBC"
        lines.append(
            f"  - {matter['debtor_company']}: {shown} (source: {matter['source']})"
        )
    lines += ["", NOTE_MARKER]
    return "\n".join(lines)


def _already_noted(org_id: int, body: str, token: str) -> bool:
    """Does this organisation already carry our note for these insolvencies?"""
    payload = _get("/notes", token, org_id=org_id, limit=100) or {}
    debtors = {
        line.strip()[2:].split(":")[0] for line in body.splitlines()
        if line.strip().startswith("- ")
    }
    for note in payload.get("data") or []:
        content = str(note.get("content") or "")
        if NOTE_MARKER in content and all(d in content for d in debtors):
            return True
    return False


def push_notes(prospects: Iterable[Prospect]) -> int:
    """Write notes onto existing organisations. Only call deliberately."""
    token = _token()
    if not token:
        raise RuntimeError("PIPEDRIVE_API_TOKEN is required to push notes")

    written = skipped = 0
    for prospect in prospects:
        if not prospect.pipedrive_org_id:
            continue
        body = note_body(prospect)
        if _already_noted(prospect.pipedrive_org_id, body, token):
            skipped += 1
            continue
        try:
            response = requests.post(
                f"{API}/notes",
                params={"api_token": token},
                json={"content": body, "org_id": prospect.pipedrive_org_id},
                timeout=30,
            )
            response.raise_for_status()
            written += 1
        except requests.RequestException as exc:
            log.warning("Pipedrive note failed for %s: %s", prospect.display_name, exc)
    log.info("Pipedrive: %d notes written, %d already present", written, skipped)
    return written
