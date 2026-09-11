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
)

# A page only counts as a creditor table if it carries the Related Party
# column - standalone Yes/No cells - AND amount-only cells. This is the gate
# that matters. Measured on live reports: narrative pages have zero standalone
# Yes/No cells, while carrying sentences like "in the amount of $31,500.00"
# that a heading-plus-amount rule happily harvests. Without this gate the
# parser reported five creditors named "Fees:" off a remuneration schedule.
MIN_YES_NO_CELLS = 2
MIN_MONEY_CELLS = 2

# An early Initial Advice publishes the creditor names before the amounts are
# quantified: the "ROCAP Amount" column reads TBC. Measured live on RFCVIC PTY
# LTD page 18, where all four creditors had TBC. Requiring amount cells on the
# page therefore rejects real listings, so a listing heading plus the Related
# Party column is sufficient on its own.
UNQUANTIFIED = re.compile(
    r"^\s*(tbc|t\.b\.c\.?|unknown|unquantified|n/?a|nil|-|\u2013|\u2014)\s*$",
    re.IGNORECASE,
)

YES_NO_CELL = re.compile(r"^\s*(Yes|No)\s*$", re.IGNORECASE)
MONEY_CELL = re.compile(r"^\s*\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?\s*$")

# The two layouts a creditor listing comes in.
#
# CELL_PER_LINE is what PyMuPDF emits for a real ruled table: every cell on its
# own line, so the Related Party column appears as bare "Yes"/"No" lines.
# INLINE_ROW is a whole row on one line - name, address, the related-party
# flag, then the amount - which is how some reports and the Form 5604 layout
# come through.
#
# A page qualifies as a table if it shows either shape. Requiring only a
# heading and a trailing amount, as this once did, matches narrative prose.
INLINE_ROW = re.compile(
    r"^\s*\S.*?\b(Yes|No)\b[\s.|-]*\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?\s*$",
    re.IGNORECASE,
)
MIN_INLINE_ROWS = 2

# Labels that are never a creditor, however well-formed the row looks. These
# come from remuneration and position tables that sit near creditor content.
NOT_A_CREDITOR = re.compile(
    r"^(fees?|gst|total|sub-?total|balance|amount|remuneration|disbursements?|"
    r"petty cash|interest|adjustment|opening|closing|less\b|add\b|net\b|"
    r"estimated|surplus|deficiency|dividend|distribution|realisations?|"
    r"receipts?|payments?|asset|liabilit)",
    re.IGNORECASE,
)

# Prose fragments are the other false-positive family: a narrative line whose
# clause happens to end in an amount, e.g. "report to creditors of 10 September
# 2026 in the amount of $31,500.00". Two signals separate prose from a name.
# A registered company name starts with a capital or a digit, and never ends on
# a preposition or conjunction.
PROSE_TAIL = re.compile(
    r"\b(of|to|in|on|at|by|for|from|with|and|or|the|a|an|is|are|was|were|"
    r"that|this|as|per|under|dated)$",
    re.IGNORECASE,
)

# Section and annexure labels that sit above the first row.
ANNEXURE = re.compile(r"^(annexure|schedule|section|appendix|part)\b", re.IGNORECASE)

# Page furniture that lands in the row buffer ahead of the first creditor: the
# page number and the section letter. Left in, they become the first row's
# "name" and the real first creditor is lost - which is exactly what happened
# to Australian Alliance Automotive Finance Pty Limited on RFCVIC page 18.
PAGE_FURNITURE = re.compile(r"^(\d{1,4}|[A-Z]\.?|[ivxlc]+\.?|Page \d+.*)$")

# Column headers, including the ROCAP and Identified variants practitioners
# use for the amount column.
COLUMN_HEADER = re.compile(
    r"^(name(\s*of\s*creditor)?|creditor(\s*name|\s*type)?|address|"
    r"related(\s*part(y|ies))?(\s*\(?yes\s*/?\s*no\)?)?|"
    r"(rocap|identified|estimated|claimed|stated)?\s*amount(\s*owed)?"
    r"(\s*\(\$\))?|estimated\s*return|balance|total|\$|#|no\.)$",
    re.IGNORECASE,
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


def is_creditor_table(page_text: str) -> bool:
    """Does this page carry an actual creditor table?

    True for either known layout, and false for narrative. These reports run
    20-40 pages and mention creditors, remuneration and even "vote Yes, No or
    Object" throughout, so a heading match locates prose far more often than a
    table - which is how a remuneration schedule once became five creditors.
    """
    lines = page_text.splitlines()
    yes_no = sum(1 for line in lines if YES_NO_CELL.match(line))
    money = sum(1 for line in lines if MONEY_CELL.match(line))
    heading = any(h in page_text.lower() for h in HEADINGS)

    # A listing heading on the page plus the Related Party column is the
    # strongest signal available, and it holds when amounts are still TBC.
    # The heading requirement is what keeps the proposal response form out:
    # that page carries Yes/No cells too, but its heading is "Proposal
    # response form and notices", not a creditor listing.
    if heading and yes_no >= MIN_YES_NO_CELLS:
        return True

    # Cell-per-line with quantified amounts, for a continuation page where
    # the practitioner did not repeat the heading.
    if yes_no >= MIN_YES_NO_CELLS and money >= MIN_MONEY_CELLS:
        return True

    # Inline rows: whole rows on one line, and only where the page also
    # carries a listing heading, since a single inline row is weak evidence.
    if not heading:
        return False
    return sum(1 for line in lines if INLINE_ROW.match(line)) >= MIN_INLINE_ROWS


def parse_cells(
    lines: list[str], debtor_company: str, matter_id: str, source: str,
    source_document: str | None = None,
) -> list[Creditor]:
    """Read a cell-per-line creditor table.

    PyMuPDF emits one cell per line for a ruled table, so a row arrives as a
    run of cells terminated by the related-party flag and the amount:

        Acme Building Supplies Pty Ltd
        12 Industry Rd Dandenong VIC
        No
        42,500.00

    The related-party cell is the anchor: everything between the previous row's
    amount and this Yes/No is the name and address, and the next money cell is
    the amount. Anchoring on it rather than on position survives the optional
    columns (creditor type, estimated return) that some practitioners add.
    """
    creditors: list[Creditor] = []
    buffer: list[str] = []

    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue

        if YES_NO_CELL.match(line):
            # Find this row's amount cell: either a number or an explicit
            # "not quantified yet" marker such as TBC.
            amount: float | None = None
            amount_known = True
            cursor = index + 1
            while cursor < len(lines) and cursor <= index + 4:
                candidate = lines[cursor].strip()
                if MONEY_CELL.match(candidate):
                    amount = float(re.sub(r"[^0-9.]", "", candidate) or 0)
                    break
                if UNQUANTIFIED.match(candidate):
                    amount, amount_known = 0.0, False
                    break
                cursor += 1

            cells = [c.strip() for c in buffer if c.strip()]
            # Drop the section heading and the column headers, which sit in the
            # buffer ahead of the first row and would otherwise be read as the
            # first creditor's name.
            cells = [
                c for c in cells
                if not COLUMN_HEADER.match(c)
                and not PAGE_FURNITURE.match(c)
                and not any(h in c.lower() for h in HEADINGS)
                and not ANNEXURE.match(c)
            ]
            if cells and amount is not None:
                name = cells[0]
                address = " ".join(cells[1:]) or None
                if (
                    NAME_RE.search(name)
                    and not NOT_A_CREDITOR.match(name)
                    and not name.rstrip().endswith(":")
                    and (name[:1].isupper() or name[:1].isdigit())
                ):
                    creditors.append(
                        Creditor(
                            creditor_name=name,
                            debtor_company=debtor_company,
                            matter_id=matter_id,
                            amount_aud=amount,
                            amount_known=amount_known,
                            address=address,
                            related_party=line.lower() == "yes",
                            source=source,
                            source_document=source_document,
                        )
                    )
            buffer = []
            index = cursor + 1 if amount is not None else index + 1
            continue

        if not MONEY_CELL.match(line) and not LEADERS.search(line):
            buffer.append(line)
        index += 1

    return creditors


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
        # A trailing colon means a label/value pair from a fee or position
        # table, not a creditor row.
        if head.rstrip().endswith(":") or NOT_A_CREDITOR.match(head.lstrip()):
            continue
        # A creditor name is a name, not a sentence.
        if len(head) > 90 or head.count(" ") > 12:
            continue
        # A line continuing prose from the page above starts mid-sentence.
        # Registered names start with a capital or a digit. This does drop a
        # deliberately lower-cased trading name, which is the right trade:
        # a missing creditor can be recovered from the source document, a
        # fabricated one reaches the sales team as a real company.
        if not head[:1].isupper() and not head[:1].isdigit():
            continue
        if PROSE_TAIL.search(head.rstrip(" .,;:")):
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

    # Only harvest pages that are demonstrably creditor tables. A page that
    # merely mentions a heading is not one, and treating it as one publishes
    # fee lines and narrative fragments as named creditors owed money.
    creditors: list[Creditor] = []
    table_pages = 0
    for page_text in pages:
        if not is_creditor_table(page_text):
            continue
        table_pages += 1
        lines = page_text.splitlines()
        # Try both layouts and keep whichever actually yielded rows.
        rows = parse_cells(lines, debtor_company, matter_id, source, path.name)
        if not rows:
            rows = parse_lines(lines, debtor_company, matter_id, source, path.name)
        creditors.extend(rows)

    if not table_pages:
        # Distinguish "no creditor table in this document" from "we could not
        # read the table", because the first is a normal, common outcome and
        # the second is a bug to fix.
        heading = any(h in p.lower() for p in pages for h in HEADINGS)
        log.info(
            "%s: no creditor table (%d pages, listing heading %s)",
            path.name, len(pages), "present but no table" if heading else "absent",
        )
        return [], "no-section"

    if not creditors:
        log.warning(
            "%s: %d page(s) look like a creditor table but no rows parsed - "
            "the row layout differs from both known ones, do not assume the "
            "document is empty",
            path.name, table_pages,
        )
        return [], "table-unreadable"

    log.info("%s: %d creditors from %d table page(s)",
             path.name, len(creditors), table_pages)
    return creditors, "ok"
