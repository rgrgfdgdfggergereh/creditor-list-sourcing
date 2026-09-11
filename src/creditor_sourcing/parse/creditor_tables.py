"""Pull creditor rows out of a Form 5604 or an Initial Advice PDF.

Both documents present the same thing in slightly different dress: a table of
creditor name, address, and amount owed. A Form 5604 calls it the list of
creditors; a Worrells Initial Advice calls it "Listing of known creditors";
a Small Business Restructure plan calls it "Schedule of debts and claims".

Parsing rules that matter
-------------------------
* A page is only a creditor table if it has real rows - a name AND a dollar
  amount. Both the table of contents and the narrative sentence "list of
  creditors and summary of affairs" contain the heading words, and treating
  either as a table invents creditors that do not exist.
* Dotted leaders ("......") mark a contents entry. Never a data row.
* Scanned pages produce no text. Report them for manual review rather than
  running OCR and publishing names the document may not contain. Publishing a
  wrong creditor name and amount is worse than publishing nothing.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..models import Creditor

log = logging.getLogger(__name__)

HEADINGS = (
    "listing of known creditors",
    "list of creditors",
    "schedule of debts and claims",
    "unsecured creditors",
)
LEADERS = re.compile(r"\.{4,}")
AMOUNT_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})\s*$")
RELATED_RE = re.compile(r"\b(yes|no)\b", re.IGNORECASE)
# A creditor name has letters and is not a lone number, heading or page marker.
NAME_RE = re.compile(r"[A-Za-z]{3,}")
SKIP_LINE = re.compile(
    r"^(page \d+|total|sub-?total|continued|annexure|schedule|name\b.*amount)",
    re.IGNORECASE,
)


def _amount(text: str) -> float | None:
    found = AMOUNT_RE.search(text.strip())
    if not found:
        return None
    try:
        return float(found.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_lines(
    lines: list[str], debtor_company: str, matter_id: str, source: str,
    source_document: str | None = None,
) -> list[Creditor]:
    """Read creditor rows from the text lines of a creditor-listing section."""
    creditors: list[Creditor] = []
    for line in lines:
        line = re.sub(r"\s+", " ", line).strip()
        if not line or LEADERS.search(line) or SKIP_LINE.match(line):
            continue
        amount = _amount(line)
        if amount is None:
            continue
        head = AMOUNT_RE.sub("", line).strip(" .|-")
        if not NAME_RE.search(head):
            continue

        related = False
        tail = RELATED_RE.search(head)
        if tail and tail.group(1).lower() == "yes":
            related = True
        # Strip the trailing Yes/No flag column off the name.
        head = RELATED_RE.sub("", head).strip(" .|-,")

        # Split a trailing address off the name where the document runs them
        # together: the address starts at the first street-number-ish token.
        name, address = head, None
        split = re.search(r"\s(?=\d+[/\-]?\d*\s+[A-Z])", head)
        if split and split.start() > 3:
            name, address = head[: split.start()].strip(), head[split.start():].strip()

        if len(name) < 3:
            continue
        creditors.append(
            Creditor(
                creditor_name=name,
                debtor_company=debtor_company,
                matter_id=matter_id,
                amount_aud=amount,
                address=address,
                related_party=related,
                source=source,
                source_document=source_document,
            )
        )
    return creditors


def extract_pdf(
    path: Path, debtor_company: str, matter_id: str, source: str = "asic",
) -> tuple[list[Creditor], str]:
    """Extract creditors from a PDF.

    Returns (creditors, status) where status is one of:
      ok        - a creditor table was found and read
      scanned   - the document has no extractable text; needs manual review
      no-section- text extracted, but no creditor listing in it
    """
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - older wheels only expose `fitz`
        try:
            import fitz as pymupdf
        except ImportError as exc:
            raise RuntimeError("pymupdf is required to parse creditor PDFs") from exc

    if path.stat().st_size == 0:
        # A zero-byte PDF means the portal returned 404 for that document.
        return [], "missing"

    with pymupdf.open(path) as doc:
        pages = [page.get_text() for page in doc]

    if not any(p.strip() for p in pages):
        return [], "scanned"

    creditors: list[Creditor] = []
    in_section = False
    for page_text in pages:
        lowered = page_text.lower()
        if any(h in lowered for h in HEADINGS):
            in_section = True
        if not in_section:
            continue
        found = parse_lines(
            page_text.splitlines(), debtor_company, matter_id, source, path.name
        )
        if found:
            creditors.extend(found)
        elif creditors:
            # Section has ended - the table stopped producing rows.
            in_section = False

    if not creditors:
        return [], "no-section"
    log.info("%s: %d creditors", path.name, len(creditors))
    return creditors, "ok"
