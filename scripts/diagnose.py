"""Report on the live state of all three sourcing legs.

Run this in CI, where the network is reachable. It answers, for each leg:
does it respond, does the parser find rows, and what is actually in the data.
It writes only to stdout - nothing here changes state or spends money.

    python scripts/diagnose.py            # all three legs
    python scripts/diagnose.py asic       # one leg
"""

from __future__ import annotations

import io
import json
import re
import sys
import traceback
from collections import Counter
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from creditor_sourcing import config  # noqa: E402
from creditor_sourcing.sources import (  # noqa: E402
    asic_connect,
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

        if not documents:
            # The PDF pattern did not match. Dump every link and the page's
            # visible text so the real document URL shape can be read off,
            # rather than guessing at another regex.
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "lxml")
            anchors = [(a.get_text(strip=True)[:44], a["href"])
                       for a in soup.find_all("a", href=True)]
            print(f"  Anchors on page: {len(anchors)} - all of them:")
            for text, href in anchors:
                print(f"    {text!r:48} -> {href}")
            for tag in ("iframe", "embed", "object", "button"):
                found = soup.find_all(tag)
                if found:
                    print(f"  <{tag}> x{len(found)}: "
                          f"{[f.get('src') or f.get('data') or f.get_text(strip=True)[:40] for f in found][:6]}")
            body = soup.find("body")
            if body:
                text = " ".join(body.get_text(" ", strip=True).split())
                print(f"  Visible text ({len(text)} chars): {text[:900]!r}")

        # One matter proves nothing - this one may simply have no documents
        # lodged yet. Sample the list until we find matters that do.
        print("\n  Sampling appointments from the list for lodged documents:")
        with_docs: list[tuple] = []
        for matter in matters[:40]:
            try:
                page = client.get(matter.source_url).text
                matter = worrells.apply_details(matter, page)
                docs = worrells.creditor_documents(page, cfg["base_url"])
                if docs:
                    with_docs.append((matter, docs))
                    print(f"    {matter.company_name[:36]:<38} ACN {matter.acn or '-':<9} "
                          f"{matter.appointment_type or '-':<16} "
                          f"{[d['name'] for d in docs]}")
                if len(with_docs) >= 3:
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"    {matter.company_name[:36]:<38} FAILED: {exc}")

        checked = min(40, len(matters))
        print(f"    ({len(with_docs)} of the first {checked} sampled carry a "
              f"creditor document; the rest are too new)")
        if with_docs:
            documents = with_docs[0][1]

        # THE end-to-end proof: download a creditor document and parse it.
        # If this works, Worrells yields creditor lists with no ASIC purchase,
        # no browser automation and no login.
        print("\n  End-to-end: download a creditor document and extract creditors")
        import tempfile

        from creditor_sourcing.parse.creditor_tables import extract_pdf

        for matter, docs in with_docs[:3]:
            for doc in docs[:2]:
                try:
                    blob = client.get(doc["url"]).content
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp) / "doc.pdf"
                        path.write_bytes(blob)
                        rows, status = extract_pdf(
                            path, matter.company_name, matter.matter_id, "worrells")
                    total = sum(r.amount_aud for r in rows)
                    print(f"    {matter.company_name[:30]:<32} {doc['name'][:14]:<16} "
                          f"{len(blob):>8,}b  status={status:<10} "
                          f"creditors={len(rows):<4} total=${total:,.0f}")
                    for row in rows[:4]:
                        print(f"        {row.creditor_name[:44]:<46} ${row.amount_aud:>12,.2f}")
                except Exception as exc:  # noqa: BLE001
                    print(f"    {matter.company_name[:30]:<32} {doc['name'][:14]:<16} "
                          f"FAILED: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")

    verdict(bool(documents) or bool(matters),
            f"Worrells reachable unauthenticated: list={len(matters)} rows, "
            f"detail={len(documents)} PDFs")
    if not documents and not matters:
        print("  -> Portal needs an authenticated session. Options: a session")
        print("     cookie in WORRELLS_SESSION_COOKIE, or keep this leg as a")
        print("     local run driven through a logged-in browser.")



# --------------------------------------------------------------------------
# ASIC Connect - does the free 5604 detection actually detect anything?
# --------------------------------------------------------------------------
def diagnose_asic_connect() -> None:
    heading("LEG 4 - ASIC Connect Form 5604 detection")
    print("""
  Why: the 12 September run checked 744 creditors' voluntary liquidations on
  ASIC Connect and found ZERO Form 5604s. Across 744 CVLs, where the form is
  mandatory within 10 business days and free to lodge, zero is not a credible
  answer - it is what a silently failing lookup looks like. Nothing reaches the
  purchase queue until this works, so the paid leg has never produced a row.

  This fetches the organisation page for real CVL ACNs from committed state and
  reports what actually comes back.
""")
    client = Client()

    state = ROOT / "state" / "matters.json"
    if not state.exists():
        print("  No committed state to sample ACNs from.")
        return
    matters = json.loads(state.read_text())
    cvls = [
        m for m in matters.values()
        if m.get("source") != "worrells"
        and m.get("acn")
        and "voluntary liquidation" in (m.get("appointment_type") or "").lower()
    ]
    cvls.sort(key=lambda m: m.get("appointment_date") or "", reverse=True)
    sample = cvls[:5]
    print(f"  Sampling {len(sample)} of {len(cvls)} tracked CVLs, newest first.\n")

    reachable = 0
    with_documents = 0
    for matter in sample:
        url = asic_connect.organisation_url(matter["acn"])
        print(f"  {matter['company_name'][:52]:<52} ACN {matter['acn']}")
        print(f"    {url}")
        try:
            response = client.get(url)
        except Exception as exc:  # noqa: BLE001
            print(f"    FETCH FAILED: {type(exc).__name__}: {exc}\n")
            continue

        html = response.text
        soup = BeautifulSoup(html, "lxml")
        title = soup.title.get_text(strip=True) if soup.title else "(no title)"
        rows = soup.find_all("tr")
        # Does the company's own name appear? If not, we are not on its page.
        stem = re.split(r"\bPTY\b|\bLTD\b", matter["company_name"], flags=re.I)[0]
        stem = stem.strip()[:18]
        on_page = bool(stem) and stem.lower() in html.lower()
        print(f"    HTTP {response.status_code}  final={response.url}")
        print(f"    title={title[:70]!r}")
        print(f"    {len(html):,} bytes, {len(rows)} table rows, "
              f"company name on page={on_page}, login-ish={looks_like_login(html)}")
        if on_page:
            reachable += 1
        if len(rows) > 3:
            with_documents += 1
        hit = asic_connect.find_form_5604(html)
        print(f"    find_form_5604 -> {hit}")
        if "5604" in html:
            print("    NOTE: the string '5604' IS present in the page source.")
        print()

    verdict(
        reachable > 0,
        f"{reachable} of {len(sample)} organisation pages actually resolved to the "
        f"company; {with_documents} carried a document table.",
    )

    if reachable:
        return

    # The configured path is wrong. Rather than guess a replacement from
    # memory, ask the site which of the plausible entry points exist. Whatever
    # answers 200 with a search form is where the register actually lives.
    acn = sample[0]["acn"] if sample else "683236259"
    print("\n  Probing candidate ASIC Connect entry points:\n")
    candidates = [
        "https://connectonline.asic.gov.au/",
        "https://connectonline.asic.gov.au/robots.txt",
        "https://connectonline.asic.gov.au/RegistrySearch/",
        "https://connectonline.asic.gov.au/RegistrySearch/faces/landing/SearchRegisters.jspx",
        "https://connectonline.asic.gov.au/RegistrySearch/faces/landing/panelSearch.jspx"
        f"?searchText={acn}&searchType=OrgAndBusNm&sType=OrgAndBusNm",
        "https://connectonline.asic.gov.au/onlineservices/SearchRegisters/"
        "SearchOrganisationAndBusinessNames.aspx",
    ]
    for url in candidates:
        try:
            response = client.get(url)
        except Exception as exc:  # noqa: BLE001
            print(f"    {url[:96]}\n      -> {type(exc).__name__}: "
                  f"{str(exc)[:100]}")
            continue
        soup = BeautifulSoup(response.text, "lxml")
        title = soup.title.get_text(strip=True) if soup.title else ""
        forms = len(soup.find_all("form"))
        print(f"    {url[:96]}")
        print(f"      -> HTTP {response.status_code}  final={response.url[:96]}")
        print(f"         title={title[:64]!r}  forms={forms}  "
              f"{len(response.text):,} bytes  acn-on-page={acn in response.text}")

    # Every candidate above landed on the same "Service availability" page,
    # which is either a real ASIC outage or ASIC refusing this client. Those
    # need different fixes, so ask the same URL twice with different identities
    # and see whether the answer changes.
    print("\n  Same URL, two identities:\n")
    browser = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )
    target = "https://connectonline.asic.gov.au/RegistrySearch/faces/landing/SearchRegisters.jspx"
    for label, agent in (("project", None), ("browser", browser)):
        probe_client = Client()
        if agent:
            probe_client.session.headers["User-Agent"] = agent
        try:
            response = probe_client.get(target)
            soup = BeautifulSoup(response.text, "lxml")
            title = soup.title.get_text(strip=True) if soup.title else ""
            print(f"    {label:<8} HTTP {response.status_code}  "
                  f"{len(response.text):,} bytes  title={title[:50]!r}")
            print(f"             final={response.url[:92]}")
        except Exception as exc:  # noqa: BLE001
            print(f"    {label:<8} {type(exc).__name__}: {str(exc)[:90]}")
    print("""
    Same answer to both identities means ASIC Connect is genuinely
    unavailable right now, and this must be re-run on a weekday before the
    leg is called broken. A different answer means the client is being
    refused, which is a fix in this repo.
""")

    # ABN Lookup: a public JSON search, and the route from a company name to
    # the ABN that IRIS searches on. Checked here so a failure upstream is not
    # mistaken for a problem with this pipeline.
    print("\n  ABN Lookup (public, and the ACN -> ABN route IRIS needs):\n")
    for url in [
        "https://abr.business.gov.au/ABN/View?abn=" + acn,
        f"https://abr.business.gov.au/Search/ResultsActive?SearchText={acn}",
    ]:
        try:
            response = client.get(url)
            soup = BeautifulSoup(response.text, "lxml")
            title = soup.title.get_text(strip=True) if soup.title else ""
            print(f"    {url[:96]}\n      -> HTTP {response.status_code} "
                  f"title={title[:60]!r} {len(response.text):,} bytes")
        except Exception as exc:  # noqa: BLE001
            print(f"    {url[:96]}\n      -> {type(exc).__name__}: {str(exc)[:100]}")
    if not reachable:
        print("""
  -> A direct GET on OrganisationDetails.aspx does not reach the company.
     ASIC Connect is an ASP.NET app: the register search is a POST that sets up
     session state, and the deep link alone lands on a search or error page.
     That is the same shape as the Published Notices leg, which was disabled
     for exactly this reason.

     What this means for the pipeline: the free "has a 5604 been lodged"
     detection does not work, so the purchase queue can never fill, so the one
     manual step - buying the document - never gets a candidate. This must be
     fixed before the ASIC half of the pipeline is worth anything.
""")


# --------------------------------------------------------------------------
# ABN Lookup and IRIS - the route from a company to NCI's own limit history
# --------------------------------------------------------------------------
def diagnose_abn_and_iris() -> None:
    heading("LEG 5 - ABN Lookup markup, and whether IRIS is reachable at all")
    print("""
  IRIS is searched by ABN, and the pipeline only has company names and ACNs.
  So two things have to be true before "did NCI have limit activity on this
  debtor" can sit next to a purchase candidate: ABN Lookup has to give up the
  ABN for an ACN, and IRIS has to be reachable from wherever the job runs.

  This checks both. It sends NO credentials and attempts no login - it only
  asks what an unauthenticated request gets back.
""")
    client = Client()

    print("  ABN Lookup - resolving an ACN to an ABN\n")
    samples = [("683236259", "SUELL EARTHMOVING PTY LTD"),
               ("626084008", "BREADROLL ENTERPRISES PTY LTD")]
    for acn, name in samples:
        url = f"https://abr.business.gov.au/Search/ResultsActive?SearchText={acn}"
        try:
            response = client.get(url)
        except Exception as exc:  # noqa: BLE001
            print(f"    {name}: {type(exc).__name__}: {str(exc)[:90]}")
            continue
        soup = BeautifulSoup(response.text, "lxml")
        title = soup.title.get_text(strip=True) if soup.title else ""
        print(f"    {name[:40]:<40} ACN {acn}")
        print(f"      title={title[:66]!r}")
        # The markup this parser will have to read. Print the shape of it
        # rather than guessing selectors from memory.
        for label in ("ABN", "Entity name", "ABN status", "Entity type"):
            cell = soup.find("th", string=re.compile(label, re.I))
            value = ""
            if cell and cell.find_next("td"):
                value = " ".join(cell.find_next("td").get_text(" ", strip=True).split())
            print(f"      th {label!r:<16} -> {value[:56]!r}")
        print()

    print("  IRIS - unauthenticated reachability only, no credentials sent\n")
    for url in ["https://iris.nci.com.au/index.html",
                "https://iris.nci.com.au/"]:
        try:
            response = client.get(url)
            soup = BeautifulSoup(response.text, "lxml")
            title = soup.title.get_text(strip=True) if soup.title else ""
            print(f"    {url}")
            print(f"      -> HTTP {response.status_code}  {len(response.text):,} bytes"
                  f"  title={title[:50]!r}")
            print(f"         login page={looks_like_login(response.text)}  "
                  f"final={response.url[:80]}")
        except Exception as exc:  # noqa: BLE001
            print(f"    {url}\n      -> {type(exc).__name__}: {str(exc)[:100]}")
    print("""
    Reachable-but-login means an automated IRIS lookup needs a credential
    and NCI's say-so. Unreachable means the runner cannot see it at all and
    the signal has to come from an export instead.
""")


LEGS = {
    "asic": diagnose_asic,
    "notices": diagnose_published_notices,
    "worrells": diagnose_worrells,
    "connect": diagnose_asic_connect,
    "iris": diagnose_abn_and_iris,
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
