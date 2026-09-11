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


def check(matter: Matter, client: Client | None = None) -> Matter:
    """Update one Matter with its Form 5604 status. Safe to call repeatedly."""
    matter.last_checked = date.today().isoformat()
    if not matter.acn:
        log.debug("%s has no ACN - cannot check ASIC Connect", matter.company_name)
        return matter

    client = client or Client()
    try:
        html = client.get(organisation_url(matter.acn)).text
    except Exception as exc:  # noqa: BLE001 - one bad company must not stop the run
        log.warning("ASIC Connect lookup failed for %s: %s", matter.company_name, exc)
        return matter

    found = find_form_5604(html)
    if found:
        matter.form_5604_lodged = True
        matter.form_5604_date = found["date"] or None
        matter.form_5604_doc_number = found["doc_number"] or None
        log.info("5604 found for %s (doc %s)", matter.company_name, found["doc_number"])
    return matter
