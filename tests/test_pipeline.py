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
from creditor_sourcing.models import Creditor, normalise_name
from creditor_sourcing.parse.creditor_tables import parse_lines
from creditor_sourcing.sources import asic_dataset
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
    """The "data set" sheet of ASIC's insolvency statistics workbook."""

    @staticmethod
    def workbook(header, rows, sheet_name="data set", preamble=True):
        import io

        from openpyxl import Workbook

        wb = Workbook()
        wb.remove(wb.active)
        wb.create_sheet("Notes")
        ws = wb.create_sheet(sheet_name)
        if preamble:
            ws.append(["ASIC Insolvency Statistics - Series 1 and Series 2"])
            ws.append([])
        ws.append(header)
        for row in rows:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    HEADER = ["Company Name", "ACN", "Appointment Type", "Date of Appointment",
              "Industry", "State"]
    ROWS = [
        ["Bust Co Pty Ltd", "123456789", "Creditors voluntary winding up",
         date(2026, 9, 3), "Construction", "NSW"],
        ["Collapsed Builders Pty Ltd", "987 654 321", "Court liquidation",
         date(2026, 9, 8), "Construction", "VIC"],
    ]

    def test_header_is_found_below_the_title_rows(self):
        matters = asic_dataset.parse(self.workbook(self.HEADER, self.ROWS), lookback_days=0)
        assert [m.company_name for m in matters] == [
            "Bust Co Pty Ltd", "Collapsed Builders Pty Ltd",
        ]

    def test_acn_is_normalised(self):
        matters = asic_dataset.parse(self.workbook(self.HEADER, self.ROWS), lookback_days=0)
        assert matters[1].acn == "987654321"

    def test_industry_and_state_are_carried(self):
        matter = asic_dataset.parse(self.workbook(self.HEADER, self.ROWS), lookback_days=0)[0]
        assert (matter.industry, matter.state) == ("Construction", "NSW")

    def test_alternate_header_spellings_still_map(self):
        header = ["Organisation Name", "A.C.N.", "Initial appointment type",
                  "Appointment date", "ANZSIC Division", "State/Territory"]
        matter = asic_dataset.parse(self.workbook(header, self.ROWS), lookback_days=0)[0]
        assert matter.company_name == "Bust Co Pty Ltd"
        assert matter.appointment_type == "Creditors voluntary winding up"

    def test_a_renamed_schema_raises_rather_than_returning_nothing(self):
        # "No insolvencies this week" and "the schema moved" must never look
        # the same, or a silent zero gets reported as a quiet week.
        payload = self.workbook(["Widget", "Sprocket", "Gizmo"], [["a", "b", "c"]])
        with pytest.raises(RuntimeError, match="company-name column"):
            asic_dataset.parse(payload, lookback_days=0)

    def test_missing_sheet_names_what_was_there(self):
        payload = self.workbook(self.HEADER, self.ROWS, sheet_name="Summary")
        with pytest.raises(RuntimeError, match="No data-set sheet"):
            asic_dataset.parse(payload, lookback_days=0)

    def test_lookback_filters_old_appointments(self):
        rows = self.ROWS + [["Ancient Pty Ltd", "111222333", "Administration",
                             date(2024, 1, 15), "Retail", "QLD"]]
        recent = asic_dataset.parse(self.workbook(self.HEADER, rows), lookback_days=30)
        assert "Ancient Pty Ltd" not in [m.company_name for m in recent]
        assert len(asic_dataset.parse(self.workbook(self.HEADER, rows), lookback_days=0)) == 3
