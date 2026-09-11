"""Pipedrive lookup.

A prospect already in Pipedrive is NOT dropped. It is kept and flagged so the
run can add a note to the existing organisation - "this company is a creditor
in <insolvency>, owed $X" - which is useful to whoever already owns the
relationship. Creating a duplicate organisation would be worse than useless.

Writing notes is a deliberate, separate step: `notes` builds them, and only
`push_notes` sends anything. The weekly job builds them every run; pushing is
gated behind --push-notes so a dry run never writes to the CRM.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import requests

from .. import config
from ..models import Prospect

log = logging.getLogger(__name__)

API = "https://api.pipedrive.com/v1"


def _token() -> str | None:
    return config.secret("PIPEDRIVE_API_TOKEN")


def search_organisation(name: str, token: str) -> dict[str, Any] | None:
    try:
        response = requests.get(
            f"{API}/organizations/search",
            params={"term": name, "fields": "name", "limit": 5, "api_token": token},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Pipedrive search failed for %s: %s", name, exc)
        return None

    items = (response.json().get("data") or {}).get("items") or []
    return items[0]["item"] if items else None


def annotate(prospects: Iterable[Prospect], domain: str = "nci") -> int:
    """Tag each prospect with its Pipedrive organisation, if it has one."""
    token = _token()
    if not token:
        log.warning("PIPEDRIVE_API_TOKEN not set - skipping CRM cross-reference")
        return 0

    hits = 0
    for prospect in prospects:
        org = search_organisation(prospect.display_name, token)
        if not org:
            continue
        prospect.pipedrive_org_id = org.get("id")
        prospect.pipedrive_org_name = org.get("name")
        owner = org.get("owner") or {}
        prospect.pipedrive_owner = owner.get("name")
        prospect.pipedrive_url = f"https://{domain}.pipedrive.com/organization/{org.get('id')}"
        hits += 1
    log.info("Pipedrive: %d prospects already in the CRM", hits)
    return hits


def note_body(prospect: Prospect) -> str:
    """The note added to an existing Pipedrive organisation."""
    lines = [
        "Creditor exposure identified from external administration filings.",
        f"Total owed across {prospect.matter_count} "
        f"insolvenc{'y' if prospect.matter_count == 1 else 'ies'}: "
        f"${prospect.total_exposure_aud:,.2f}",
        "",
    ]
    for matter in prospect.matters:
        lines.append(
            f"  - {matter['debtor_company']}: ${matter['amount_aud']:,.2f} "
            f"(source: {matter['source']})"
        )
    lines += ["", "Sourced automatically by the creditor-list-sourcing pipeline."]
    return "\n".join(lines)


def push_notes(prospects: Iterable[Prospect]) -> int:
    """Write notes onto existing organisations. Only call deliberately."""
    token = _token()
    if not token:
        raise RuntimeError("PIPEDRIVE_API_TOKEN is required to push notes")

    written = 0
    for prospect in prospects:
        if not prospect.pipedrive_org_id:
            continue
        try:
            response = requests.post(
                f"{API}/notes",
                params={"api_token": token},
                json={"content": note_body(prospect), "org_id": prospect.pipedrive_org_id},
                timeout=30,
            )
            response.raise_for_status()
            written += 1
        except requests.RequestException as exc:
            log.warning("Pipedrive note failed for %s: %s", prospect.display_name, exc)
    log.info("Pipedrive: %d notes written", written)
    return written
