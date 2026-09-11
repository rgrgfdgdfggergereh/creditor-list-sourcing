"""ASIC Published Notices - the feed of companies entering external administration.

https://publishednotices.asic.gov.au

This is the source that yields NAMED companies. It is free, needs no
registration, and carries company name, ACN, notice type, appointment date and
the appointed practitioner. It replaces the insolvency statistics spreadsheet,
which is aggregate counts only and contains no company names at all.

Selector calibration
--------------------
The site is a server-rendered ASP.NET application whose markup is not
documented and not reachable from the analysis sandbox. Rather than guess at
selectors, `probe()` captures a live response so the parser can be calibrated
against real HTML on the first CI run. `parse_results()` is written against the
structure the site is known to present (a results table, one row per notice)
and is deliberately tolerant: it reads by column header text, not position, so
a column being added or reordered does not silently shift the data.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from .. import config
from ..models import Matter
from .http import Client

log = logging.getLogger(__name__)

ACN_RE = re.compile(r"\b(\d{3}\s?\d{3}\s?\d{3})\b")

# Header text -> the Matter field it populates. Matched case-insensitively on a
# substring so "Published date" and "Date published" both land.
COLUMN_MAP = {
    "organisation": "company_name",
    "company": "company_name",
    "name": "company_name",
    "acn": "acn",
    "abn": "acn",
    "notice type": "appointment_type",
    "type": "appointment_type",
    "published": "appointment_date",
    "date": "appointment_date",
    "practitioner": "practitioner",
    "appointee": "practitioner",
    "firm": "practitioner_firm",
}

SEARCH_PATH = "/browsesearch-notices/browse-notices"


def probe(client: Client | None = None) -> str:
    """Fetch the notices browse page and return the raw HTML.

    Used by `cli.py probe` so a CI run can upload the real markup as an
    artifact. Calibrate COLUMN_MAP and the row selector from that, not guesses.
    """
    cfg = config.settings()["sources"]["asic_notices"]
    client = client or Client()
    return client.get(cfg["base_url"] + SEARCH_PATH).text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_date(text: str) -> str | None:
    text = _clean(text)
    for fmt in ("%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_results(html: str, base_url: str = "") -> list[Matter]:
    """Turn a notices results page into Matters.

    Reads by column header rather than index, and skips any row that does not
    yield a company name - a header, spacer or "no results" row must not become
    a phantom prospect.
    """
    soup = BeautifulSoup(html, "lxml")
    matters: list[Matter] = []

    for table in soup.find_all("table"):
        headers = [_clean(th.get_text()).lower() for th in table.find_all("th")]
        if not headers:
            continue
        # Map each column index to a Matter field.
        index_map: dict[int, str] = {}
        for i, header in enumerate(headers):
            for needle, field in COLUMN_MAP.items():
                if needle in header and i not in index_map:
                    index_map[i] = field
                    break
        if "company_name" not in index_map.values():
            continue

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            values: dict[str, str] = {}
            for i, cell in enumerate(cells):
                field = index_map.get(i)
                if field and not values.get(field):
                    values[field] = _clean(cell.get_text())

            company = values.get("company_name")
            if not company:
                continue

            acn = values.get("acn")
            if not acn:
                found = ACN_RE.search(row.get_text())
                acn = found.group(1) if found else None

            link = row.find("a", href=True)
            url = None
            if link:
                href = link["href"]
                url = href if href.startswith("http") else base_url + href

            matters.append(
                Matter(
                    source="asic",
                    company_name=company,
                    acn=re.sub(r"\s", "", acn) if acn else None,
                    appointment_type=values.get("appointment_type"),
                    appointment_date=_parse_date(values.get("appointment_date", "")),
                    practitioner=values.get("practitioner"),
                    practitioner_firm=values.get("practitioner_firm"),
                    source_url=url,
                    first_seen=date.today().isoformat(),
                )
            )
    return matters


def is_external_administration(matter: Matter) -> bool:
    """Keep only notices that mean an administration has actually started."""
    cfg = config.settings()["sources"]["asic_notices"]
    wanted = [t.lower() for t in cfg["notice_types"]]
    notice = (matter.appointment_type or "").lower()
    return any(w in notice or notice in w for w in wanted)


def recent(matters: list[Matter], lookback_days: int | None = None) -> list[Matter]:
    cfg = config.settings()["sources"]["asic_notices"]
    days = lookback_days or cfg["lookback_days"]
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [m for m in matters if not m.appointment_date or m.appointment_date >= cutoff]


def collect(client: Client | None = None) -> list[Matter]:
    """Fetch and filter this week's external administration appointments."""
    cfg = config.settings()["sources"]["asic_notices"]
    client = client or Client()
    html = client.get(cfg["base_url"] + SEARCH_PATH).text
    matters = parse_results(html, base_url=cfg["base_url"])
    log.info("ASIC notices: parsed %d rows", len(matters))
    kept = recent([m for m in matters if is_external_administration(m)])
    log.info("ASIC notices: %d recent external administrations", len(kept))
    return kept
