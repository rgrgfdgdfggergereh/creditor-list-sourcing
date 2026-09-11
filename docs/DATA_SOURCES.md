# Data sources

## ASIC Published Notices — the feed of named companies

<https://publishednotices.asic.gov.au>

Free, no registration, no fee to search. Publishes the notices companies must
give under the Corporations Act, including appointment of an external
administrator. Carries **company name, ACN, notice type, date and the appointed
practitioner** — which is what makes it the source that produces prospects.
Coverage starts 1 July 2012.

## ASIC insolvency statistics (Series 1 and 2) — context only

The spreadsheet at `download.asic.gov.au/media/.../asic-insolvency-statistics-
series-1-and-series-2-*.xlsx` was the original intended source for this
pipeline. **It cannot do the job.** Series 1 and Series 2 are *aggregate
counts* of companies entering external administration, broken down by month,
state, industry and appointment type. There are no company names in it at all,
so nothing in it can become a prospect.

It is still useful — it is the right source for insolvency trend commentary
(and for the monthly Industry Report) — so the pipeline keeps it for the
narrative, and takes named companies from Published Notices instead.

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
