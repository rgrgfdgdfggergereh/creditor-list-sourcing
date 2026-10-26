"""Survey how many published Worrells documents actually contain a creditor listing.

The first dump found that Valera Recycling's 23-page First Advice contains NO
creditor listing at all: no page has a listing heading, and no page has more
than one standalone Yes/No cell. The parser's "no-section" on that document was
correct; its "ok" on the others was a false positive off the fee tables.

So the open question is no longer "can we fetch the documents" - we can - but
"do the publicly served documents carry the creditor listing at all". This
samples many documents and reports, per document, the evidence for a listing.

Read-only. Run from CI.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from creditor_sourcing import config  # noqa: E402
from creditor_sourcing.sources import worrells  # noqa: E402
from creditor_sourcing.sources.http import Client  # noqa: E402

YES_NO = re.compile(r"^\s*(Yes|No)\s*$", re.IGNORECASE)
MONEY_CELL = re.compile(r"^\s*\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?\s*$")
HEADING = re.compile(
    r"(listing of known creditors|list of creditors|schedule of debts|"
    r"creditor listing|known creditors|statement of position|"
    r"summary of affairs)", re.IGNORECASE,
)


def main() -> int:
    cfg = config.settings()["sources"]["worrells"]
    client = Client(throttle_ms=cfg["throttle_ms"])
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    matters = worrells.parse_new_appointments(
        client.get(cfg["base_url"] + cfg["list_path"]).text
    )
    print(f"New Appointments list: {len(matters)} matters\n")

    import pymupdf

    print("y/n = most standalone Yes/No cells on any one page")
    print("$cell = amount-only cells on that same page (both must co-occur)\n")
    print(f"{'company':<30} {'document':<15} {'pg':>3} {'y/n':>4} "
          f"{'$cell':>6} {'head':>5}  verdict")
    print("-" * 82)

    checked = with_listing = 0
    scanned = 0
    for matter in matters:
        if checked >= budget:
            break
        try:
            docs = worrells.creditor_documents(
                client.get(matter.source_url).text, cfg["base_url"]
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{matter.company_name[:28]:<30} detail fetch failed: {exc}")
            continue
        if not docs:
            continue

        for doc in docs:
            if checked >= budget:
                break
            try:
                blob = client.get(doc["url"]).content
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "d.pdf"
                    path.write_bytes(blob)
                    with pymupdf.open(path) as pdf:
                        pages = [p.get_text() for p in pdf]
            except Exception as exc:  # noqa: BLE001
                print(f"{matter.company_name[:28]:<30} {doc['name'][:13]:<15} "
                      f"fetch/parse failed: {exc}")
                continue

            checked += 1
            if not any(p.strip() for p in pages):
                scanned += 1
                print(f"{matter.company_name[:28]:<30} {doc['name'][:13]:<15} "
                      f"{len(pages):>3} {'-':>4} {'-':>6} {'-':>5}  SCANNED (no text)")
                continue

            # Both counts must come from the SAME page, which is what the
            # parser requires. An earlier version took the per-page maximum
            # Yes/No count but summed money cells across the whole document,
            # so almost any document scored as having a listing.
            best_yes_no = 0
            best_page_money = 0
            heading_pages = 0
            qualifying_pages = 0
            for text in pages:
                lines = text.splitlines()
                yn = sum(1 for ln in lines if YES_NO.match(ln))
                money = sum(1 for ln in lines if MONEY_CELL.match(ln))
                best_yes_no = max(best_yes_no, yn)
                if yn >= 2:
                    best_page_money = max(best_page_money, money)
                if yn >= 2 and money >= 2:
                    qualifying_pages += 1
                if HEADING.search(text):
                    heading_pages += 1

            has_listing = qualifying_pages > 0
            if has_listing:
                with_listing += 1
            verdict = "LISTING" if has_listing else "no listing"
            print(f"{matter.company_name[:28]:<30} {doc['name'][:13]:<15} "
                  f"{len(pages):>3} {best_yes_no:>4} {best_page_money:>6} "
                  f"{heading_pages:>5}  {verdict}")

    print("-" * 82)
    print(f"\n{checked} documents examined")
    print(f"{with_listing} carry a creditor listing "
          f"({100 * with_listing / checked if checked else 0:.0f}%)")
    print(f"{scanned} are image-only (no extractable text)")
    if checked and not with_listing:
        print("\nNone of the sampled documents carries a creditor listing.")
        print("The publicly served Worrells reports may omit the annexure that")
        print("the logged-in portal includes, or these recent simplified")
        print("liquidations may not publish one. Either way, Worrells cannot be")
        print("relied on as a free creditor source on this evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
