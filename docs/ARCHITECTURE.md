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

**ingest** — parse creditor tables out of the PDFs in `state/inbox/`, whether
they came from an ASIC purchase or a free Worrells download.

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

**Never invent a creditor.** A page counts as a creditor table only if it has a
name *and* a dollar amount. Contents entries with dotted leaders, totals rows
and page markers are excluded explicitly, because both the table of contents
and the sentence "list of creditors and summary of affairs" contain the heading
words. Scanned documents with no extractable text are reported for manual
review rather than run through OCR — publishing a wrong creditor name and
amount is worse than publishing nothing.

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
