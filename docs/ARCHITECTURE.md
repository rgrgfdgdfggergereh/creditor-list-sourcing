# Architecture

## The constraint that shapes everything

The environment Claude analyses this repository in has **no outbound network
access except GitHub**. It cannot reach asic.gov.au, the Worrells portal, or
Cloudflare. So the agent session cannot be the thing that fetches data.

That splits the system in three, by what each part is actually good at:

| Layer | Runs | Does |
|---|---|---|
| **GitHub Actions** | weekly cron + on demand | all network fetching, parsing, qualification, the workbook |
| **Cloudflare** | always on | the purchase-queue dashboard, R2 storage for bought PDFs |
| **Claude session** | weekly, and on demand | judgement: reviewing the shortlist, contact enrichment via Lusha/Pipedrive MCP, the brief |

Actions has full network access and free scheduled minutes. Cloudflare gives a
URL a human can open on their phone, which is what the manual purchase step
needs. Claude has the MCP connectors — Pipedrive, Lusha, Microsoft 365 — that
Actions does not.

## Pipeline

```
  collect ──► watch ──► [ HUMAN BUYS ] ──► ingest ──► report
     │          │              │              │          │
  ASIC       ASIC          Cloudflare      creditor   qualify
  data set   Connect        dashboard       tables    + score
  Notices    doc list       R2 upload       from PDF  + enrich
  Worrells
```

**collect** — new external administrations from the `data set` sheet of ASIC's
insolvency statistics workbook (the primary feed: one structured download, with
industry and state on every row), optionally corroborated by ASIC Published
Notices for appointments newer than the last monthly workbook, plus the
Worrells New Appointments list. Merged into `state/matters.json`.

**watch** — for every open matter, check the ASIC Connect document list for a
lodged Form 5604. Free. Matters without one stay open and are re-checked next
week for `watch_days` (default 120) — the form is often lodged well after the
appointment, and closing the matter early loses the lead permanently.

**buy** — the one manual step. See below.

**ingest** — parse creditor tables out of PDFs in `state/inbox/`. This is now
the *purchased* path only: Worrells documents are harvested automatically in
`watch`, so the inbox holds the Form 5604 documents a human bought.

**report** — aggregate to one prospect per company, qualify, enrich, build the
workbook.

Each stage runs independently, so a failing source does not cost the week's
run, and `ingest` can be re-run on its own after a purchase without re-scraping.

## The one manual step

Buying a Form 5604 costs money per document and the transaction is not
idempotent — a retried purchase buys it twice. So the pipeline stops short of
it deliberately.

The Cloudflare Worker in `cloudflare/` is the handover:

1. The weekly run POSTs the purchase queue to `/api/queue`.
2. A human opens the dashboard (behind Cloudflare Access), sees exactly what to
   buy, and clicks straight through to each company on ASIC Connect.
3. They drop the purchased PDFs back on the same page; the Worker stores them
   in R2 and marks the row received.
4. The next run pulls them into `state/inbox/` and ingests them.

One page, one pass, no file shuffling. If Cloudflare is not set up, the same
loop works by dropping PDFs into `state/inbox/` named `<matter_id>__<name>.pdf`.

## Why state is committed JSON

`state/` holds the run's memory: which administrations we have seen, which have
a 5604, what is waiting to be bought. It is JSON in the repository rather than
a database because the weekly job is an Actions runner with no persistent disk,
and because **every state change should be visible in a diff**. When the run
does something surprising, `git log state/` says exactly what changed and when.

D1 holds only the purchase queue, because the dashboard needs to write to it
(marking a document received) and that write has to survive the next refresh.

## Scoring: why repeat exposure beats a big single debt

```python
score = (matter_count - 1) * 25 + log10(total_exposure) * 10
```

A supplier owed $400,000 by one collapsed customer has had bad luck. A supplier
owed $30,000 by each of three collapsed customers in a quarter has a credit
management problem, is feeling it right now, and is the better conversation.
The repeat term is weighted to reflect that. `log10` on exposure lets a $2m
debt outrank a $200k one without swamping the repeat signal.

This is the one piece of genuine product judgement in the pipeline, and it is
deliberately in one readable line so it can be argued with and tuned.

## Design rules

**Never invent a creditor.** This rule was written first and then broken, so
it is worth recording exactly how. An early parser turned its creditor section
on when a heading appeared anywhere in the document, then took any line ending
in a dollar amount. On live Worrells reports it produced five "creditors" named
`Fees:` totalling $66,960 — the practitioner's remuneration schedule — plus
prose fragments like `report to creditors of` from the sentence "report to
creditors of 10 September 2026 in the amount of $31,500.00". These reports run
20-40 pages and mention creditors, remuneration, and even "vote Yes, No or
Object" throughout, so a heading is no evidence of a table.

What actually works: a page qualifies only on **table evidence**. Either
standalone `Yes`/`No` cells (the Related Party column) plus amount-only cells,
or — with a listing heading on the same page — two or more complete inline
rows. Narrative pages score zero standalone Yes/No cells; a real listing
page scores 4 to 21.

The row reader has to match the layout too. PyMuPDF emits **one cell per line**
for a ruled table, so a row arrives as a run of cells, not a single line. A
line-based parser cannot read a real listing at all — the only single-line
"name plus amount" text in these documents is narrative and fee labels, so it
finds exclusively the wrong rows. `parse_cells()` handles the cell layout and
anchors each row on the related-party cell, so an added Creditor Type or
Estimated Return column cannot shift the data.

Rows are rejected when the name is a fee or position label, ends in a colon,
starts lower-case or ends on a preposition (prose continuing from the line
above), or runs to sentence length. That last rule drops a deliberately
lower-cased trading name, which is the right trade: a missing creditor can be
recovered from the source document, a fabricated one reaches the sales team as
a real company owed real money.

Scanned documents with no extractable text are reported for manual review
rather than run through OCR. `extract_pdf` also separates "no creditor table
in this document" from "pages look like a table but no rows parsed", because
the first is a normal, common outcome and the second is a bug.

**Parse by header, not by position.** Every table parser maps column *header
text* to fields. A reordered or added column then degrades to "not found"
instead of silently reading the wrong cell.

**Excluded is not deleted.** Everything dropped keeps its reason and ships on
its own tab. Exclusion rules that cannot be audited get either too loose or too
tight and nobody notices.

**Secrets only from the environment.** `config.secret()` reads from the process
environment; nothing credential-shaped is ever read from the repository.

## Version 2

- **Pipedrive leads.** The write path already exists (`enrich/pipedrive.py`
  annotates and can push notes). V2 turns qualified prospects into Leads with
  the creditor context attached, instead of a workbook the rep copies from.
- **Lusha and Hunter contact enrichment** inside the Actions run rather than in
  the Claude session, so the workbook ships with contacts already in it.
- **More practitioner portals.** Worrells is one firm. The same
  list-then-details-then-PDF shape covers most mid-tier insolvency portals, and
  each one added is creditor data that never has to be bought from ASIC.
