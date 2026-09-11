# Creditor list sourcing

Finds companies that have just lost money to an insolvency, and turns them into
a qualified trade credit insurance prospect list — every Monday at 10am.

A company that appears as an unsecured trade creditor in an external
administration has just taken a bad debt. That is the moment trade credit
insurance is easiest to sell. This repository automates finding those
companies, filtering them down to genuine prospects, and handing the sales team
a workbook.

```
ASIC statistics "Data set" ─┐
Worrells portal ────────────┴─► matters ─► 5604 watch ─► [ buy on ASIC ] ─┐
                                                                          │
                                                                          ▼
                                            creditors ─► qualify ─► enrich ─► workbook
```

Everything is automated except one step: **buying the Form 5604 document on
ASIC Connect.** That costs money per document and is not idempotent, so a human
does it from a one-page dashboard. See [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Quick start

```bash
pip install -r requirements.txt
export PYTHONPATH=src

python -m creditor_sourcing collect    # find new administrations
python -m creditor_sourcing watch      # check ASIC Connect for a lodged 5604
python -m creditor_sourcing ingest     # parse purchased / downloaded PDFs
python -m creditor_sourcing report     # qualify, enrich, build the workbook
python -m creditor_sourcing run        # all four
```

`run --respect-schedule` exits immediately unless it is the configured local
slot, which is how the weekly GitHub Actions job avoids firing twice across
daylight saving.

## What comes out

`out/NCI_Creditor_Prospects_<date>.xlsx`, five tabs:

| Tab | What it is |
|---|---|
| **Prospects** | Qualified companies, highest priority first. The call list. |
| **Repeat Exposure** | Companies bleeding across more than one insolvency. |
| **Purchase Queue** | ASIC documents to buy — the manual step. |
| **Excluded** | Everything dropped, with the reason, so the rules stay auditable. |
| **Matters** | Every administration being watched and its Form 5604 status. |

## Who gets excluded

Set in [`config/settings.yml`](config/settings.yml) and
[`config/exclusions.yml`](config/exclusions.yml):

- exposure under **$5,000** — too small to signal a real trade book
- **existing NCI clients** (fuzzy-matched against the PolicyList export)
- **non-trade creditors** — the ATO, employees, banks and financiers,
  landlords, utilities, insolvency firms, lawyers, insurers, related parties

A company **already in Pipedrive is kept**, not dropped. It is flagged so the
run adds a note to the existing organisation rather than creating a duplicate
lead for whoever already owns the relationship.

## Repository layout

```
src/creditor_sourcing/
  sources/     ASIC statistics data set, Published Notices, ASIC Connect, Worrells
  parse/       creditor tables out of Form 5604 / Initial Advice PDFs
  enrich/      PolicyList and Pipedrive cross-reference
  qualify.py   the exclusion rules and the priority score
  aggregate.py one prospect per company, across every matter
  workbook.py  the Excel deliverable
config/        settings and exclusion rules (edit these, not the code)
state/         committed run state - every change shows up in a diff
cloudflare/    the purchase-queue dashboard Worker
.github/       weekly run, CI, and the source-probe workflow
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — why it is built this way
- [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) — what each source does and does not give us
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — the weekly routine and how to set it up
