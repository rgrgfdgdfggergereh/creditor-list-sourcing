"""Command line entry point for every stage of the weekly run.

    python -m creditor_sourcing collect    # find new administrations
    python -m creditor_sourcing watch      # check ASIC Connect for Form 5604
    python -m creditor_sourcing ingest     # parse purchased/downloaded PDFs
    python -m creditor_sourcing report     # qualify, enrich, build the workbook
    python -m creditor_sourcing run        # collect + watch + ingest + report
    python -m creditor_sourcing probe      # dump live HTML for calibration

Each stage is separately runnable so a failure in one source does not cost the
whole week's run, and so `ingest` can be re-run on its own after the manual
ASIC purchase without re-scraping anything.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import aggregate, config, ledger, qualify, workbook
from .enrich import pipedrive, policylist
from .models import Creditor, Matter
from .parse.creditor_tables import extract_pdf
from .sources import asic_connect, asic_dataset, asic_notices, worrells
from .sources.http import Client

log = logging.getLogger("creditor_sourcing")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def check_schedule() -> bool:
    """True if it is the scheduled local hour.

    GitHub Actions cron is UTC only and the business timezone shifts with
    daylight saving, so the workflow schedules every candidate UTC hour and
    this gate decides whether the run is the real one.
    """
    cfg = config.settings()["schedule"]
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    return now.hour == cfg["local_hour"] and now.isoweekday() == cfg["weekday"]


def cmd_collect(args: argparse.Namespace) -> int:
    known = ledger.load_matters()
    found: list[Matter] = []
    sources = config.settings()["sources"]

    # The statistics workbook's "data set" sheet is the primary named-company
    # feed: one structured download instead of hundreds of scraped pages.
    if sources["asic_stats"]["enabled"] and "asic" in args.sources:
        try:
            found.extend(asic_dataset.collect())
        except Exception as exc:  # noqa: BLE001
            log.error("ASIC data set collection failed: %s", exc)

    # Published Notices corroborates and fills gaps - it publishes within days
    # of an appointment, while the workbook is republished monthly.
    if sources["asic_notices"]["enabled"] and "asic-notices" in args.sources:
        try:
            found.extend(asic_notices.collect())
        except Exception as exc:  # noqa: BLE001
            log.error("ASIC notices collection failed: %s", exc)

    if sources["worrells"]["enabled"] and "worrells" in args.sources:
        try:
            found.extend(worrells.collect())
        except Exception as exc:  # noqa: BLE001
            log.error("Worrells collection failed: %s", exc)

    known, new = ledger.merge(known, found)
    ledger.save_matters(known)
    log.info("Collect: %d found, %d new, %d tracked in total",
             len(found), new, len(known))
    return 0


# Harvest statuses that mean the document was fetched and read to a definite
# conclusion, so whatever rows a previous run stored for that matter are now
# known to be wrong. "ok" replaces them; "no-section" and "table-unreadable"
# clear them. Every other status - failed, scanned, no-documents - means we
# did not get a reading, and the rows already held are the best data there is.
CONCLUSIVE = ("ok", "no-section", "table-unreadable")


def _append_creditors(
    path: Path, creditors: list[Creditor], reparsed: set[str] | None = None,
) -> None:
    """Add creditor rows to the running JSON, replacing any for the same matter.

    Replacing rather than appending means re-harvesting a matter (after a
    practitioner lodges a fuller document, say) corrects the data instead of
    duplicating every creditor.

    `reparsed` carries the matters that were read to a conclusion this run,
    including those that yielded nothing. Without it a re-parse that correctly
    drops every row for a matter leaves the old rows in place: the fix to the
    parser lands, and the bad creditor stays in the workbook. That is exactly
    what happened to "Report for NAVIQ GROUP PTY LTD (Administrator
    Appointed)", which survived the run that stopped the parser producing it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(path.read_text()) if path.exists() else []
    touched = {c.matter_id for c in creditors} | (reparsed or set())
    kept = [row for row in existing if row.get("matter_id") not in touched]
    kept.extend(c.to_dict() for c in creditors)
    path.write_text(json.dumps(kept, indent=2, ensure_ascii=False) + "\n")


def cmd_watch(args: argparse.Namespace) -> int:
    """Advance every open matter, by whichever route its source allows.

    The two sources need opposite handling and it matters which is which:

      Worrells publishes its creditor listing itself, so an open matter is
      harvested outright - fetch the detail page, download the report, read
      the creditors. Free, and no human in the loop.

      ASIC only tells us for free THAT a Form 5604 exists. Reading it means
      buying it, so those matters go on the purchase queue for a human.

    Treating them alike would either pay for creditor lists Worrells gives
    away, or wait forever for a purchase that was never needed.
    """
    known = ledger.load_matters()
    outstanding = ledger.open_matters(known)
    worrells_open = [m for m in outstanding if m.get("source") == "worrells"]
    asic_open = [m for m in outstanding if m.get("source") != "worrells"]
    log.info("Watch: %d open matters (%d Worrells, %d ASIC)",
             len(outstanding), len(worrells_open), len(asic_open))

    settings = config.settings()
    client = Client()
    creditors: list[Creditor] = []
    reparsed: set[str] = set()
    harvested = no_documents = 0

    # --- Worrells: harvest the published listing directly -------------------
    cap = args.limit or settings["sources"]["worrells"].get("max_matters_per_run", 60)
    for record in worrells_open[:cap]:
        rows, status, updates = worrells.harvest(record, client)
        record.update(updates)
        record["last_checked"] = date.today().isoformat()
        record["document_status"] = status
        if status in CONCLUSIVE:
            reparsed.add(record["matter_id"])
        if status == "ok":
            # Carry the debtor's sector onto every creditor row. Without this
            # the Worrells leg loses the industry entirely, and "Debtor
            # industries" - the column that tells a rep a timber supplier's
            # bad debts all came from construction - comes through blank.
            for creditor in rows:
                creditor.debtor_industry = record.get("industry")
                creditor.debtor_state = record.get("state")
            creditors.extend(rows)
            record["creditors_captured"] = True
            harvested += 1
        elif status == "no-documents":
            no_documents += 1

    # --- ASIC: free detection now, paid purchase later ----------------------
    # Capped and prioritised: the open set is ~1,600 matters and grows weekly,
    # so an uncapped pass would eventually outlast the job timeout while
    # spending most of its requests on appointment types that never produce a
    # 5604. check_order puts the likely ones and the least recently checked
    # first, so the cap rotates rather than starves.
    asic_cap = args.limit or settings["sources"]["asic_connect"].get(
        "max_matters_per_run", 150
    )
    asic_checked = 0
    for record in asic_connect.check_order(asic_open)[:asic_cap]:
        matter = Matter(
            source=record["source"],
            company_name=record["company_name"],
            acn=record.get("acn"),
        )
        matter = asic_connect.check(matter, client)
        record["last_checked"] = matter.last_checked
        if matter.form_5604_lodged:
            record["form_5604_lodged"] = True
            record["form_5604_date"] = matter.form_5604_date
            record["form_5604_doc_number"] = matter.form_5604_doc_number
        asic_checked += 1

    if creditors or reparsed:
        _append_creditors(Path(args.out), creditors, reparsed)

    ledger.save_matters(known)
    queue = ledger.queue_for_purchase(list(known.values()))
    ledger.save_queue(queue)
    log.info(
        "Watch: %d Worrells matters harvested (%d creditors), %d not lodged yet; "
        "%d of %d open ASIC matters checked; %d documents queued for purchase",
        harvested, len(creditors), no_documents,
        asic_checked, len(asic_open), len(queue),
    )
    if asic_checked < len(asic_open):
        log.info(
            "ASIC backlog: %d matters not reached this run - they sort first "
            "next week, so nothing is dropped",
            len(asic_open) - asic_checked,
        )
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Parse creditor PDFs sitting in the inbox into creditor rows.

    Drop purchased Form 5604 PDFs (and any downloaded Worrells Initial Advice)
    into state/inbox/. Name each file <matter_id>__<company name>.pdf, or pass
    --matter-id for a single file.
    """
    known = ledger.load_matters()
    inbox = Path(args.inbox)
    if not inbox.exists():
        log.error("Inbox %s does not exist", inbox)
        return 1

    creditors: list[Creditor] = []
    review: list[dict[str, str]] = []

    for pdf in sorted(inbox.glob("*.pdf")):
        matter_id = args.matter_id or pdf.stem.split("__")[0]
        record = known.get(matter_id)
        if not record:
            log.warning("%s: no matter %s in state - skipping", pdf.name, matter_id)
            review.append({"file": pdf.name, "status": "unknown-matter"})
            continue

        rows, status = extract_pdf(
            pdf, record["company_name"], matter_id, record.get("source", "asic")
        )
        if status == "ok":
            for creditor in rows:
                creditor.debtor_industry = record.get("industry")
                creditor.debtor_state = record.get("state")
            creditors.extend(rows)
            record["creditors_captured"] = True
            record["form_5604_purchased"] = True
        else:
            review.append({"file": pdf.name, "status": status,
                           "company": record["company_name"]})
            log.warning("%s: %s", pdf.name, status)

    ledger.save_matters(known)
    ledger.save_queue(ledger.queue_for_purchase(list(known.values())))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(out.read_text()) if out.exists() else []
    existing.extend(c.to_dict() for c in creditors)
    out.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")

    log.info("Ingest: %d creditors from %d documents, %d need manual review",
             len(creditors), len(list(inbox.glob("*.pdf"))) - len(review), len(review))
    if review:
        for item in review:
            log.info("  review: %s (%s)", item["file"], item["status"])
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    settings = config.settings()
    creditors_path = Path(args.creditors)
    if not creditors_path.exists():
        log.error("No creditor data at %s - run ingest first", creditors_path)
        return 1

    raw = json.loads(creditors_path.read_text())
    creditors = [
        Creditor(
            creditor_name=r["creditor_name"], debtor_company=r["debtor_company"],
            matter_id=r["matter_id"], amount_aud=r.get("amount_aud", 0.0),
            amount_known=r.get("amount_known", True),
            address=r.get("address"), related_party=r.get("related_party", False),
            creditor_type=r.get("creditor_type"), source=r.get("source", "asic"),
            source_document=r.get("source_document"),
            debtor_industry=r.get("debtor_industry"),
            debtor_state=r.get("debtor_state"),
        )
        for r in raw
    ]

    policy_keys = policylist.load(Path(args.policylist)) if args.policylist else {}
    prospects = qualify.apply(aggregate.build(creditors), policy_keys)

    if not args.no_crm:
        pipedrive.annotate([p for p in prospects if p.qualified])

    if args.push_notes:
        in_crm = [p for p in prospects if p.qualified and p.pipedrive_org_id]
        pipedrive.push_notes(in_crm)

    ledger.save_prospects([p.to_dict() for p in prospects])
    path = workbook.build(
        prospects, ledger.load_queue(), ledger.load_matters(),
        Path(args.out_dir or settings["output"]["directory"]),
        settings["output"]["workbook_prefix"],
    )
    print(path)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Save live HTML from a source so its parser can be calibrated."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = Client()

    if args.source == "asic-notices":
        (out / "asic_notices.html").write_text(asic_notices.probe(client))
    elif args.source == "asic-connect":
        if not args.acn:
            log.error("--acn is required to probe ASIC Connect")
            return 1
        (out / f"asic_connect_{args.acn}.html").write_text(
            asic_connect.probe(args.acn, client)
        )
    elif args.source == "worrells":
        cfg = config.settings()["sources"]["worrells"]
        (out / "worrells_new_appointments.html").write_text(
            client.get(cfg["base_url"] + cfg["list_path"]).text
        )
    log.info("Probe written to %s", out)
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    """Download the ASIC workbook and print its shape.

    This is how the data set sheet's real column names get discovered from an
    environment with no access to download.asic.gov.au: run it in CI and read
    the job log.
    """
    url = args.url or asic_dataset.resolve_latest_url()
    payload = asic_dataset.download(url)
    print(f"Source: {url}")
    print(f"Size:   {len(payload):,} bytes\n")
    print(asic_dataset.describe(payload))

    if args.save:
        target = asic_dataset.save(payload, Path(args.save))
        print(f"\nSaved workbook to {target}")
    try:
        matters = asic_dataset.parse(payload, lookback_days=0)
        print(f"\nParsed {len(matters)} appointments in total.")
        for matter in matters[:5]:
            print(f"  {matter.company_name} | ACN {matter.acn} | "
                  f"{matter.appointment_type} | {matter.appointment_date} | "
                  f"{matter.industry} | {matter.state}")
    except RuntimeError as exc:
        print(f"\nPARSE FAILED: {exc}")
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.respect_schedule and not check_schedule():
        cfg = config.settings()["schedule"]
        now = datetime.now(ZoneInfo(cfg["timezone"]))
        log.info("Not the scheduled slot (%s local, want %02d:00 on weekday %d) - exiting",
                 now.strftime("%a %H:%M"), cfg["local_hour"], cfg["weekday"])
        return 0
    for stage in (cmd_collect, cmd_watch, cmd_ingest, cmd_report):
        code = stage(args)
        if code:
            return code
    return 0


def build_parser() -> argparse.ArgumentParser:
    # -v is accepted both before and after the subcommand. argparse puts
    # top-level flags before it, which is easy to get wrong in a shell script
    # and fails the whole run with "unrecognized arguments" - it broke two
    # workflows before anyone ran them. Declaring it on a parent parser makes
    # both spellings work.
    # SUPPRESS matters here: with an ordinary default the subparser writes
    # its own False over a -v that was given before the subcommand, so
    # "-v collect" would parse as quiet. Suppressed, whichever parser actually
    # saw the flag is the one that sets it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
    )

    parser = argparse.ArgumentParser(prog="creditor_sourcing", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--sources", default="asic,worrells",
                       help="comma separated: asic,asic-notices,worrells")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--inbox", default=str(config.STATE_DIR / "inbox"))
        p.add_argument("--matter-id")
        p.add_argument("--out", default=str(config.STATE_DIR / "creditors.json"))
        p.add_argument("--creditors", default=str(config.STATE_DIR / "creditors.json"))
        p.add_argument("--policylist", default="")
        p.add_argument("--out-dir", default="")
        p.add_argument("--no-crm", action="store_true")
        p.add_argument("--push-notes", action="store_true")

    for name, handler in (
        ("collect", cmd_collect), ("watch", cmd_watch),
        ("ingest", cmd_ingest), ("report", cmd_report), ("run", cmd_run),
    ):
        p = sub.add_parser(name, parents=[common])
        add_common(p)
        if name == "run":
            p.add_argument("--respect-schedule", action="store_true")
        p.set_defaults(handler=handler)

    schema = sub.add_parser("schema", parents=[common])
    schema.add_argument("--url", default="", help="override the workbook URL")
    schema.add_argument("--save", default="", help="also save the .xlsx here")
    schema.set_defaults(handler=cmd_schema)

    probe = sub.add_parser("probe", parents=[common])
    probe.add_argument("source", choices=["asic-notices", "asic-connect", "worrells"])
    probe.add_argument("--acn")
    probe.add_argument("--out", default="out/probe")
    probe.set_defaults(handler=cmd_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    if hasattr(args, "sources"):
        args.sources = [s.strip() for s in args.sources.split(",")]
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
