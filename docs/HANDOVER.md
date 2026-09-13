# Handover

Written for whoever picks this up next — most likely a Cowork session that can
reach IRIS. Read this before changing anything: several findings below were
expensive to get and look like oversights if you do not know the history.

Branch: `claude/trade-credit-creditor-sourcing-5bn617` · PR #1 · 49 commits ·
296 tests · CI green.

**Updated 13 September 2026 (Cowork session):** items 3.1 and 3.2 are done
and the exclusion vocabulary has been extended - see the notes in each
section. The qualified list is 82, down from 115; every drop is on the
Excluded tab with its reason.

---

## 1. What this is

Source companies that have just taken a bad debt in an external administration,
and turn them into a qualified trade credit insurance prospect list, weekly.

A creditor of an insolvent company has just lost money on credit terms it
extended. That is the buying trigger. A supplier that appears as a creditor in
*several* administrations is bleeding across its ledger, which is a stronger
trigger still, and is what the scoring rewards.

Two sources feed it. **Worrells** publishes creditor listings itself, free and
without a login. **ASIC** tells you for free that a Form 5604 exists; reading it
costs money per document, which is the one deliberately manual step.

---

## 2. What works today

The **Worrells half runs end to end, unattended**, and needs nothing from
anyone. Measured on the live data currently committed in `state/`:

| | |
|---|---|
| Administrations tracked | 1,955 (372 Worrells, 1,583 ASIC) |
| Creditor lists captured | 31 |
| Creditor rows | 452 |
| Stated exposure | $21,505,274 |
| Rows with an unstated amount (TBC) | 77 |
| Qualified prospects | **82** (115 before the 13 Sep fixes) |
| Excluded, with a reason | 287 |

Top of the current list: Melbourne Plaster Labour services ($716,213),
MBS Architectural ($667,865), Archiclad Pty Ltd ($484,312).

Why 287 are excluded:

| Count | Reason |
|---|---|
| 177 | Under the $5,000 floor |
| 34 | Secured lender or financier |
| 28 | Individual, not a business |
| 14 | Statutory / government creditor |
| 9 | Insolvency / legal / accounting adviser |
| 8 | Existing NCI client (PolicyList, 4 June 2026 export) |
| 6 | Landlord or utility |
| 5 | Insurer or broker |
| 2 | Employee / personal claim |
| 2 | Related party |
| 2 | Not a resolvable company name |

Also working: the ASIC statistics workbook feed (schema confirmed against the
7 September 2026 release), the PDF creditor-table parser, qualification and
scoring, and the Excel workbook.

---

## 3. What still needs to be completed

Ordered by what will hurt most if it ships as-is.

### 3.1 PolicyList — existing clients are not being excluded  ✅ done 13 Sep

It was real: against the 4 June 2026 export, **eight of the 115 prospects
were current NCI clients** - Mitre 10, Supapanel Australia, Fetch Personnel,
Americold Logistics, Home Timber & Hardware Group, Metal Manufactures, Acrow
Formwork and Scaffolding, and Studworks (STUDWORKS PROFILE SYSTEMS).

What changed: `config/policylist.csv` is the 4 June export stripped to policy
number, names, state and industry (no contacts) and is the default; the loader
reads .csv/.xls/.xlsx, finds the header on row 1 or 2, and folds **both**
`Policy Name` and `Client Name` (Mitre 10 and Home Timber only appear as policy
names under the client TOTAL TOOLS & HARDWARE GROUP); `match_name` gained a
distinctive-prefix rule for the Studworks case. 19 tests.

**Still yours:** refresh `config/policylist.csv` when you next export the
PolicyList (same five columns), or pass a fresh file with `--policylist`.

### 3.2 Pipedrive — the "note, not a drop" rule has never run  ✅ code done 13 Sep · 🟠 token still needed

The code had a correctness bug beyond never running: it took Pipedrive's
**first search hit unconditionally**. Live, "Melbourne Plaster Labour
services" returns Creative Plastering Group first - notes would have gone
onto the wrong organisations. Every candidate now has to pass the same
"same company?" test as the PolicyList, or carry the prospect's ABN/ACN in
its custom fields. Owner names are resolved (the search endpoint only returns
the owner id). `push_notes` is idempotent, so the weekly run does not re-post
the same note. Verified against 19 live candidate sets via the Pipedrive
connector: every match was the right organisation (Archiclad, Dahlsens,
Trumark, Crimsafe, Knauf Gypsum, Spicers, Moffat, Criterion Industries
head office). 14 tests.

**Still yours:** add `PIPEDRIVE_API_TOKEN` as an Actions secret. Run once
without `--push-notes` - `annotate()` now logs every match and the note it
would write - then enable `push_notes` on the workflow.

### 3.2a Non-trade exclusions leaked  ✅ done 13 Sep

Twenty-five qualified prospects on the 13 September list were financiers
(Shift, Lumi, OnDeck, Dynamoney, MoneyMe, Square, Procuret x2,
FlexiCommercial, Nissan Financial Services, Motorpass), insurers/brokers
(Gallagher Bassett x2, Trade Risk), advisers (KHI Partners, DLK Advisory,
Platinum Advisory Accountants, Bell Partners, "Mergers and Acquisitions"),
EnergyAustralia, a body corporate, the Building and Plumbing Commission, two
individuals the detector missed ("Phyllis (Meiping ) Yang", "William
Longhurst 002") and a bare "Pharmacy". `config/exclusions.yml` and
`looks_like_a_person` cover them; re-qualifying lost nothing legitimate.

### 3.3 ASIC Connect 5604 detection — broken, blocks the entire paid half  🔴

The 12 September run reported 744 creditors' voluntary liquidations checked
with zero Form 5604s found. **Every one of those requests had returned HTTP
404.** The failure was swallowed per company and the matter stamped as checked,
so "no 5604 exists" and "we never got an answer" were recorded identically.

That bug is fixed — `check()` now returns `(matter, reached)`, an unreached
matter is not stamped, and a run where most lookups fail logs an error saying
the results are *absent* rather than negative. **The underlying source is still
not reachable.**

What the probe found, Sunday 13 September:

- Every `connectonline.asic.gov.au` path — including the host root — returns
  the same asic.gov.au *"Service availability"* page, 79,981 bytes, zero forms.
- Byte-identical for the project's user agent and a browser's, so this is **not**
  a client being refused.
- Either a genuine ASIC Connect outage (it was Sunday morning Australian time)
  or a permanent host-wide redirect. **Not yet distinguished.**

**A reading to test first (added 13 Sep):** Daniel's earlier finding is that
ASIC Connect blocks datacenter IPs and rate-limits by IP - it is why his
Cloudflare Worker for 5604 search failed. A GitHub Actions runner is a
datacenter IP, so the "Service availability" page may be the block, not an
outage, and the weekday re-test is expected to fail the same way. If it does,
the working route is the desktop browser scrape loop from Cowork (unspaced
ACN, reset to `SearchRegisters.jspx` first, AdfPage fetchsize trick), run
weekly on the new CVLs only - well under 10 lookups/min, ~200 per session.

**To finish:** re-run `Diagnose sourcing legs → legs: connect` on a weekday
business morning. If ASIC answers, find the real organisation-details path and
fix `organisation_url()` and `find_form_5604()` against live markup. If it
still serves the availability page, the ASIC leg needs a different approach.

Until this works: the purchase queue is always empty, nothing is ever bought,
and `cmd_ingest` — the path that turns a purchased 5604 into creditor rows —
remains unexercised on a real document.

### 3.4 Cloudflare purchase dashboard — never executed  🟠

`cloudflare/src/index.ts` (277 lines) and `scripts/sync_inbox.py` (70 lines)
have **never run** and have no tests. That is the whole round trip: queue out
to the dashboard, bought PDFs back in. `state/inbox/` contains only a README.

**To finish:** deploy per `docs/RUNBOOK.md`, set `QUEUE_ENDPOINT` and
`QUEUE_TOKEN`, and prove one document through the loop. Blocked behind 3.3 in
practice, since there is nothing to queue.

### 3.5 `probe.yml` has never run  🟠

Zero runs. It is the workflow the runbook tells you to use for scraper
calibration, and it is unproven.

### 3.6 Dead code  🟡

`sources/asic_notices.py` — zero test references, and the Published Notices leg
is deliberately disabled. Delete it or mark it parked the way IRIS now is.

### 3.7 Setup only Daniel can do  🟠

- Actions secrets: `PIPEDRIVE_API_TOKEN`, `QUEUE_ENDPOINT`, `QUEUE_TOKEN`
- ~~A PolicyList export~~ done - refresh `config/policylist.csv` periodically
- Cloudflare deploy
- **Connectors on the weekly Routine** (`trig_017heozZUfRSDQ1PaJskTxKV`, fires
  Mon 10:00 Adelaide). This is the only channel that can *push* "N documents to
  buy" — everything else waits to be looked at. Must be attached in the
  claude.ai Routines UI; it cannot be done from a session.

---

## 4. The IRIS component — for Cowork

Parked in this build because it cannot work from here. **It can work from
Cowork**, and here is everything already established so none of it is redone.

### Why it cannot run from a cloud runner — settled, do not re-test

IRIS is **VPN-only, enforced at Cloudflare in front of the application**. From
a GitHub Actions runner, no credentials sent:

```
HTTP 403, 51 bytes, server: cloudflare, cf-ray: ...-DFW
body: "Please connect to the NCI VPN before accessing Iris"
```

Byte-identical for the project's user agent and a browser's — it is about where
the request comes from, not who is asking. The block sits in front of IRIS, so
**a session cookie cannot help** (the request never arrives to have a session)
and **neither can a headless browser in CI**.

### There is no per-debtor URL — settled, do not try to build one

IRIS is a single-page app; everything after the `#` is a client-side route that
never reaches the server:

```
.../index.html#oDashboard/oSelectDebtor/oSelectDebtorSearch-1644824
.../index.html#oDashboard/oSelectDebtor/oSelectDebtorSearch-1644824/oZoomDebtor1-1122
```

The second is the first *after* zooming into a debtor. `oSelectDebtorSearch-1644824`
is **byte-identical in both** and was already present before any debtor was
chosen, so it identifies a screen instance, not a company. Neither URL carries
an ABN, an ACN or a name.

Substituting a number would open whichever record that instance resolves to in
the viewer's own session — a rep would read one company's limit history under
another company's name. Worse than no link.

### What is already built for it

`sources/abn_lookup.py` resolves ACN → ABN, verified live:

```
SUELL EARTHMOVING PTY LTD      -> 50 683 236 259   checksum=valid
BREADROLL ENTERPRISES PTY LTD  -> 93 626 084 008   checksum=valid
```

It reads the ABN from the **page title** — `"Current details for ABN 50 683 236
259 | ABN Lookup"` — because the body table's `ABN` header cell holds the
*status* (`"Active from 19 Dec 2024"`), not the number. Every candidate is
checksum-verified against the ATO algorithm, so a page without a real ABN
yields nothing rather than digits that would send a rep to the wrong debtor.

The buy list (`ledger.queue_for_purchase`) already carries `abn`, and the
workbook's Purchase Queue tab shows it.

### The work itself

On a machine already on the NCI VPN, with an IRIS session:

1. For each purchase candidate, take the `abn` from the buy list.
2. Search it in the IRIS debtor search
   (`https://iris.nci.com.au/index.html#oDashboard/oSelectDebtor`).
3. Read the limit activity off the zoom screen.
4. Write it back onto the matter so it reaches the workbook beside the ASIC buy
   link. Suggested shape: `iris_activity` on the matter — whether NCI held
   limits on the debtor, how many clients, total limit value, most recent
   activity date.

**Open question for Daniel, never answered:** does prior NCI activity make him
*more* likely to buy that 5604, or less? Both readings are defensible — activity
means NCI knows the sector and has clients exposed; or it means those creditors
are already clients so the list yields fewer new prospects. **Surface it as a
neutral column until he says.** Do not build an auto-filter on it.

**Governance:** IRIS holds customer credit data. Pointing an agent at it in a
browser is worth a nod from whoever owns that system at NCI.

### If it is ever automated

Only two of three routes automate:

1. **An export** — debtor ABN plus limit activity, dropped in periodically, the
   same shape as the PolicyList input. Nothing to scrape, no credentials in CI,
   survives any IRIS UI change. Recommended.
2. **A self-hosted GitHub Actions runner on an NCI machine inside the VPN** —
   the weekly job does the lookups itself, unattended.
3. A browser on the VPN — works, but only when someone is at that machine.

---

## 5. Decisions already made — do not re-litigate

| Decision | Detail |
|---|---|
| Output | Excel now; direct to Pipedrive is V2 |
| Exposure floor | Drop under $5,000, **only where the amount is stated** |
| Existing clients | Drop on a PolicyList fuzzy match (threshold 92) |
| Non-trade creditors | Drop — statutory, employees, financiers, equipment rental, landlords, utilities, tolls, card issuers, advisers, insurers, related parties, trustee vehicles |
| Already in Pipedrive | **Keep** and flag for a note — never a drop, never a duplicate org |
| Individuals | **Drop.** Daniel's call. An individual creditor is an employee, a director loan or a private lender — no receivables ledger to insure |
| TBC exposures | **Keep and flag.** Daniel's call. 77 of 452 rows |
| Everything dropped | Ships on the Excluded tab with its reason, so judgement is visible not silent |
| IRIS | Parked for this build; picked up in Cowork |

---

## 6. Findings that must not be re-derived

Each of these cost a live investigation. They are the reason the code looks the
way it does.

**The creditor listing has two amount columns.**
`Name | Address | Related Party | ROCAP Amount | Identified Amount`. ROCAP comes
from the director's report and reads `$0.00` on almost every row; Identified is
the liquidator's figure. Reading the first amount cell put **80 of 445 creditors
at exactly $0.00** — the ATO at $0.00 on a page that says $370,993.81. A row now
reads up to two consecutive amount cells and takes the rightmost non-zero one.

**`$0.00` means unquantified, not zero.** A creditor owed nothing would not be
listed. Recording it as zero handed it to the $5,000 floor to be dropped as
"too small" — a false reason to lose a real prospect.

**PyMuPDF emits one cell per line for a ruled table.** A line-based parser
structurally cannot read a real listing; the only single-line "name plus amount"
text in these documents is narrative and fee labels. An early parser produced
five "creditors" named `Fees:` totalling $66,960 from a remuneration schedule.

**A page must qualify on table evidence, not a heading.** These reports run
20–40 pages and mention creditors constantly.

**A company is never its own creditor.** The running page header — `Report for
NAVIQ GROUP PTY LTD (Administrator Appointed)` — reached the qualified list owed
$747,812. Both parsers now reject a row naming the debtor.

**Fixing a parser does not remove bad rows.** A matter that re-parses to nothing
must have its rows cleared, or the fix lands and the artefact stays. NAVIQ
survived the fix that was meant to kill it.

**A failed lookup is not an answer.** See 3.3. Never stamp a matter as checked
when the request failed.

**The ASIC lookback must exceed the workbook's lag.** The newest appointment was
19 days old against a 14-day lookback, so `collect` returned **zero rows** — a
weekly run would have reported "no insolvencies" forever, looking healthy. Now
60 days, and the loader logs an error naming the lag if the window cannot reach
the newest date.

**ASIC stores ACNs as numbers, so leading zeros are gone.** `TANCRED BROTHERS` =
`25712`, really 000 025 712. Zero-pad to nine digits; a length check silently
drops every low-ACN company.

**Filter the workbook to Series 1.** Series 2 repeats a company per appointment
and double-counts its creditors. Members' voluntary liquidation is solvent and
never prospect material.

**Worrells needs no login,** and the documents page is
`/FileInformation/FileInformationDetailsView/<32-hex-id>`, not
`/FileInformation/FileInformation/<id>`.

**Page furniture only sits above the first row.** Filtering numeric cells from
every row ate the postcode off wrapped addresses — Commonwealth Bank's lost its
`2124`.

**A lower-cased brand is a real name.** `iCare Workers Insurance` was being
dropped as prose.

**Person-vs-business is one-sided by design.** A name counts as a person only if
it carries no business signal at all. `config/individuals.yml` holds the
vocabulary and a `keep` list — `Gross Waddell`, a real commercial property
agency, is indistinguishable from a person by name and lives there.

---

## 7. Repo map

```
config/settings.yml        all runtime config
config/exclusions.yml      non-trade creditor patterns, by category
config/individuals.yml     person-vs-business vocabulary + keep list
src/creditor_sourcing/
  cli.py                   collect | watch | ingest | report | run | probe | schema
  models.py                Matter, Creditor, Prospect, normalise_name
  ledger.py                committed JSON state; the buy list
  qualify.py               the rules; looks_like_a_person
  aggregate.py             fold creditors into prospects
  workbook.py              the Excel output
  parse/creditor_tables.py the PDF parser — read its docstrings first
  sources/                 asic_dataset, asic_connect, worrells, abn_lookup, http
  enrich/                  policylist, pipedrive
scripts/diagnose.py        live leg diagnostics — asic | connect | iris | worrells | survey | pdf-text
.github/workflows/         ci, weekly-sourcing, diagnose, probe, dataset-schema
state/                     committed run state (matters, creditors, prospects, queue)
docs/                      ARCHITECTURE, DATA_SOURCES, RUNBOOK, this file
```

**Run it:** Actions → *Weekly creditor sourcing* → Run workflow, or
`PYTHONPATH=src python -m creditor_sourcing run --policylist <path>`.

**Verify a claim about a live source:** Actions → *Diagnose sourcing legs* →
pick a leg. Read-only, spends no money, sends no credentials.

**Do not trust a source that returns zero rows.** It means the selectors need
recalibrating, not that there were no insolvencies that week. That failure mode
has bitten this project twice.
