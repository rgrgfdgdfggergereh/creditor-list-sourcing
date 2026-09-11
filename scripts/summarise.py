"""Print a GitHub step summary for the weekly run."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

STATE = ROOT / "state"


def read(name, default):
    path = STATE / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def main() -> int:
    matters = read("matters.json", {})
    queue = read("purchase_queue.json", [])
    prospects = read("prospects.json", [])

    qualified = [p for p in prospects if p.get("qualified")]
    repeat = [p for p in qualified if p.get("matter_count", 0) > 1]
    in_crm = [p for p in qualified if p.get("pipedrive_org_id")]
    captured = sum(1 for m in matters.values() if m.get("creditors_captured"))

    print("## Weekly creditor sourcing\n")
    print("| | |")
    print("|---|---|")
    print(f"| Administrations tracked | {len(matters)} |")
    print(f"| Creditor lists captured | {captured} |")
    print(f"| Qualified prospects | **{len(qualified)}** |")
    print(f"| Bleeding across >1 insolvency | **{len(repeat)}** |")
    print(f"| Already in Pipedrive (note, not a new lead) | {len(in_crm)} |")
    print(f"| Excluded | {len(prospects) - len(qualified)} |")
    print(f"| **ASIC documents waiting to be purchased** | **{len(queue)}** |")

    if queue:
        print("\n### Documents to buy on ASIC Connect\n")
        print("| Company | ACN | Document | Lodged | Buy |")
        print("|---|---|---|---|---|")
        for row in queue[:25]:
            link = row.get("asic_connect_url") or ""
            print(
                f"| {row.get('company_name','')} | {row.get('acn') or ''} "
                f"| {row.get('document_number') or 'see register'} "
                f"| {row.get('lodged_date') or ''} "
                f"| {f'[open]({link})' if link else ''} |"
            )
        if len(queue) > 25:
            print(f"\n_{len(queue) - 25} more in `state/purchase_queue.json`._")

    if repeat:
        print("\n### Top repeat-exposure prospects\n")
        print("| Creditor | Total exposure | Insolvencies |")
        print("|---|---:|---:|")
        for p in sorted(repeat, key=lambda x: -x.get("total_exposure_aud", 0))[:10]:
            print(
                f"| {p['display_name']} | ${p['total_exposure_aud']:,.0f} "
                f"| {p['matter_count']} |"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
