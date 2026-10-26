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
    abn_lookup,
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

    # ASIC Connect answered on a weekday, so the Sunday result was an outage.
    # The register lives under /RegistrySearch/faces/landing/, not the
    # configured .aspx path. What is still unknown is how a search result
    # reaches a company's DOCUMENT LIST - the results page did not even
    # contain the ACN that was searched for, which is what an Oracle ADF app
    # looks like when the results arrive by stateful postback rather than in
    # the GET. So: follow the search as a browser would and print the route.
    print("\n  Following a real search for one ACN:\n")
    follow = Client()
    follow.session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    })
    landing = "https://connectonline.asic.gov.au/RegistrySearch/faces/landing/SearchRegisters.jspx"
    try:
        first = follow.get(landing)
        print(f"    landing  HTTP {first.status_code}  {len(first.text):,} bytes  "
              f"cookies={list(follow.session.cookies.keys())}")
        soup = BeautifulSoup(first.text, "lxml")
        # Form fields an ADF postback would need.
        for form in soup.find_all("form"):
            names = [i.get("name") for i in form.find_all(["input", "select"])
                     if i.get("name")]
            print(f"    form action={str(form.get('action'))[:70]!r} "
                  f"method={form.get('method')} fields={names[:12]}")
        for hidden in ("javax.faces.ViewState", "oracle.adf.view.rich.STATE"):
            tag = soup.find("input", {"name": hidden})
            if tag:
                print(f"    {hidden} present, len={len(tag.get('value') or '')}")

        results = follow.get(
            "https://connectonline.asic.gov.au/RegistrySearch/faces/landing/"
            f"panelSearch.jspx?searchText={acn}&searchType=OrgAndBusNm"
        )
        rsoup = BeautifulSoup(results.text, "lxml")
        text = rsoup.get_text(" ", strip=True)
        print(f"\n    results  HTTP {results.status_code}  {len(results.text):,} bytes")
        print(f"    acn digits on page = {acn in text.replace(' ', '')}")
        print(f"    'no results'-ish   = "
              f"{any(p in text.lower() for p in ('no match', 'no results', 'not found'))}")
        # Any link that could lead to a company or its documents.
        seen = set()
        for a in rsoup.find_all("a", href=True):
            href = a["href"]
            if any(k in href.lower() for k in
                   ("organisation", "companydetails", "documents", "detail", "extract")):
                key = href[:90]
                if key not in seen:
                    seen.add(key)
                    print(f"    link {a.get_text(' ', strip=True)[:32]!r:<36} -> {key}")
        if not seen:
            print("    NO organisation/detail links in the results HTML.")
            print("    -> the result rows are not in the GET response; the ADF")
            print("       app fetches them on a postback. A plain GET cannot")
            print("       reach the document list.")
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED: {type(exc).__name__}: {str(exc)[:120]}")

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
  -> Settled on 15 September, on a weekday, with the site up.

     The configured path is simply gone: /onlineservices/SearchRegisters/
     OrganisationDetails.aspx 404s. The register now lives at
     /RegistrySearch/faces/landing/ and is an Oracle ADF (JSF/Trinidad) app,
     not ASP.NET.

     Finding the new path does not fix the leg. A GET on panelSearch.jspx
     with the ACN returns the search SHELL, not results: 87,720 bytes with
     the ACN nowhere on the page, no "no results" message, and not one link
     to an organisation or a document list. The rows arrive on an ADF
     postback - the landing form is method=POST carrying
     org.apache.myfaces.trinidad.faces.FORM, Adf-Window-Id, a
     javax.faces.ViewState token and a JSESSIONID. A __cf_bm cookie is set
     too, so Cloudflare bot management sits in front of it.

     So the free "has a 5604 been lodged" detection cannot be done with a
     plain GET. Three ways forward, and the third deserves the most thought:

       1. Replay the ADF postback - carry the cookies, ViewState and
          Trinidad field ids. Possible, and brittle: those ids change when
          ASIC redeploys, and __cf_bm means the bot check can tighten at any
          time. Expect to re-fix it periodically.
       2. Drive a real browser. Robust against markup changes, heavy to run
          weekly, and still subject to the bot check.
       3. Do not use ASIC Connect at all. Its only job here is to find out
          that a creditor list exists so a human can buy it. The Worrells leg
          already gets complete creditor lists free, without a login, and it
          is the half of this pipeline that works. Extending that approach to
          the other large insolvency practitioners' portals would likely
          yield more prospects, at no cost per document, than fighting an ADF
          app for the right to pay ASIC per PDF.
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

    print("  abn_lookup.resolve() against the live pages\n")
    for acn, name in samples:
        abn = abn_lookup.resolve(acn, client)
        ok = abn and abn_lookup.is_valid_abn(abn)
        print(f"    {name[:40]:<40} -> "
              f"{abn_lookup.format_abn(abn) if abn else 'NOT RESOLVED':<16} "
              f"checksum={'valid' if ok else 'FAILED'}")
    print()

    print("  IRIS - unauthenticated reachability only, no credentials sent\n")
    print("""    A 403 alone does not say why. Three causes need different answers:
      app-level  - "you are not signed in". A session cookie from a logged-in
                   browser would then work from CI, the way
                   WORRELLS_SESSION_COOKIE already does.
      edge/WAF   - a Cloudflare/Akamai style block on the client or its IP
                   range. No cookie helps; the request never reaches IRIS.
      IP allow   - NCI only admits its own network. Same conclusion as a WAF.
    So print the response itself rather than just the status code.
""")
    import requests

    browser = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )
    for label, agent in (("project", None), ("browser", browser)):
        session = requests.Session()
        session.headers.update({"User-Agent": agent or Client().session.headers["User-Agent"]})
        try:
            response = session.get("https://iris.nci.com.au/index.html",
                                   timeout=30, allow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    {label:<8} {type(exc).__name__}: {str(exc)[:90]}")
            continue
        print(f"    {label:<8} HTTP {response.status_code}  "
              f"{len(response.content):,} bytes  final={response.url[:60]}")
        # The headers that name the blocker, and nothing else.
        for header in ("server", "cf-ray", "cf-mitigated", "x-amz-cf-id",
                       "x-akamai-transformed", "via", "www-authenticate",
                       "x-frame-options", "content-type"):
            if header in response.headers:
                print(f"             {header}: {response.headers[header][:70]}")
        body = " ".join(response.text.split())[:300]
        print(f"             body: {body!r}")
        print()
    print("""    A Cloudflare/Akamai signature, or a body that talks about access
    being denied rather than about signing in, means the browser route is
    the only route and it has to run on a machine NCI admits.
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
