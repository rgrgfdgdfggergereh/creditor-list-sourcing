"""Dump the raw text around creditor headings in a live Worrells PDF.

The parser reported five "creditors" all named "Fees:" on a real Initial
Advice report - it was harvesting the practitioner's remuneration table. The
parser cannot be fixed by guessing; this prints what the document actually
contains so the section boundaries and row shape can be calibrated against it.

Read-only. Run from CI, where the portal is reachable.
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

# Anything that might mark the start or end of a table we care about.
MARKERS = re.compile(
    r"(listing of known creditors|list of creditors|schedule of debts|"
    r"unsecured creditors|secured creditors|priority creditors|related part|"
    r"remuneration|fees|disbursement|declaration of independence|"
    r"estimated statement of position|summary of affairs|annexure)",
    re.IGNORECASE,
)
MONEY = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2}")


def main() -> int:
    cfg = config.settings()["sources"]["worrells"]
    client = Client(throttle_ms=cfg["throttle_ms"])
    wanted = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    matters = worrells.parse_new_appointments(
        client.get(cfg["base_url"] + cfg["list_path"]).text
    )

    import pymupdf

    done = 0
    for matter in matters[:40]:
        page_html = client.get(matter.source_url).text
        docs = worrells.creditor_documents(page_html, cfg["base_url"])
        if not docs:
            continue

        for doc in docs[:1]:
            blob = client.get(doc["url"]).content
            print("\n" + "=" * 78)
            print(f"{matter.company_name} - {doc['name']} ({len(blob):,} bytes)")
            print("=" * 78)

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "d.pdf"
                path.write_bytes(blob)
                with pymupdf.open(path) as pdf:
                    pages = [p.get_text() for p in pdf]

            print(f"Pages: {len(pages)}")
            for number, text in enumerate(pages, start=1):
                if not MARKERS.search(text):
                    continue
                lines = [ln.rstrip() for ln in text.splitlines()]
                hits = [ln for ln in lines if MARKERS.search(ln)]
                money_lines = sum(1 for ln in lines if MONEY.search(ln))
                yes_no = sum(1 for ln in lines if re.fullmatch(r"\s*(Yes|No)\s*", ln))
                print(f"\n-- page {number}: {len(lines)} lines, "
                      f"{money_lines} with amounts, {yes_no} standalone Yes/No")
                print(f"   markers: {hits[:6]}")
                # Print the page verbatim so row shape is unambiguous.
                for index, line in enumerate(lines):
                    if line.strip():
                        print(f"   {index:>3}| {line[:110]}")
            done += 1
        if done >= wanted:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
