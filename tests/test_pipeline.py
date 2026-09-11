"""Tests for the rules that decide what reaches the sales team.

The scrapers are not tested here - they run against live sites and are
calibrated by `probe`. What is tested is everything that turns raw creditor
rows into a prospect list, because that is where a silent bug publishes a
wrong name, a wrong amount, or an existing client as a new lead.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from creditor_sourcing import aggregate, qualify
from creditor_sourcing.models import Creditor, Matter, normalise_name
from creditor_sourcing.parse import creditor_tables
from creditor_sourcing.parse.creditor_tables import parse_lines
from creditor_sourcing.sources import asic_dataset, worrells
from creditor_sourcing.sources.worrells import details_view_url


def creditor(name, debtor="Bust Co Pty Ltd", matter="m1", amount=50000.0, **kw):
    return Creditor(name, debtor, matter, amount, **kw)


class TestNormalisation:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("Acme Building Supplies Pty Ltd", "ACME BUILDING SUPPLIES"),
            ("Riverside Timber & Hardware", "Riverside Timber and Hardware Pty Ltd"),
            # Corporate-family words fold too - "Acme Group" and "Acme Australia"
            # are the same customer relationship for prospecting purposes.
            ("The Steel Group Australia Limited", "Steel  Pty. Ltd."),
        ],
    )
    def test_variants_fold_together(self, a, b):
        assert normalise_name(a) == normalise_name(b)

    def test_distinct_companies_stay_distinct(self):
        assert normalise_name("Acme Timber") != normalise_name("Acme Steel")


class TestAggregation:
    def test_same_company_across_matters_is_one_prospect(self):
        prospects = aggregate.build(
            [
                creditor("Acme Pty Ltd", "Bust Co", "m1", 42000.0),
                creditor("ACME", "Other Bust", "m2", 18000.0),
            ]
        )
        assert len(prospects) == 1
        assert prospects[0].matter_count == 2
        assert prospects[0].total_exposure_aud == 60000.0

    def test_related_parties_never_become_prospects(self):
        assert aggregate.build([creditor("Smith Family Trust", related_party=True)]) == []


class TestQualification:
    def test_under_the_floor_is_dropped(self):
        [p] = qualify.apply(aggregate.build([creditor("Tiny Widgets Pty Ltd", amount=4999.0)]))
        assert not p.qualified and "under the" in p.disqualified_reason

    def test_at_the_floor_is_kept(self):
        [p] = qualify.apply(aggregate.build([creditor("Small Widgets Pty Ltd", amount=5000.0)]))
        assert p.qualified

    @pytest.mark.parametrize(
        "name",
        [
            "Australian Taxation Office",
            "Westpac Banking Corporation",
            "Worrells Solvency & Forensic Accountants",
            "QBE Insurance (Australia) Limited",
            "Smith & Co Lawyers",
            "Employee Entitlements",
        ],
    )
    def test_non_trade_creditors_are_dropped(self, name):
        [p] = qualify.apply(aggregate.build([creditor(name, amount=250000.0)]))
        assert not p.qualified, f"{name} should not be a prospect"

    def test_ordinary_supplier_survives(self):
        [p] = qualify.apply(aggregate.build([creditor("Riverside Timber Supplies Pty Ltd")]))
        assert p.qualified

    def test_existing_client_is_dropped_despite_spelling_drift(self):
        [p] = qualify.apply(
            aggregate.build([creditor("Riverside Timber & Hardware Pty Ltd", amount=250000.0)]),
            {"riverside timber hardware": "Riverside Timber and Hardware Pty Ltd"},
        )
        assert not p.qualified and "Existing NCI client" in p.disqualified_reason

    def test_word_boundaries_stop_false_positives(self):
        # "agl" is an excluded utility; "Eagle" must not match it.
        [p] = qualify.apply(aggregate.build([creditor("Eagle Fasteners Pty Ltd")]))
        assert p.qualified


class TestScoring:
    def test_repeat_exposure_outranks_a_bigger_single_debt(self):
        repeat = aggregate.build(
            [creditor("Repeat Co", "A", "m1", 30000.0), creditor("Repeat Co", "B", "m2", 30000.0)]
        )[0]
        once = aggregate.build([creditor("Single Co", "A", "m1", 400000.0)])[0]
        assert qualify.score(repeat) > qualify.score(once)

    def test_score_is_bounded(self):
        huge = aggregate.build(
            [creditor("Big Co", f"D{i}", f"m{i}", 5_000_000.0) for i in range(10)]
        )[0]
        assert 0 <= qualify.score(huge) <= 100


class TestCreditorTableParsing:
    LINES = [
        "Listing of known creditors",
        "Name Address Related Party Amount",
        "Acme Building Supplies Pty Ltd 12 Industry Rd Dandenong VIC No 42,500.00",
        "Smith Family Trust 3 Hill St Toorak VIC Yes 60,000.00",
        "Contents ................................ 4",
        "Total 230,500.00",
        "Page 7",
    ]

    def test_reads_real_rows_only(self):
        rows = parse_lines(self.LINES, "Bust Co", "m1", "asic")
        assert [r.creditor_name for r in rows] == [
            "Acme Building Supplies Pty Ltd",
            "Smith Family Trust",
        ]

    def test_amount_and_address_split(self):
        row = parse_lines(self.LINES, "Bust Co", "m1", "asic")[0]
        assert row.amount_aud == 42500.0
        assert row.address == "12 Industry Rd Dandenong VIC"

    def test_related_party_flag(self):
        assert parse_lines(self.LINES, "Bust Co", "m1", "asic")[1].related_party

    def test_contents_lines_are_never_creditors(self):
        rows = parse_lines(["Schedule of debts .......... 12"], "Bust Co", "m1", "asic")
        assert rows == []


class TestWorrellsDetails:
    """The File Details panel on a DetailsView page.

    DETAIL_HTML is the verbatim visible text of the live page for
    MAYDE ELECTRICAL PTY LTD, captured by the diagnose workflow. The panel is
    label/value pairs in inline elements, not a table.
    """

    DETAIL_HTML = (
        "<html><body>File Information MAYDE ELECTRICAL PTY LTD File Details "
        "ACN/Estate#: 168 364 298 Office Name: North Lakes Trading Name None known "
        "Principal Lee Crosthwaite Admin Type Creditors Vol Manager: Broderick Dipple "
        "Start Date 11/09/2026 Exec Analyst Broderick Dipple Status Priority "
        "Contact Person Joseph Eckersley Industry Building/Construction "
        "Appointee Andrew Worrell I want to Lodge a Proof of Debt</body></html>"
    )

    def test_values_stop_at_the_next_panel_label(self):
        # Without every panel label as a boundary - including the ones we do
        # not map - Start Date swallowed the rest of the panel.
        details = worrells.parse_details(self.DETAIL_HTML)
        assert details["appointment_date"] == "11/09/2026"
        assert details["appointment_type"] == "Creditors Vol"
        assert details["office"] == "North Lakes"

    def test_acn_is_extracted_and_padded(self):
        # The ACN is what lets a Worrells matter reconcile against the same
        # company in the ASIC workbook.
        matter = worrells.apply_details(
            Matter(source="worrells", company_name="MAYDE ELECTRICAL PTY LTD"),
            self.DETAIL_HTML,
        )
        assert matter.acn == "168364298"

    def test_start_date_becomes_an_iso_date(self):
        matter = worrells.apply_details(
            Matter(source="worrells", company_name="X"), self.DETAIL_HTML)
        assert matter.appointment_date == "2026-09-11"

    def test_industry_and_appointee_are_captured(self):
        matter = worrells.apply_details(
            Matter(source="worrells", company_name="X"), self.DETAIL_HTML)
        assert matter.industry == "Building/Construction"
        assert matter.practitioner == "Andrew Worrell"
        assert matter.practitioner_firm == "Worrells"

    def test_placeholder_values_are_dropped(self):
        assert "trading_name" not in worrells.parse_details(self.DETAIL_HTML)

    def test_existing_values_are_never_overwritten(self):
        matter = Matter(source="worrells", company_name="X", acn="999888777",
                        industry="Manufacturing")
        worrells.apply_details(matter, self.DETAIL_HTML)
        assert matter.acn == "999888777"
        assert matter.industry == "Manufacturing"

    def test_a_bare_page_yields_nothing_rather_than_junk(self):
        assert worrells.parse_details("<html><body>Nothing here</body></html>") == {}


class TestWorrellsDocuments:
    """Document links on a DetailsView page.

    Confirmed live: most matters carry no documents (they are days old), while
    a matured one such as Valera Recycling Pty Ltd carries "First Advice" and
    "2nd Advice" - both creditor listings, downloadable unauthenticated.
    """

    HTML = """<html><body>
      <a href="/">home</a>
      <a href="/FileInformation">File Information</a>
      <a href="/WebDocuments/12345/first-advice.pdf">First Advice</a>
      <a href="/WebDocuments/12345/2nd-advice.pdf">2nd Advice</a>
      <a href="/WebDocuments/12345/remuneration.pdf">Remuneration Report</a>
      <a href="/Privacy">Privacy</a>
    </body></html>"""

    BASE = "https://customerportal.worrells.net.au"

    def test_only_pdfs_are_returned(self):
        docs = worrells.parse_documents(self.HTML, self.BASE)
        assert len(docs) == 3
        assert all(d["url"].endswith(".pdf") for d in docs)

    def test_creditor_documents_rank_first(self):
        names = [d["name"] for d in worrells.parse_documents(self.HTML, self.BASE)]
        assert names[:2] == ["First Advice", "2nd Advice"]

    def test_creditor_documents_excludes_other_reports(self):
        names = [d["name"] for d in worrells.creditor_documents(self.HTML, self.BASE)]
        assert names == ["First Advice", "2nd Advice"]

    def test_urls_are_absolute(self):
        doc = worrells.creditor_documents(self.HTML, self.BASE)[0]
        assert doc["url"] == f"{self.BASE}/WebDocuments/12345/first-advice.pdf"

    def test_a_new_matter_with_no_documents_is_not_an_error(self):
        bare = """<html><body><a href="/">h</a><a href="/Privacy">p</a></body></html>"""
        assert worrells.parse_documents(bare, self.BASE) == []


class TestWorrellsUrl:
    LIST_URL = (
        "https://customerportal.worrells.net.au/FileInformation/FileInformation/"
        "202502200334422382761DFEC99D43BE"
    )
    VIEW_URL = (
        "https://customerportal.worrells.net.au/FileInformation/"
        "FileInformationDetailsView/202502200334422382761DFEC99D43BE"
    )

    def test_list_url_is_rewritten_to_the_documents_page(self):
        assert details_view_url(self.LIST_URL) == self.VIEW_URL

    def test_already_correct_url_is_unchanged(self):
        assert details_view_url(self.VIEW_URL) == self.VIEW_URL

    def test_bare_id_is_accepted(self):
        assert details_view_url("202502200334422382761DFEC99D43BE") == self.VIEW_URL

    def test_garbage_is_rejected(self):
        with pytest.raises(ValueError):
            details_view_url("https://customerportal.worrells.net.au/FileInformation")


class TestAsicDataSet:
    """The "Data set" sheet of ASIC's insolvency statistics workbook.

    HEADER and the sheet shape below are ASIC's real ones, captured from the
    published workbook by the "ASIC data set schema" workflow: five blank/title
    rows, a merged group-label row, then the header on row 7.
    """

    # Exactly as published, trailing spaces and embedded newlines included.
    HEADER = [
        "Data to", "ACN No ", "Organisation name ", "Appointee (person or company)",
        "Appointment type", "Effective date", "Period\n(Year month)",
        "Period (financial year)", "Industry type (division)",
        "Industry type (subdivision)", "Industry type (group)",
        "State of incorporation (state or territory)",
        "Principal place of business (state or territory)",
        "Principal place of business (area)",
        "Principal place of business (postcode)",
        "Series 1 \n(companies entering)", "Series 2 \n(all appointments)",
    ]

    @staticmethod
    def row(name, acn, appointment, when, series_1=1, industry="Construction",
            subdivision="Building Construction", state="Victoria", postcode=3000):
        return [None, acn, name, "JONES, MICHAEL GREGORY", appointment, when,
                "2026 09", "2026-2027", industry, subdivision, "Group",
                "Victoria", state, "Melbourne - West", postcode, series_1, 1]

    ROWS = [
        row.__func__("TANCRED BROTHERS PTY LTD", 25712, "Court liquidation",
                     date(2026, 9, 3), industry="Retail Trade",
                     subdivision="Food Retailing", state="Queensland", postcode=4305),
        row.__func__("JINDONG HOLDINGS PTY LTD", 612927974,
                     "Creditors' voluntary liquidation", date(2026, 9, 8)),
    ]

    @classmethod
    def workbook(cls, header=None, rows=None, sheet_name="Data set"):
        import io

        from openpyxl import Workbook

        wb = Workbook()
        wb.remove(wb.active)
        wb.create_sheet("Contents")
        wb.create_sheet("1.1")
        ws = wb.create_sheet(sheet_name)
        for _ in range(5):
            ws.append([])
        ws.append([None, "Organisation details", None, "Appointee", "Role"])
        ws.append(header or cls.HEADER)
        for row in rows if rows is not None else cls.ROWS:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def parse(self, **kw):
        return asic_dataset.parse(self.workbook(**kw), lookback_days=30)

    def test_header_is_found_on_row_seven(self):
        assert [m.company_name for m in self.parse()] == [
            "TANCRED BROTHERS PTY LTD", "JINDONG HOLDINGS PTY LTD",
        ]

    def test_low_acn_is_zero_padded_not_dropped(self):
        # ACNs are stored as numbers, so 000 025 712 arrives as 25712.
        # Dropping short values loses every early-registered company.
        assert self.parse()[0].acn == "000025712"

    def test_normal_acn_is_unchanged(self):
        assert self.parse()[1].acn == "612927974"

    def test_industry_and_location_are_captured(self):
        matter = self.parse()[0]
        assert (matter.industry, matter.industry_subdivision) == (
            "Retail Trade", "Food Retailing",
        )
        assert (matter.state, matter.postcode) == ("Queensland", "4305")

    def test_practitioner_is_captured(self):
        assert self.parse()[0].practitioner == "JONES, MICHAEL GREGORY"

    def test_repeat_appointments_are_excluded(self):
        # A Series 2 row without the Series 1 flag is a subsequent appointment
        # for a company already counted - it would duplicate the matter.
        rows = [*self.ROWS, self.row("JINDONG HOLDINGS PTY LTD", 612927974,
                                     "Court liquidation", date(2026, 9, 9),
                                     series_1=None)]
        assert len(self.parse(rows=rows)) == 2

    def test_solvent_wind_ups_are_excluded(self):
        # Members' voluntary liquidation is solvent - nobody lost money.
        rows = [*self.ROWS, self.row("RICH OWNERS PTY LTD", 111222333,
                                     "Members' voluntary liquidation",
                                     date(2026, 9, 5))]
        names = [m.company_name for m in self.parse(rows=rows)]
        assert "RICH OWNERS PTY LTD" not in names

    def test_lookback_filters_the_history(self):
        # The real sheet holds 70,000+ appointments back to July 2021.
        rows = [*self.ROWS, self.row("ANCIENT PTY LTD", 999888777,
                                     "Court liquidation", date(2021, 7, 1))]
        assert len(self.parse(rows=rows)) == 2
        assert len(asic_dataset.parse(self.workbook(rows=rows), lookback_days=0)) == 3

    def test_a_renamed_schema_raises_rather_than_returning_nothing(self):
        # "No insolvencies this week" and "the schema moved" must never look
        # the same, or a silent zero gets reported as a quiet week.
        with pytest.raises(RuntimeError, match="company-name column"):
            self.parse(header=["Widget", "Sprocket", "Gizmo"],
                       rows=[["a", "b", "c"]])

    def test_missing_sheet_names_what_was_there(self):
        with pytest.raises(RuntimeError, match="No data-set sheet"):
            self.parse(sheet_name="Summary")

    def test_stale_file_reports_the_lag_instead_of_looking_like_a_quiet_week(self, caplog):
        # Measured on the real 7 Sep 2026 release: the newest appointment was
        # 19 days old, so a 14-day window returned zero and the weekly run
        # would have reported "no insolvencies" rather than "file is stale".
        import logging

        old = date.today().replace(year=date.today().year - 1).isoformat()
        rows = [self.row("STALE CO PTY LTD", 123456789, "Court liquidation", old)]
        with caplog.at_level(logging.ERROR):
            assert asic_dataset.parse(self.workbook(rows=rows), lookback_days=14) == []
        assert "newest appointment in the file" in caplog.text
        assert "lookback window is only 14 days" in caplog.text


class TestCreditorTableGate:
    """Which pages count as a creditor table.

    These are regression tests for a real failure: on live Worrells reports the
    parser returned five "creditors" all named "Fees:" totalling $66,960,
    harvested from the practitioner's remuneration schedule. The reports run
    20-40 pages and mention creditors throughout, so a heading match locates
    narrative far more often than a table.
    """

    # Verbatim shape of a remuneration page from RFCVIC PTY LTD's report:
    # narrative prose with one inline amount, no Related Party column.
    REMUNERATION_PAGE = "\n".join([
        "NOTICE OF PROPOSAL TO CREDITORS",
        "RFCVIC PTY LTD (In Liquidation)",
        "Proposal 2: Future fee approval",
        "That the remuneration of the Liquidator from 10 September 2026 to the",
        "finalisation of the liquidation is determined at a sum equal to the costs",
        "report to creditors of 10 September 2026 in the amount of $31,500.00",
        "Liquidator's remuneration is paid in priority to unsecured creditors",
        "You may vote Yes, No or Object to the proposal being resolved",
        "Fees: 3,405.70",
        "Fees: 11,000.00",
    ])

    # A real creditor listing: the Related Party column gives standalone
    # Yes/No cells, and amounts sit in their own cells.
    LISTING_PAGE = "\n".join([
        "Listing of known creditors",
        "Name", "Address", "Related Party", "Amount",
        "Acme Building Supplies Pty Ltd", "12 Industry Rd Dandenong VIC",
        "No", "42,500.00",
        "Riverside Timber Pty Ltd", "8 Kembla St Wollongong NSW",
        "No", "128,000.00",
    ])

    def test_a_remuneration_page_is_not_a_creditor_table(self):
        assert not creditor_tables.is_creditor_table(self.REMUNERATION_PAGE)

    def test_a_listing_page_is_a_creditor_table(self):
        assert creditor_tables.is_creditor_table(self.LISTING_PAGE)

    def test_narrative_yes_no_does_not_qualify_a_page(self):
        # "You may vote Yes, No or Object" must not count as the Related Party
        # column - only a cell that is nothing but Yes or No does.
        page = "\n".join(["You may vote Yes, No or Object", "1,000.00", "2,000.00"])
        assert not creditor_tables.is_creditor_table(page)

    @pytest.mark.parametrize(
        "line",
        [
            "Fees: 3,405.70",
            "Fees 11,000.00",
            "GST 1,100.00",
            "Total 230,500.00",
            "Remuneration 31,500.00",
            "Disbursements 265.84",
            "Estimated surplus 5,000.00",
        ],
    )
    def test_fee_and_position_labels_are_never_creditors(self, line):
        rows = creditor_tables.parse_lines([line], "Bust Co", "m1", "worrells")
        assert rows == [], f"{line!r} must not become a creditor"

    def test_a_sentence_ending_in_an_amount_is_not_a_creditor(self):
        line = ("report to creditors of 10 September 2026 in the amount of "
                "31,500.00")
        assert creditor_tables.parse_lines([line], "Bust Co", "m1", "worrells") == []

    def test_a_real_supplier_row_still_parses(self):
        line = "Acme Building Supplies Pty Ltd 12 Industry Rd Dandenong VIC No 42,500.00"
        rows = creditor_tables.parse_lines([line], "Bust Co", "m1", "worrells")
        assert len(rows) == 1
        assert rows[0].creditor_name == "Acme Building Supplies Pty Ltd"
        assert rows[0].amount_aud == 42500.0


class TestCellPerLineListing:
    """PyMuPDF emits one cell per line for a ruled table.

    This is the layout a real creditor listing arrives in, and the original
    line-based parser could not read it at all - which is why the only rows it
    ever produced came from narrative and fee lines.
    """

    PAGE = "\n".join([
        "Annexure C",
        "Listing of known creditors",
        "Name", "Address", "Related Party", "Amount",
        "Acme Building Supplies Pty Ltd", "12 Industry Rd Dandenong VIC",
        "No", "42,500.00",
        "Smith Family Trust", "3 Hill St Toorak VIC", "Yes", "60,000.00",
        "Riverside Timber Pty Ltd", "8 Kembla St Wollongong NSW",
        "No", "128,000.00",
    ])

    def rows(self):
        return creditor_tables.parse_cells(
            self.PAGE.splitlines(), "Bust Co Pty Ltd", "m1", "worrells")

    def test_every_row_is_read(self):
        assert [r.creditor_name for r in self.rows()] == [
            "Acme Building Supplies Pty Ltd",
            "Smith Family Trust",
            "Riverside Timber Pty Ltd",
        ]

    def test_the_section_heading_is_not_a_creditor(self):
        assert "Listing of known creditors" not in [
            r.creditor_name for r in self.rows()]

    def test_the_annexure_label_is_not_a_creditor(self):
        assert "Annexure C" not in [r.creditor_name for r in self.rows()]

    def test_column_headers_are_not_creditors(self):
        names = [r.creditor_name for r in self.rows()]
        assert not {"Name", "Address", "Related Party", "Amount"} & set(names)

    def test_amounts_and_addresses_pair_with_the_right_name(self):
        rows = {r.creditor_name: r for r in self.rows()}
        acme = rows["Acme Building Supplies Pty Ltd"]
        assert acme.amount_aud == 42500.0
        assert acme.address == "12 Industry Rd Dandenong VIC"
        assert rows["Riverside Timber Pty Ltd"].amount_aud == 128000.0

    def test_related_party_flag_follows_the_yes_no_cell(self):
        rows = {r.creditor_name: r for r in self.rows()}
        assert rows["Smith Family Trust"].related_party is True
        assert rows["Acme Building Supplies Pty Ltd"].related_party is False

    def test_an_extra_creditor_type_column_does_not_shift_the_data(self):
        # Some practitioners add a Creditor Type column; anchoring on the
        # Yes/No cell rather than on position has to survive that.
        page = "\n".join([
            "Listing of known creditors",
            "Name", "Address", "Related Party", "Creditor Type", "Amount",
            "Acme Building Supplies Pty Ltd", "12 Industry Rd Dandenong VIC",
            "No", "42,500.00",
        ])
        rows = creditor_tables.parse_cells(
            page.splitlines(), "Bust Co", "m1", "worrells")
        assert len(rows) == 1
        assert rows[0].creditor_name == "Acme Building Supplies Pty Ltd"
        assert rows[0].amount_aud == 42500.0
