"""Report on the live state of all three sourcing legs.

Run this in CI, where the network is reachable. It answers, for each leg:
does it respond, does the parser find rows, and what is actually in the data.
It writes only to stdout - nothing here changes state or spends money.

    python scripts/diagnose.py            # all three legs
    python scripts/diagnose.py asic       # one leg
"""

from __future__ import annotations

import io
import sys
import traceback
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from creditor_sourcing import config  # noqa: E402
from creditor_sourcing.sources import (  # noqa: E402
    asic_dataset,
    asic_notices,
    worrells,
)
from creditor_sourcing.sources.http import Client  # noqa: E402

WORRELLS_SAMPLE_ID = "202502200334422382761DFEC99D43BE"
RULE = "=" * 78


def heading(text: str) -> None:
    print(f"\n{RULE}\n{text}\n{RULE}")


def verdict(ok: bool, text: str) -> None:
    print(f"\n  {'WORKS' if ok else 'BLOCKED'}: {text}")


def looks_like_login(html: str) -> bool:
    lowered = html.lower()
    signals = ("type=\"password\"", "type='password'", "sign in", "log in",
               "login", "forgot your password")
    return sum(s in lowered for s in signals) >= 2


# --------------------------------------------------------------------------
# ASIC statistics workbook - the primary feed
# --------------------------------------------------------------------------
def diagnose_asic() -> None:
    heading("LEG 1 - ASIC insolvency statistics workbook ('Data set' sheet)")
    client = Client()

    url = asic_dataset.resolve_latest_url(client)
    configured = config.settings()["sources"]["asic_stats"]["url"]
    print(f"  Resolved URL : {url}")
    print(f"  Configured   : {configured}")
    print(f"  Resolver agrees with config: {url == configured}")

    payload = asic_dataset.download(url, client)
    print(f"  Downloaded   : {len(payload):,} bytes")

    # Read the raw sheet once so we can report on the data itself.
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    try:
        sheet = asic_dataset._find_sheet(workbook)
        rows = list(sheet.iter_rows(values_only=True))
    finally:
        workbook.close()
    header_index, mapping = asic_dataset._find_header(rows)
    columns = {field: col for col, field in mapping.items()}

    dates: list[str] = []
    types: Counter[str] = Counter()
    industries: Counter[str] = Counter()
    series_1 = 0
    for row in rows[header_index + 1:]:
        if columns.get("company_name") is None:
            break
        name = row[columns["company_name"]] if columns["company_name"] < len(row) else None
        if not name:
            continue
        first = asic_dataset._is_true(row[columns["series_1"]]) if "series_1" in columns else True
        if not first:
            continue
        series_1 += 1
        when = asic_dataset._parse_date(row[columns["appointment_date"]])
        if when:
            dates.append(when)
        kind = row[columns["appointment_type"]]
        if kind:
            types[str(kind).strip()] += 1
        if "industry" in columns:
            industry = row[columns["industry"]]
            if industry:
                industries[str(industry).strip()] += 1

    dates.sort()
    print(f"\n  First appointments (Series 1) : {series_1:,}")
    print(f"  Appointment date range        : {dates[0]} to {dates[-1]}")

    # THE question for a weekly run: how fresh is the newest data?
    lag = (date.today() - date.fromisoformat(dates[-1])).days
    print(f"  Newest appointment is {lag} days old (today is {date.today()})")

    by_month = Counter(d[:7] for d in dates)
    print("\n  Appointments per month, last 8 months in the file:")
    for month, count in sorted(by_month.items())[-8:]:
        print(f"    {month}  {count:>6,}  {'#' * min(40, count // 40)}")

    print("\n  Appointment types (Series 1):")
    for kind, count in types.most_common():
        share = 100 * count / series_1
        flag = "  <- Form 5604 expected" if "creditors' voluntary" in kind.lower() else ""
        print(f"    {count:>7,} ({share:>4.1f}%)  {kind}{flag}")

    print("\n  Top industries (Series 1):")
    for industry, count in industries.most_common(8):
        print(f"    {count:>7,}  {industry}")

    # What the configured weekly lookback would actually pick up.
    print("\n  What `collect` would return at various lookback windows:")
    for days in (14, 30, 60, 90, 180):
        found = asic_dataset.parse(payload, lookback_days=days)
        cvl = sum(1 for m in found
                  if m.appointment_type and "creditors' voluntary" in m.appointment_type.lower())
        print(f"    {days:>4} days: {len(found):>6,} matters ({cvl:,} CVL)")

    configured_days = config.settings()["sources"]["asic_notices"]["lookback_days"]
    live = asic_dataset.parse(payload, lookback_days=configured_days)
    verdict(bool(live),
            f"ASIC leg returns {len(live):,} matters at the configured "
            f"{configured_days}-day lookback")
    if not live:
        print("  -> The configured lookback is SHORTER than the publication lag.")
        print("     A weekly run would find nothing. Widen sources.asic_stats")
        print("     lookback, or key freshness off the file's own newest date.")


# --------------------------------------------------------------------------
# ASIC Published Notices - the scraped secondary feed
# --------------------------------------------------------------------------
def diagnose_published_notices() -> None:
    heading("LEG 2 - ASIC Published Notices (scraped)")
    cfg = config.settings()["sources"]["asic_notices"]
    client = Client()
    url = cfg["base_url"] + asic_notices.SEARCH_PATH
    print(f"  URL: {url}")

    response = client.session.get(url, timeout=45)
    html = response.text
    print(f"  HTTP {response.status_code}, {len(html):,} bytes, "
          f"final URL {response.url}")

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    print(f"  Tables on page: {len(tables)}")
    for index, table in enumerate(tables[:4]):
        headers = [h.get_text(strip=True) for h in table.find_all("th")][:10]
        print(f"    table {index}: {len(table.find_all('tr'))} rows, headers={headers}")

    forms = soup.find_all("form")
    print(f"  Forms: {len(forms)}")
    if forms:
        names = [i.get("name") or i.get("id") for i in forms[0].find_all(["input", "select"])]
        print(f"    first form fields: {[n for n in names if n][:12]}")

    matters = asic_notices.parse_results(html, base_url=cfg["base_url"])
    print(f"\n  Parser found {len(matters)} rows")
    for matter in matters[:5]:
        print(f"    {matter.company_name} | {matter.acn} | "
              f"{matter.appointment_type} | {matter.appointment_date}")

    verdict(bool(matters),
            f"Published Notices parser yields {len(matters)} rows from the browse page")
    if not matters:
        print("  -> The browse page is almost certainly a search FORM, not a")
        print("     results list. Results need a POST (or the site is a JS app).")
        print("     Given the workbook already covers named companies, the")
        print("     question is whether this leg is worth building at all.")


# --------------------------------------------------------------------------
# Worrells portal - the free creditor-list feed
# --------------------------------------------------------------------------
def diagnose_worrells() -> None:
    heading("LEG 3 - Worrells customer portal")
    cfg = config.settings()["sources"]["worrells"]
    client = Client(throttle_ms=cfg["throttle_ms"])

    list_url = cfg["base_url"] + cfg["list_path"]
    print(f"  New Appointments: {list_url}")
    try:
        response = client.session.get(list_url, timeout=45, allow_redirects=True)
        html = response.text
        print(f"  HTTP {response.status_code}, {len(html):,} bytes")
        print(f"  Final URL: {response.url}")
        print(f"  Looks like a login page: {looks_like_login(html)}")
        matters = worrells.parse_new_appointments(html)
        print(f"  Parser found {len(matters)} appointments")
        for matter in matters[:5]:
            print(f"    {matter.company_name} -> {matter.source_url}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        matters = []

    # The URL rewrite Daniel described: does the DetailsView page render
    # without a portal session?
    view_url = worrells.details_view_url(WORRELLS_SAMPLE_ID)
    print(f"\n  DetailsView probe: {view_url}")
    documents = []
    try:
        response = client.session.get(view_url, timeout=45, allow_redirects=True)
        html = response.text
        print(f"  HTTP {response.status_code}, {len(html):,} bytes")
        print(f"  Final URL: {response.url}")
        print(f"  Looks like a login page: {looks_like_login(html)}")
        documents = worrells.parse_documents(html, cfg["base_url"])
        print(f"  PDF documents found: {len(documents)}")
        for doc in documents[:8]:
            print(f"    {doc['name']!r} -> {doc['url']}")
        creditor_docs = worrells.creditor_documents(html, cfg["base_url"])
        print(f"  Of those, creditor-listing documents: {len(creditor_docs)}")
        if not documents:
            text = " ".join(html.split())[:400]
            print(f"  Page text starts: {text!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")

    verdict(bool(documents) or bool(matters),
            f"Worrells reachable unauthenticated: list={len(matters)} rows, "
            f"detail={len(documents)} PDFs")
    if not documents and not matters:
        print("  -> Portal needs an authenticated session. Options: a session")
        print("     cookie in WORRELLS_SESSION_COOKIE, or keep this leg as a")
        print("     local run driven through a logged-in browser.")


LEGS = {
    "asic": diagnose_asic,
    "notices": diagnose_published_notices,
    "worrells": diagnose_worrells,
}


def main(argv: list[str]) -> int:
    wanted = argv[1:] or list(LEGS)
    failures = []
    for name in wanted:
        try:
            LEGS[name]()
        except KeyError:
            print(f"Unknown leg {name!r}; choose from {list(LEGS)}")
            return 2
        except Exception:  # noqa: BLE001 - report every leg even if one dies
            print(f"\n  UNHANDLED ERROR in the {name} leg:")
            traceback.print_exc()
            failures.append(name)

    heading("SUMMARY")
    for name in wanted:
        print(f"  {name:<10} {'errored' if name in failures else 'reported above'}")
    # Diagnosis is informational: a blocked leg is a finding, not a job failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
