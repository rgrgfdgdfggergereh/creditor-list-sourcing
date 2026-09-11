# Data sources

## ASIC insolvency statistics workbook — the primary feed

<https://www.asic.gov.au/about-asic/corporate-publications/statistics/insolvency-statistics>

The workbook ASIC publishes as *Insolvency statistics — Series 1 and Series 2*
carries the summary tables **and a sheet named `data set`** listing the
companies that entered external administration, one row per appointment. That
sheet is the primary named-company feed.

It is the best source available for this job:

- **Structured, not scraped.** One `.xlsx` download; no CSS selectors to break.
- **One request instead of hundreds.** Replaces a page-by-page crawl.
- **Carries the fields that matter** — company name, ACN, appointment type,
  effective date, ANZSIC industry division and subdivision, principal place of
  business state and postcode, and the appointed practitioner. Industry and
  state come through onto the prospect row, so a rep can see which sector the
  bad debt came from.

### Confirmed schema

Captured from the published workbook (7 September 2026 release, 20 MB) by the
**ASIC data set schema** workflow. The sheet is `Data set`, the header is on
**row 7** under five title rows and a merged group-label row, and there were
**73,206 appointment rows** going back to 1 July 2021.

| Col | Header (verbatim) | Mapped to |
|---:|---|---|
| 1 | `Data to` | — |
| 2 | `ACN No ` | `acn` |
| 3 | `Organisation name ` | `company_name` |
| 4 | `Appointee (person or company)` | `practitioner` |
| 5 | `Appointment type` | `appointment_type` |
| 6 | `Effective date` | `appointment_date` |
| 7 | `Period\n(Year month)` | — |
| 8 | `Period (financial year)` | — |
| 9 | `Industry type (division)` | `industry` |
| 10 | `Industry type (subdivision)` | `industry_subdivision` |
| 11 | `Industry type (group)` | — |
| 12 | `State of incorporation (state or territory)` | `state_of_incorporation` |
| 13 | `Principal place of business (state or territory)` | `state` |
| 14 | `Principal place of business (area)` | — |
| 15 | `Principal place of business (postcode)` | `postcode` |
| 16 | `Series 1 \n(companies entering)` | `series_1` |
| 17 | `Series 2 \n(all appointments)` | `series_2` |

Three things in that layout matter more than they look:

**ACNs are stored as numbers, so leading zeros are gone.** `TANCRED BROTHERS
PTY LTD` arrives as `25712` and is really ACN 000 025 712. The loader
zero-pads to nine digits. Validating on length and dropping short values —
the obvious first cut — silently loses every company registered early enough
to have a low ACN.

**Series 1 marks a company's first entry into external administration.** Series
2 counts *every* appointment, so the same company recurs for each subsequent
one. The loader keeps Series 1 rows only, or the same insolvency produces
duplicate matters and double-counts its creditors.

**Members' voluntary liquidation is a solvent wind-up** — nobody lost money, so
those rows are never prospect material and are dropped by appointment type.

Appointment types as published: `Creditors' voluntary liquidation`, `Court
liquidation`, `Voluntary administration`, `Controller appointed (except
receiver or managing controller)`. Only the creditors' voluntary liquidations
reliably produce a Form 5604; the others carry creditor information on
different forms.

Principal place of business is preferred over state of incorporation for sales
territory, because it is where the company actually trades.

### Why it holds up

`sources/asic_dataset.py` reads it. Two things make it robust rather than
brittle:

- **The header row is found by content, not position.** ASIC puts title and
  note rows above the real header, so the loader scans for the first row that
  matches at least two known header names. `HEADER_ALIASES` holds every
  spelling ASIC has used for each field.
- **A renamed schema raises instead of returning nothing.** "No insolvencies
  this week" and "the columns moved" must never look the same, or a silent
  zero gets reported as a quiet week.

The published URL embeds the publication date and ASIC mints a new media id
each release, so `resolve_latest_url()` reads the landing page for the current
workbook rather than trusting a pinned URL. The configured URL in
`config/settings.yml` is the fallback.

To re-confirm the shape of the sheet after a republish, run the **ASIC data set
schema** workflow.
It prints the sheet names, the detected header row, which columns mapped to
which fields, and any header it did not recognise — and saves the workbook as
an artifact. That workflow also runs monthly, so a column rename surfaces
before it breaks a Monday run.

### On the Series 1 / Series 2 summary tables

The summary tables in the same workbook are aggregate counts by month, state,
industry and appointment type, with no company names. They are the right
source for insolvency **trend commentary** (and for the monthly Industry
Report) but cannot produce prospects. The `data set` sheet is the one this
pipeline reads.

## ASIC Published Notices — corroboration and fresher timing

<https://publishednotices.asic.gov.au>

Free, no registration, no fee to search. Publishes the notices companies must
give under the Corporations Act, including appointment of an external
administrator. Carries company name, ACN, notice type, date and the appointed
practitioner. Coverage starts 1 July 2012.

Its value alongside the workbook is **timing**: notices appear within days of
an appointment, while the workbook is republished monthly. It is a secondary
source — enabled with `--sources asic-notices` — and catches this month's
appointments before the next workbook lands.

## ASIC Connect — Form 5604 detection

<https://connectonline.asic.gov.au>

**Form 5604, "Information about the company's affairs sent to creditors"**, is
the document that carries the creditor list: names, addresses and estimated
amounts owed. A liquidator must lodge it within 10 business days of the
resolution to wind the company up, and the lodgement fee is nil — so for
creditors' voluntary liquidations it is reliably there.

The economics that shape the design:

| | Cost |
|---|---|
| Searching the organisation | free |
| Seeing the **document list** — form code, document number, date | free |
| Downloading the **document image** | paid, per document |

So the pipeline detects for free whether a 5604 exists, and queues the document
for a human to buy. **Purchasing is never automated**: it spends real money per
document and a retry buys the same document twice.

## Worrells customer portal — creditor lists without paying

<https://customerportal.worrells.net.au>

Worrells publishes its Initial Advice reports to creditors. Those contain the
"Listing of known creditors" table — the same data a Form 5604 carries, free,
and available the week of appointment rather than after a purchase. When a
matter is a Worrells appointment this is always the cheaper and faster path.

**The URL rewrite this depends on.** The New Appointments list links each
matter as

```
/FileInformation/FileInformation/202502200334422382761DFEC99D43BE
```

which is not the page carrying the documents. Keep the 32-character hex id and
swap the path segment:

```
/FileInformation/FileInformationDetailsView/202502200334422382761DFEC99D43BE
```

`sources/worrells.py:details_view_url()` does this, and accepts either URL form
or a bare id.

## Scraper calibration

The environment these parsers were written in has no outbound network access,
so selectors could not be checked against the live sites. Rather than ship
guessed selectors, each source has a **probe**:

```bash
gh workflow run probe.yml -f source=asic-notices
```

It captures the live HTML as a build artifact. Calibrate the parser against
that markup before trusting a source. The parsers are written to read by column
*header text* rather than position, and to yield nothing rather than guess, so
a layout change shows up as "0 rows" instead of silently wrong data.

Run the probe again whenever a source starts returning zero rows.
