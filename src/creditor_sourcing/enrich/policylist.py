"""Load the NCI PolicyList export so existing clients can be excluded."""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import normalise_name

log = logging.getLogger(__name__)

NAME_HEADERS = ("client name", "company name", "insured", "policy holder",
                "policyholder", "name", "client")


def load(path: Path) -> dict[str, str]:
    """Return {normalised name: original name} for every policyholder.

    Reads the first sheet and finds the client-name column by header text, so
    an export with extra columns or a different column order still works.
    """
    from openpyxl import load_workbook

    if not path.exists():
        log.warning("PolicyList not found at %s - client exclusion is OFF", path)
        return {}

    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    rows = sheet.iter_rows(values_only=True)

    header = next(rows, None)
    if not header:
        return {}
    lowered = [str(h or "").strip().lower() for h in header]
    column = next(
        (i for i, h in enumerate(lowered) if any(n == h for n in NAME_HEADERS)),
        None,
    )
    if column is None:
        column = next(
            (i for i, h in enumerate(lowered) if any(n in h for n in NAME_HEADERS)),
            None,
        )
    if column is None:
        log.warning("No client-name column in %s (headers: %s)", path, lowered)
        return {}

    clients: dict[str, str] = {}
    for row in rows:
        if column >= len(row):
            continue
        name = str(row[column] or "").strip()
        key = normalise_name(name)
        if key:
            clients.setdefault(key, name)
    workbook.close()
    log.info("PolicyList: %d client names loaded from %s", len(clients), path.name)
    return clients
