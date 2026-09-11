"""Find and dump the creditor-listing pages of a live Worrells report.

Why this exists: the parser reported five "creditors" all named "Fees:" on a
real Initial Advice report - it had matched lines in the practitioner's
remuneration section. These reports are 35+ pages of narrative that mentions
"creditors" constantly, so a heading match is not enough to locate the table.

The documented discriminator is the Related Party column: the creditor listing
has standalone Yes/No cells, narrative pages have none. This prints a compact
per-page profile for every page, then the verbatim text of the pages that
actually look like a table - so the row shape (one line per cell, or one line
per row) can be read off rather than assumed.

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

YES_NO = re.compile(r"^\s*(Yes|No)\s*$", re.IGNORECASE)
MONEY = re.compile(r"^\s*\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?\s*$")
INLINE_MONEY = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2}")
HEADING = re.compile(
    r"(listing of known creditors|list of creditors|schedule of debts|"
    r"creditor listing|known creditors)", re.IGNORECASE,
)


def profile(lines: list[str]) -> dict[str, int]:
    return {
        "lines": len([ln for ln in lines if ln.strip()]),
        "yesno": sum(1 for ln in lines if YES_NO.match(ln)),
        "money_cells": sum(1 for ln in lines if MONEY.match(ln)),
        "inline_money": sum(1 for ln in lines if INLINE_MONEY.search(ln)),
    }


def main() -> int:
    cfg = config.settings()["sources"]["worrells"]
    client = Client(throttle_ms=cfg["throttle_ms"])
    wanted_docs = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    # Target "Initial Advice" - the substantial report. A First Advice is
    # usually a short covering letter with no creditor annexure, so dumping
    # one tells us nothing about the table layout.
    want = (sys.argv[2] if len(sys.argv) > 2 else "initial advice").lower()

    matters = worrells.parse_new_appointments(
        client.get(cfg["base_url"] + cfg["list_path"]).text
    )

    import pymupdf

    done = 0
    for matter in matters[:40]:
        docs = [
            doc for doc in worrells.creditor_documents(
                client.get(matter.source_url).text, cfg["base_url"]
            )
            if want in doc["name"].lower()
        ]
        if not docs:
            continue

        for doc in docs[:1]:
            blob = client.get(doc["url"]).content
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "d.pdf"
                path.write_bytes(blob)
                with pymupdf.open(path) as pdf:
                    pages = [p.get_text() for p in pdf]

            print("\n" + "=" * 76)
            print(f"{matter.company_name} - {doc['name']} - {len(pages)} pages")
            print("=" * 76)

            profiles = []
            for number, text in enumerate(pages, start=1):
                lines = text.splitlines()
                info = profile(lines)
                info["heading"] = bool(HEADING.search(text))
                profiles.append((number, info, lines))

            print("\npages with any Yes/No cells or a listing heading:")
            print("  page  lines  yes/no  money-cells  inline-money  heading")
            for number, info, _ in profiles:
                if info["yesno"] or info["heading"]:
                    print(f"  {number:>4}  {info['lines']:>5}  {info['yesno']:>6}  "
                          f"{info['money_cells']:>11}  {info['inline_money']:>12}  "
                          f"{info['heading']}")

            # Verbatim dump of every page carrying any Yes/No cell, then the
            # heading pages. One of these is the listing; seeing them all is
            # the only way to learn the row shape.
            candidates = [p for p in profiles if p[1]["yesno"] >= 1][:3]
            if not candidates:
                candidates = [p for p in profiles if p[1]["heading"]][:3]
                print("\nNO page has a standalone Yes/No cell. "
                      "Dumping heading pages instead.")

            for number, info, lines in candidates:
                print(f"\n--- page {number} verbatim ({info['yesno']} yes/no, "
                      f"{info['money_cells']} money cells) ---")
                for index, line in enumerate(lines):
                    if line.strip():
                        print(f"  {index:>3}| {line[:100]}")
            done += 1
        if done >= wanted_docs:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
