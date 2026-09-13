"""Committed run state.

State lives in the repo as JSON, not in a database, because the weekly run is
a GitHub Actions job with no persistent disk and every state change should be
visible in a diff. Three files:

  state/matters.json        every administration we have seen, and its 5604 status
  state/purchase_queue.json documents waiting on the manual ASIC purchase step
  state/prospects.json      the running creditor -> prospect roll-up

A matter is only recorded as DONE when its creditor list has actually been
captured. A matter with no 5604 lodged yet is deliberately left open so the
next run re-checks it - the form is often lodged weeks after the appointment,
and dropping it would lose the lead permanently.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config
from .models import Matter

STATE_DIR = config.STATE_DIR
MATTERS = STATE_DIR / "matters.json"
QUEUE = STATE_DIR / "purchase_queue.json"
PROSPECTS = STATE_DIR / "prospects.json"


def _read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def load_matters() -> dict[str, dict[str, Any]]:
    return _read(MATTERS, {})


def save_matters(matters: dict[str, dict[str, Any]]) -> None:
    _write(MATTERS, matters)


def merge(known: dict[str, dict[str, Any]], found: list[Matter]) -> tuple[dict, int]:
    """Fold newly seen matters into state. Returns (state, new_count)."""
    new = 0
    for matter in found:
        key = matter.matter_id
        if key in known:
            # Never clobber captured status with a fresh empty scrape.
            existing = known[key]
            for field, value in matter.to_dict().items():
                if value and not existing.get(field):
                    existing[field] = value
            existing["last_seen"] = date.today().isoformat()
        else:
            record = matter.to_dict()
            record["last_seen"] = date.today().isoformat()
            known[key] = record
            new += 1
    return known, new


def open_matters(known: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Matters still worth re-checking on ASIC Connect.

    Open = creditors not yet captured, and first seen inside the watch window.
    Past the window a company almost certainly is not going to lodge, and
    re-checking it forever is wasted requests against ASIC.
    """
    watch_days = config.settings()["sources"]["asic_connect"]["watch_days"]
    cutoff = (date.today() - timedelta(days=watch_days)).isoformat()
    return [
        m
        for m in known.values()
        if not m.get("creditors_captured")
        and (m.get("first_seen") or cutoff) >= cutoff
        # A Worrells matter whose documents exist but carry no listing will
        # not grow one; re-fetching it every week is pure portal load. One
        # with nothing lodged yet is the opposite - that is most of the
        # intake, and it is exactly what we are waiting on.
        and m.get("document_status") not in ("no-section", "scanned")
    ]


def load_queue() -> list[dict[str, Any]]:
    return _read(QUEUE, [])


def save_queue(rows: list[dict[str, Any]]) -> None:
    _write(QUEUE, rows)


def queue_for_purchase(matters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the buy list: 5604 lodged, not yet purchased, creditors not captured."""
    from .enrich.iris import debtor_search_url
    from .sources.abn_lookup import format_abn
    from .sources.asic_connect import organisation_url

    rows = []
    for m in matters:
        if not m.get("form_5604_lodged") or m.get("form_5604_purchased"):
            continue
        if m.get("creditors_captured"):
            continue
        rows.append(
            {
                "matter_id": m["matter_id"],
                "company_name": m["company_name"],
                "acn": m.get("acn"),
                "document_number": m.get("form_5604_doc_number"),
                "lodged_date": m.get("form_5604_date"),
                "appointment_type": m.get("appointment_type"),
                "asic_connect_url": organisation_url(m["acn"]) if m.get("acn") else None,
                # For the IRIS check before spending money on the document:
                # the ABN to paste, and the screen to paste it into. IRIS has
                # no per-debtor URL - see enrich/iris.py for why.
                "abn": format_abn(m["abn"]) if m.get("abn") else None,
                "iris_search_url": debtor_search_url(),
                "queued_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    return rows


def save_prospects(rows: list[dict[str, Any]]) -> None:
    _write(PROSPECTS, rows)


def load_prospects() -> list[dict[str, Any]]:
    return _read(PROSPECTS, [])
