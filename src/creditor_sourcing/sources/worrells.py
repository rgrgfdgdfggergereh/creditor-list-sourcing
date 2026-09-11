"""Worrells customer portal - creditor lists without paying ASIC.

Worrells publishes its Initial Advice reports to creditors on a public portal.
Those reports contain the "Listing of known creditors" table, which is the same
data a Form 5604 carries - but free and available the week of appointment
rather than after a purchase.

The URL tweak that makes this work
----------------------------------
The New Appointments list links each matter as

    /FileInformation/FileInformation/<32-hex-id>

which is not the page that carries the client documents. Swapping the path
segment to

    /FileInformation/FileInformationDetailsView/<32-hex-id>

- same id, different path - renders the page that lists the downloadable PDFs.
Both forms are handled by `details_view_url()`.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import config
from ..models import Matter
from .http import Client

log = logging.getLogger(__name__)

FILE_ID_RE = re.compile(r"([0-9A-Fa-f]{32})")
PDF_RE = re.compile(r"/WebDocuments/.+\.pdf$", re.IGNORECASE)

# Documents that actually carry a creditor listing, best first.
CREDITOR_DOCUMENTS = ("notice of plan", "initial advice", "first advice", "2nd advice")

# Label -> Matter field, as the labels appear in the File Details panel. The
# panel gives us the ACN, which is what lets a Worrells matter be reconciled
# against the same company in the ASIC workbook.
DETAIL_LABELS = {
    "ACN/Estate#": "acn",
    "Admin Type": "appointment_type",
    "Start Date": "appointment_date",
    "Industry": "industry",
    "Appointee": "practitioner",
    "Office Name": "office",
    "Principal": "principal",
}

# EVERY label the File Details panel renders, mapped or not. These are the
# boundaries a value must stop at. Omitting the unmapped ones lets a value
# swallow the rest of the panel - "Start Date" came back as
# "11/09/2026 Exec Analyst Broderick Dipple Status Priority Contact Person ...".
PANEL_LABELS = (
    "ACN/Estate#", "Office Name", "Trading Name", "Principal", "Admin Type",
    "Manager:", "Manager", "Start Date", "Exec Analyst", "Status",
    "Contact Person", "Industry", "Appointee", "File Details", "I want to",
)


def details_view_url(url_or_id: str) -> str:
    """Normalise any Worrells file reference to the DetailsView URL.

    Accepts a full list URL, a full DetailsView URL, or a bare 32-hex file id.
    """
    cfg = config.settings()["sources"]["worrells"]
    found = FILE_ID_RE.search(url_or_id or "")
    if not found:
        raise ValueError(f"No 32-character Worrells file id in {url_or_id!r}")
    return f"{cfg['base_url']}{cfg['detail_path_to']}{found.group(1)}"


def parse_new_appointments(html: str) -> list[Matter]:
    """Pull (company, file id) pairs off the New Appointments list."""
    soup = BeautifulSoup(html, "lxml")
    matters: dict[str, Matter] = {}

    for link in soup.find_all("a", href=True):
        found = FILE_ID_RE.search(link["href"])
        if not found or "fileinformation" not in link["href"].lower():
            continue
        file_id = found.group(1)
        name = re.sub(r"\s+", " ", link.get_text()).strip()
        if not name or file_id in matters:
            continue
        matters[file_id] = Matter(
            source="worrells",
            company_name=name,
            source_id=file_id,
            source_url=details_view_url(file_id),
            first_seen=date.today().isoformat(),
        )
    return list(matters.values())


def parse_documents(html: str, base_url: str) -> list[dict[str, str]]:
    """List the client-document PDFs on a DetailsView page, creditor-lists first."""
    soup = BeautifulSoup(html, "lxml")
    documents: list[dict[str, str]] = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not PDF_RE.search(href.split("?")[0]):
            continue
        documents.append(
            {
                "name": re.sub(r"\s+", " ", link.get_text()).strip(),
                "url": urljoin(base_url, href),
            }
        )

    def rank(doc: dict[str, str]) -> int:
        name = doc["name"].lower()
        for i, wanted in enumerate(CREDITOR_DOCUMENTS):
            if wanted in name:
                return i
        return len(CREDITOR_DOCUMENTS)

    return sorted(documents, key=rank)


def creditor_documents(html: str, base_url: str) -> list[dict[str, str]]:
    """Only the documents worth downloading and parsing."""
    return [
        doc
        for doc in parse_documents(html, base_url)
        if any(w in doc["name"].lower() for w in CREDITOR_DOCUMENTS)
    ]


def parse_details(html: str) -> dict[str, str]:
    """Read the File Details panel off a DetailsView page.

    The panel is label/value pairs rendered as adjacent inline elements rather
    than a table, so this walks the page's visible text and reads the span
    after each known label. Labels not present simply do not appear in the
    result - a brand new matter has most of them blank.
    """
    soup = BeautifulSoup(html, "lxml")
    text = " ".join(soup.get_text(" ", strip=True).split())

    # A value runs until the next panel label, whether or not we map it.
    stop = "|".join(
        re.escape(label) for label in sorted(PANEL_LABELS, key=len, reverse=True)
    )

    details: dict[str, str] = {}
    for label in sorted(DETAIL_LABELS, key=len, reverse=True):
        field = DETAIL_LABELS[label]
        if field in details:
            continue
        match = re.search(
            rf"{re.escape(label)}\s*:?\s*(.+?)\s*(?={stop}|$)", text
        )
        if not match:
            continue
        value = match.group(1).strip(" :-")
        if value and value.lower() not in ("none known", "none", "n/a"):
            details[field] = value
    return details


def apply_details(matter: Matter, html: str) -> Matter:
    """Fill a Matter from its DetailsView page. Never overwrites a set field."""
    details = parse_details(html)

    acn = re.sub(r"[^0-9]", "", details.get("acn", ""))
    if acn and len(acn) <= 9 and not matter.acn:
        matter.acn = acn.zfill(9)
    if not matter.appointment_type:
        matter.appointment_type = details.get("appointment_type")
    if not matter.industry:
        matter.industry = details.get("industry")
    if not matter.practitioner:
        matter.practitioner = details.get("practitioner")
    if not matter.practitioner_firm:
        matter.practitioner_firm = "Worrells"

    start = details.get("appointment_date")
    if start and not matter.appointment_date:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d %b %Y"):
            try:
                matter.appointment_date = datetime.strptime(start, fmt).date().isoformat()
                break
            except ValueError:
                continue
    return matter


def collect(client: Client | None = None) -> list[Matter]:
    cfg = config.settings()["sources"]["worrells"]
    client = client or Client(throttle_ms=cfg["throttle_ms"])
    html = client.get(cfg["base_url"] + cfg["list_path"]).text
    matters = parse_new_appointments(html)
    log.info("Worrells: %d appointments on the New Appointments list", len(matters))
    return matters
