# Runbook

## The weekly routine

**Monday 10:00 (Australia/Adelaide)** the pipeline runs itself. Nothing is
required of anyone unless there are documents to buy.

1. Open the **run summary** on the Actions run — prospect counts, the top
   repeat-exposure companies, and the list of documents waiting to be bought.
   Worrells creditors are already in there: that leg harvests itself and needs
   nothing from you.
2. If documents are queued, open the **purchase dashboard**, buy them on ASIC
   Connect (the links go straight to each company), and drop the PDFs back on
   the page.
3. Download the workbook from the run's artifacts and send it to the team.
4. Purchased documents are ingested on the next run. To get them the same day,
   re-run the workflow with `stages = ingest` then `stages = report`.

## Running it on demand

From the Actions tab, **Weekly creditor sourcing → Run workflow**, leaving
`ignore_schedule` ticked. Or locally:

```bash
export PYTHONPATH=src
export PIPEDRIVE_API_TOKEN=...
python -m creditor_sourcing run --policylist ~/Downloads/PolicyList.xlsx
```

## Setup

### 1. Repository secrets

`Settings → Secrets and variables → Actions`:

| Secret | Needed for |
|---|---|
| `PIPEDRIVE_API_TOKEN` | CRM cross-reference and notes |
| `LUSHA_API_KEY` | contact enrichment (V2) |
| `HUNTER_API_KEY` | contact enrichment (V2) |
| `WORRELLS_SESSION_COOKIE` | the Worrells portal, if it needs a session |
| `QUEUE_ENDPOINT` | the Cloudflare dashboard URL |
| `QUEUE_TOKEN` | shared token for the dashboard API |

The run degrades gracefully: every missing secret disables its step and logs
why, rather than failing the run.

### 2. Confirm the ASIC data set schema — do this first

The primary feed is the `Data set` sheet of ASIC's insolvency statistics
workbook. Its schema is already confirmed against the 7 September 2026 release
(see [DATA_SOURCES.md](DATA_SOURCES.md)); re-confirm after a republish:

```
Actions → ASIC data set schema → Run workflow
```

The log prints the sheet names, the detected header row, every column that
mapped onto a field, and any header it did not recognise. If a header is
unmapped and useful, add it to `HEADER_ALIASES` in `sources/asic_dataset.py`.
This workflow also runs monthly so a rename surfaces on its own.

### 3. Calibrate the scrapers

The two scraped sources — Published Notices and the Worrells portal — were
written without network access to the live sites. Before relying on either,
capture its real markup:

```
Actions → Probe live sources → Run workflow → source: asic-notices
```

Download the artifact, check the parser reads it, adjust `COLUMN_MAP` in
`sources/asic_notices.py` (or the equivalent) if the site's headers differ.
Repeat for `worrells` and for `asic-connect` with a known ACN.

**A source returning 0 rows means the selectors need recalibrating, not that
there were no insolvencies that week.** Probe again.

### 4. Cloudflare dashboard

```bash
cd cloudflare
npx wrangler d1 create creditor-sourcing          # put the id in wrangler.toml
npx wrangler d1 execute creditor-sourcing --file schema.sql --remote
npx wrangler r2 bucket create creditor-sourcing-documents
npx wrangler secret put QUEUE_TOKEN               # same value as the repo secret
npx wrangler deploy
```

Then in **Zero Trust → Access → Applications**, add a self-hosted application
covering the Worker's route, restricted to the NCI email domain. The `/api`
routes used by Actions authenticate with `QUEUE_TOKEN` independently, so
automation does not depend on Access.

### 5. PolicyList

Client exclusion is off until a PolicyList export is supplied. Pass it with
`--policylist <path>`, or commit it and set the path in the workflow. Without
it, existing clients will appear as prospects.

## Tuning the rules

Both files are plain YAML, no code change needed:

- `config/settings.yml` — the $5,000 floor, the watch window, scoring weights,
  the schedule.
- `config/exclusions.yml` — which creditors are not trade suppliers.

After changing exclusions, check the **Excluded** tab of the next workbook: it
shows every dropped company and the rule that dropped it.

## When something looks wrong

| Symptom | Cause | Fix |
|---|---|---|
| `collect` raises "company-name column" | the data set sheet was renamed | run the ASIC data set schema workflow, update `HEADER_ALIASES` |
| A scraped source returns 0 rows | site markup changed | run the probe workflow, recalibrate |
| Worrells matters collected but no creditors | most are days old with nothing lodged | normal — they stay open and are re-checked weekly |
| A Worrells matter shows `no-section` | its documents carry no listing | expected on a First Advice; the matter stops being re-fetched |
| Creditor names have address fragments | run-together PDF cells | acceptable; tune the split in `parse/creditor_tables.py` |
| A document is `scanned` | image-only PDF | read it by hand — OCR is not trusted to publish names |
| A document is `missing` | ASIC/portal returned 404 | leave it; the matter stays open and retries next week |
| Existing clients appear as prospects | no PolicyList supplied | pass `--policylist` |
| The weekly run did nothing | the 10am local gate | expected on one of the two cron slots |

**If a run fails partway, do not hand-edit `state/`.** Leaving it untouched
means everything retries next week and nothing is lost.
