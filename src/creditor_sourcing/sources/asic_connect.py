"""ASIC Connect - detect whether a Form 5604 has been lodged for a company.

Form 5604 is "Information about the company's affairs sent to creditors". It
carries the list of creditors: names, addresses and estimated amounts owed.
That is exactly the creditor list this pipeline exists to source. It is lodged
within 10 business days of the resolution to wind up, and the lodgement fee is
nil - so it is reliably present for creditors' voluntary liquidations.

What is free and what is not
----------------------------
ASIC Connect's organisation search shows the company's document list for free:
document number, form code, received date and availability. Only the document
IMAGE costs money. So this module answers "has a 5604 been lodged, and what is
its document number" at no cost, and the pipeline then queues that document for
purchase. Buying it is the one deliberately manual step in the workflow.

Never automate the purchase. It spends real money per document and the
transaction is not idempotent - a retry buys the document twice.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

from bs4 import BeautifulSoup

from .. import config
from ..models import Matter
from .http import Client

log = logging.getLogger(__name__)

SEARCH_PATH = "/onlineservices/SearchRegisters/SearchOrganisationAndBusinessNames.aspx"
ORG_PATH = "/onlineservices/SearchRegisters/OrganisationDetails.aspx"

# A document-list row that mentions this form code carries a creditor list.
FORM_RE = re.compile(r"\b(5604)\b")
DOC_NUMBER_RE = re.compile(r"\b(\d{9,12})\b")


def organisation_url(acn: str) -> str:
    """Deep link a human can click to buy the document."""
    base = config.settings()["sources"]["asic_connect"]["base_url"]
    return f"{base}{ORG_PATH}?acn={re.sub(r'[^0-9]', '', acn)}"


def probe(acn: str, client: Client | None = None) -> str:
    """Fetch one organisation page and return raw HTML for selector calibration."""
    client = client or Client()
    return client.get(organisation_url(acn)).text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_date(text: str) -> str | None:
    for fmt in ("%d/%m/%Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(_clean(text), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def find_form_5604(html: str) -> dict[str, str] | None:
    """Scan an organisation page's document list for a lodged Form 5604.

    Returns {"doc_number", "date", "form"} or None. Matches on the row rather
    than a fixed column so a layout change degrades to "not found" instead of
    reading the wrong cell and claiming a document exists that does not.
    """
    soup = BeautifulSoup(html, "lxml")
    targets = set(config.settings()["sources"]["asic_connect"]["target_forms"])

    for row in soup.find_all("tr"):
        cells = [_clean(c.get_text()) for c in row.find_all(["td", "th"])]
        if not cells:
            continue
        joined = " ".join(cells)
        match = FORM_RE.search(joined)
        if not match or match.group(1) not in targets:
            continue

        doc_number = None
        lodged = None
        for cell in cells:
            if not doc_number:
                found = DOC_NUMBER_RE.search(cell)
                # The form code itself is 4 digits, so the 9+ digit rule above
                # already excludes it.
                if found:
                    doc_number = found.group(1)
            if not lodged:
                lodged = _parse_date(cell)
        return {"form": match.group(1), "doc_number": doc_number or "", "date": lodged or ""}
    return None


# Only a creditors' voluntary liquidation reliably produces a Form 5604 - it
# is 47.3% of appointments and the form is mandatory within 10 business days
# of the resolution. Court liquidations and administrations sometimes carry
# creditor information on other forms, so they are checked, just later.
LIKELY_5604 = ("creditors' voluntary", "creditors voluntary")


def check_order(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order open matters so a capped run spends its requests where they pay.

    Three tiers, and the last one is what keeps the rotation fair:
      1. Appointment types that actually produce a 5604, newest first - a
         recent CVL is the most likely to have just lodged one.
      2. Everything else, newest first.
      3. Within each tier, matters never checked come before matters checked
         recently, so a capped run works through the backlog instead of
         re-asking about the same 150 companies every week.
    """
    def key(record: dict[str, Any]) -> tuple:
        kind = (record.get("appointment_type") or "").lower()
        likely = any(marker in kind for marker in LIKELY_5604)
        return (
            0 if likely else 1,
            record.get("last_checked") or "",
            # Newest appointment first within the same check age.
            _invert(record.get("appointment_date") or ""),
        )

    return sorted(records, key=key)


def _invert(date_text: str) -> str:
    """Sort ISO dates descending inside an ascending sort key."""
    return "".join(chr(0x10FFFD - ord(ch)) for ch in date_text)


def check(matter: Matter, client: Client | None = None) -> tuple[Matter, bool]:
    """Update one Matter with its Form 5604 status. Safe to call repeatedly.

    Returns (matter, reached) where `reached` says whether ASIC actually
    answered. That distinction is the whole point: 744 CVLs were marked
    checked, and found no 5604, when every single request had 404'd. A matter
    whose lookup failed is not stamped as checked, so it stays at the front of
    the rotation instead of being retired on evidence that was never gathered.
    """
    if not matter.acn:
        log.debug("%s has no ACN - cannot check ASIC Connect", matter.company_name)
        matter.last_checked = date.today().isoformat()
        return matter, True

    client = client or Client()
    try:
        html = client.get(organisation_url(matter.acn)).text
    except Exception as exc:  # noqa: BLE001 - one bad company must not stop the run
        log.warning("ASIC Connect lookup failed for %s: %s", matter.company_name, exc)
        return matter, False

    matter.last_checked = date.today().isoformat()
    found = find_form_5604(html)
    if found:
        matter.form_5604_lodged = True
        matter.form_5604_date = found["date"] or None
        matter.form_5604_doc_number = found["doc_number"] or None
        log.info("5604 found for %s (doc %s)", matter.company_name, found["doc_number"])
    return matter, True
