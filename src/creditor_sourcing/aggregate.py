"""Collapse per-matter creditor rows into one prospect per company.

The same supplier turns up in several administrations. Left as raw rows the
sales team sees five near-duplicate leads; aggregated, they see one company
with a total exposure and a count of how many insolvencies it is caught in.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from .models import Creditor, Prospect


def build(creditors: Iterable[Creditor]) -> list[Prospect]:
    grouped: dict[str, list[Creditor]] = defaultdict(list)
    for creditor in creditors:
        if creditor.related_party:
            # Related parties are never arm's length trade suppliers.
            continue
        key = creditor.name_key
        if key:
            grouped[key].append(creditor)

    prospects: list[Prospect] = []
    for key, rows in grouped.items():
        # Use the longest name seen as the display name - the shorter variants
        # are usually truncated PDF cells.
        display = max((r.creditor_name for r in rows), key=len).strip()
        prospects.append(
            Prospect(
                name_key=key,
                display_name=display,
                total_exposure_aud=round(sum(r.amount_aud for r in rows), 2),
                matter_count=len({r.matter_id for r in rows}),
                matters=[
                    {
                        "matter_id": r.matter_id,
                        "debtor_company": r.debtor_company,
                        "amount_aud": r.amount_aud,
                        "source": r.source,
                        "source_document": r.source_document,
                    }
                    for r in rows
                ],
            )
        )
    return prospects
