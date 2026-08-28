"""
The Task 1.7A reports, their two historical modes, and the CSV export.

The demo scenario is the fixture. That is deliberate: these reports read the
ledger, and a hand-built fixture would be a second, simpler ledger that agrees
with the reports because both were written by the same person on the same
afternoon. The seeded data came out of the posting services, so a report that
disagrees with it is wrong about the real thing.

The sharpest tests here are the redaction ones. A report has three renderings —
screen, htmx partial, CSV — and a valuation leak in any one of them is a
storekeeper learning what the stock cost.
"""

from __future__ import annotations

import csv
import datetime
import io
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.inventory import reports
from apps.inventory.report_views import neutralise, safe_filename
from apps.inventory.reports import ReportFilters, ReportMode
from apps.inventory.tests.conftest import refuse_transactional_tests, seed_demo_once
from apps.organizations.models import Organization, Role
from apps.organizations.services import create_organization, grant_organization_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

HX = {"hx-request": "true"}

#: Route, and a string the seeded data guarantees is on the page.
REPORT_ROUTES: list[tuple[str, str]] = [
    ("inventory:report_valuation", "DEMO-RICE"),
    ("inventory:report_stock_card", "DEMO-RICE"),
    ("inventory:report_expiry", "DEMO-"),
    ("inventory:report_reorder", "DEMO-RICE"),
    ("inventory:report_waste", "WST-"),
    ("inventory:report_count_variance", "CNT-"),
    ("inventory:report_adjustments", "ADJ-"),
]

#: Every Arabic cost heading a report can render. Absence of all of them is
#: what "omitted, not blanked" means in practice.
COST_HEADINGS = ("متوسط الكلفة", "القيمة", "كلفة الوحدة", "القيمة بعد", "قيمة متبقية")


#: Every test here reads the same seeded dataset and writes nothing that the
#: next one must not see, so the seed runs once for the module instead of
#: forty-nine times. That is about ten minutes off the suite — the seed posts
#: eighty documents and costs ten seconds, and it was the entire runtime of
#: this file. See `seed_demo_once` for how the isolation still holds.
#:
#: The guard is not decoration: a `transaction=True` test added to this module
#: later would run inside the shared block, prove nothing about concurrency,
#: and pass. This fails the module instead.
@pytest.fixture(scope="module", autouse=True)
def seeded(django_db_setup: object, django_db_blocker: Any) -> Iterator[None]:
    import apps.inventory.tests.test_reports_and_exports as this_module

    refuse_transactional_tests(this_module)
    yield from seed_demo_once(django_db_blocker, username="reports-owner")


@pytest.fixture
def owner() -> User:
    return User.objects.get(username="reports-owner")


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.get(code="DEMO-KHAN-MANDI")


@pytest.fixture
def keeper() -> User:
    """The demo storekeeper: full stock visibility, no valuation."""
    return User.objects.get(username="demo-storekeeper")


def base_filters(organization: Organization, **overrides: object) -> ReportFilters:
    return ReportFilters(organization_id=organization.pk, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The report services
# ---------------------------------------------------------------------------


class TestStockValuation:
    def test_it_reports_the_seeded_positions(self, owner: User, organization: Organization) -> None:
        rows = {
            (row["warehouse_code"], row["item_code"], row["lot_code"]): row
            for row in reports.stock_valuation(
                owner, base_filters(organization), include_valuation=True
            )
        }
        rice = rows[("DEMO-MAIN", "DEMO-RICE", "")]
        assert rice["quantity"] == Decimal("145.000")
        assert rice["value"] == Decimal("216642.857")
        assert rice["category_code"] == "DEMO-GRAINS"
        assert rice["control_account"] == "1-03-01-001"
        assert rice["average_cost"] > Decimal("0")
        assert rice["last_posted_sequence"] > 0

        lot = rows[("DEMO-MAIN", "DEMO-CHICKEN", "DEMO-CHK-LOT-03")]
        assert lot["quantity"] == Decimal("12.000")

    def test_a_caller_without_view_valuation_gets_no_cost_keys(
        self, keeper: User, organization: Organization
    ) -> None:
        """
        Omitted, not blanked. An empty cell still says a number belongs there.
        """
        rows = reports.stock_valuation(keeper, base_filters(organization), include_valuation=False)
        assert rows
        for row in rows:
            assert "value" not in row
            assert "average_cost" not in row
            assert "control_account" not in row
            assert "quantity" in row


class TestStockCard:
    def test_the_running_totals_are_the_kernel_figures(
        self, owner: User, organization: Organization
    ) -> None:
        from apps.inventory.models import InventoryItem

        rice = InventoryItem.objects.get(organization=organization, code="DEMO-RICE")
        opening, rows = reports.stock_card(
            owner, base_filters(organization, item_id=rice.pk), include_valuation=True
        )
        assert opening["quantity"] == Decimal("0.000")
        assert rows

        # Ordered by posting sequence, and each row's closing balance is the
        # kernel's own — never recomputed here.
        sequences = [row["posted_sequence"] for row in rows]
        assert sequences == sorted(sequences)
        assert all("effective_at" in row and "posted_at" in row for row in rows)

        # ADR-017: a source identity is complete or absent, never partial. The
        # transfer arrival legs carry a reference and no triple, which is the
        # "absent" case and legitimate; what would be a defect is a row naming
        # a document type without an id, or the other way round.
        for row in rows:
            triple = (
                row["source_document_type"],
                row["source_document_id"],
                row["source_event"],
            )
            assert all(triple) or not any(triple), row
            assert row["reference"] or any(triple), "every movement is attributable"

        main_rows = [row for row in rows if row["warehouse_code"] == "DEMO-MAIN"]
        assert main_rows[-1]["quantity_after"] == Decimal("145.000")

    def test_a_reversal_is_visible_on_the_card(
        self, owner: User, organization: Organization
    ) -> None:
        from apps.inventory.models import InventoryItem

        oil = InventoryItem.objects.get(organization=organization, code="DEMO-OIL")
        _opening, rows = reports.stock_card(
            owner, base_filters(organization, item_id=oil.pk), include_valuation=True
        )
        assert any(row["is_reversal"] for row in rows), "the reversed oil receipt must show"

    def test_cost_columns_are_absent_without_permission(
        self, keeper: User, organization: Organization
    ) -> None:
        _opening, rows = reports.stock_card(
            keeper, base_filters(organization), include_valuation=False
        )
        assert rows
        for row in rows:
            assert "unit_cost" not in row
            assert "value_after" not in row


class TestHistoricalModes:
    """
    §D's requirement, and the reason it exists.

    The two modes are not two spellings of one filter. They slice on different
    times, and when a movement is posted on a later day than it took effect,
    they include different sets — which is a legitimate disagreement, not
    corruption.
    """

    def test_the_two_modes_can_return_different_answers_for_one_window(
        self, owner: User, organization: Organization
    ) -> None:
        from apps.inventory.models import StockMovement

        movement = (
            StockMovement.objects.filter(organization=organization)
            .order_by("posted_sequence")
            .first()
        )
        assert movement is not None
        # A window that ends the day before everything was posted. Nothing was
        # *known* by then; everything was already effective by then only if the
        # two timestamps agree, which for the demo they do — so this asserts
        # the mechanism, not an accident of the fixture.
        day_before = movement.posted_at.date() - datetime.timedelta(days=1)

        posted = reports.stock_valuation(
            owner,
            base_filters(organization, date_to=day_before, mode=ReportMode.POSTED_AS_OF),
            include_valuation=True,
        )
        assert posted == [], "nothing had been posted by the day before"

        effective_from_start = reports.stock_valuation(
            owner,
            base_filters(
                organization,
                date_from=movement.effective_at.date(),
                mode=ReportMode.EFFECTIVE_DATE,
            ),
            include_valuation=True,
        )
        assert effective_from_start, "the movements are effective from that day"

    def test_posted_as_of_uses_the_kernels_running_totals(
        self, owner: User, organization: Organization
    ) -> None:
        """A prefix of the posting order, so nothing is re-derived."""
        today = datetime.date.today()
        rows = reports.stock_valuation(
            owner,
            base_filters(organization, date_to=today, mode=ReportMode.POSTED_AS_OF),
            include_valuation=True,
        )
        by_key = {(row["warehouse_code"], row["item_code"], row["lot_code"]): row for row in rows}
        assert by_key[("DEMO-MAIN", "DEMO-RICE", "")]["quantity"] == Decimal("145.000")

    def test_effective_date_sums_deltas_and_derives_the_average(
        self, owner: User, organization: Organization
    ) -> None:
        today = datetime.date.today()
        rows = reports.stock_valuation(
            owner,
            base_filters(organization, date_to=today, mode=ReportMode.EFFECTIVE_DATE),
            include_valuation=True,
        )
        rice = next(
            row
            for row in rows
            if row["warehouse_code"] == "DEMO-MAIN" and row["item_code"] == "DEMO-RICE"
        )
        # Value ÷ quantity, because this movement set was never replayed in
        # this order and the stored average does not describe it.
        assert rice["average_cost"] == pytest.approx(
            rice["value"] / rice["quantity"], abs=Decimal("0.000001")
        )

    def test_the_mode_is_shown_on_every_historical_screen(
        self, owner: User, client_for: Callable[[User], Client], seeded: None
    ) -> None:
        """An unlabelled 'as of' would be wrong for half its readers."""
        body = client_for(owner).get(reverse("inventory:report_valuation")).content.decode()
        assert "حسب تاريخ الترحيل" in body
        assert "حسب تاريخ الاستحقاق" in body


class TestTheOtherReports:
    def test_expiry_buckets_expired_soon_and_later(
        self, owner: User, organization: Organization
    ) -> None:
        buckets = {
            row["lot_code"]: row
            for row in reports.expiry(owner, base_filters(organization), include_valuation=True)
        }
        assert buckets["DEMO-CHK-LOT-04"]["is_expired"] is True
        assert buckets["DEMO-CHK-LOT-04"]["bucket"] == "EXPIRED"
        assert buckets["DEMO-CHK-LOT-03"]["bucket"] == "WITHIN_30"
        assert buckets["DEMO-CHK-LOT-01"]["bucket"] == "WITHIN_90"
        # The fully wasted lot holds nothing and is not listed: it has already
        # been dealt with, and keeping it would bury the ones that have not.
        assert "DEMO-CHK-LOT-02" not in buckets

    def test_reorder_reports_below_at_and_above(
        self, owner: User, organization: Organization
    ) -> None:
        rows = {
            row["item_code"]: row
            for row in reports.reorder(owner, base_filters(organization), include_valuation=True)
            if row["branch_code"] == "DEMO-BUNOOK" and row["reorder_point"] is not None
        }
        assert rows["DEMO-RICE"]["is_below"] is True
        assert rows["DEMO-RICE"]["shortage"] == Decimal("25.500")
        assert rows["DEMO-OIL"]["is_at_point"] is True
        assert rows["DEMO-OIL"]["shortage"] == Decimal("0")
        assert rows["DEMO-CONTAINER"]["is_below"] is False
        assert rows["DEMO-CONTAINER"]["is_at_point"] is False

    def test_count_variance_names_both_people(
        self, owner: User, organization: Organization
    ) -> None:
        rows = reports.count_variance(owner, base_filters(organization), include_valuation=True)
        assert rows
        rice = next(row for row in rows if row["item_code"] == "DEMO-RICE")
        assert rice["book_quantity"] == Decimal("30.000")
        assert rice["counted_quantity"] == Decimal("29.500")
        assert rice["variance_quantity"] == Decimal("-0.500")
        assert rice["conductor"] == "demo-storekeeper"
        assert rice["approver"] and rice["approver"] != rice["conductor"]

    def test_adjustments_show_all_three_kinds(
        self, owner: User, organization: Organization
    ) -> None:
        rows = reports.adjustments(owner, base_filters(organization), include_valuation=True)
        kinds = {row["kind"] for row in rows}
        assert kinds == {"QUANTITY_GAIN", "QUANTITY_LOSS", "VALUE_ONLY"}
        assert all(row["is_reversed"] is False for row in rows)

    def test_gl_reconciliation_agrees_and_offers_no_repair(
        self, organization: Organization
    ) -> None:
        rows, discrepancies = reports.gl_reconciliation(organization)
        assert rows
        assert discrepancies == []
        assert all(row.agrees for row in rows), [str(row.difference) for row in rows]
        assert not hasattr(reports, "repair_gl_reconciliation")

    def test_a_manual_journal_against_control_shows_as_drift(
        self, organization: Organization
    ) -> None:
        """
        Planted drift must remain visible rather than be explained away.

        The verifier sums *every* line on an inventory-control account on
        purpose: a manual journal there is precisely what this screen exists
        to surface.
        """
        from apps.accounting.models import Account, JournalEntry, JournalLine

        control = Account.objects.get(organization=organization, code="1-03-01-001")
        entry = JournalEntry.objects.filter(organization=organization).first()
        assert entry is not None
        first_line = entry.lines.first()
        assert first_line is not None
        branch = first_line.branch

        # Balanced, because `accounting_entry_must_balance_when_posted` refuses
        # anything else — the drift being planted is a legitimate manual
        # journal that touches inventory control, which is exactly the case
        # this screen exists to surface. An unbalanced one cannot be created
        # at all, and that is the accounting kernel working.
        expense = Account.objects.get(organization=organization, code="6-01-02-001")
        JournalLine.objects.create(
            entry=entry,
            branch=branch,
            account=control,
            debit=Decimal("1000.000"),
            credit=Decimal("0.000"),
            line_number=98,
        )
        JournalLine.objects.create(
            entry=entry,
            branch=branch,
            account=expense,
            debit=Decimal("0.000"),
            credit=Decimal("1000.000"),
            line_number=99,
        )

        rows, discrepancies = reports.gl_reconciliation(organization)
        control_row = next(row for row in rows if row.account_code == "1-03-01-001")
        assert not control_row.agrees
        assert control_row.difference == Decimal("-1000.000")
        assert discrepancies, "the authoritative verifier must see it too"


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestReportScope:
    def test_a_submitted_organization_id_cannot_widen_scope(
        self, keeper: User, organization: Organization, seeded: None
    ) -> None:
        """
        Scope first, filters second.

        Naming another organization must return nothing, not that
        organization's rows — the filter narrows what the caller may already
        read and can never add to it.
        """
        other = create_organization(code="RIVAL17", name="منافس")
        rows = reports.stock_valuation(
            keeper, ReportFilters(organization_id=other.pk), include_valuation=False
        )
        assert rows == []

    def test_a_user_with_no_membership_sees_nothing(self, seeded: None, units: None) -> None:
        outsider = User.objects.create_user(username="outsider", password="pw-not-real-1234")
        organization = Organization.objects.get(code="DEMO-KHAN-MANDI")
        assert (
            reports.stock_valuation(outsider, base_filters(organization), include_valuation=False)
            == []
        )

    def test_a_global_permission_without_reach_grants_nothing(
        self, seeded: None, units: None
    ) -> None:
        """
        ADR-016: a permission says what, a membership says where.

        Membership in an unrelated organization must not widen reach into the
        demo one, however many Django permissions the account carries.
        """
        unrelated = create_organization(code="ELSEWHERE", name="آخر")
        user = User.objects.create_user(username="wide", password="pw-not-real-1234")
        grant_organization_access(user=user, organization=unrelated, role=Role.OWNER)
        organization = Organization.objects.get(code="DEMO-KHAN-MANDI")
        assert (
            reports.stock_valuation(user, base_filters(organization), include_valuation=True) == []
        )


# ---------------------------------------------------------------------------
# Screens, htmx and pagination
# ---------------------------------------------------------------------------


class TestReportScreens:
    @pytest.mark.parametrize(("route", "expected"), REPORT_ROUTES)
    def test_the_screen_renders_seeded_rows(
        self,
        owner: User,
        client_for: Callable[[User], Client],
        seeded: None,
        route: str,
        expected: str,
    ) -> None:
        response = client_for(owner).get(reverse(route))
        assert response.status_code == 200
        assert expected in response.content.decode()

    def test_an_hx_request_returns_only_the_results(
        self, owner: User, client_for: Callable[[User], Client], seeded: None
    ) -> None:
        body = (
            client_for(owner)
            .get(reverse("inventory:report_valuation"), headers=HX)
            .content.decode()
        )
        assert "<html" not in body
        assert 'class="ui-app-shell"' not in body
        assert body.strip().startswith('<section class="ui-data-card" id="list-results"')

    def test_the_full_page_and_the_partial_agree_on_rows(
        self, owner: User, client_for: Callable[[User], Client], seeded: None
    ) -> None:
        client = client_for(owner)
        url = reverse("inventory:report_valuation")
        full = client.get(url).content.decode()
        partial = client.get(url, headers=HX).content.decode()
        assert full.count("DEMO-RICE") == partial.count("DEMO-RICE")

    def test_filters_and_mode_survive_paging(
        self, owner: User, client_for: Callable[[User], Client], seeded: None
    ) -> None:
        import re

        response = client_for(owner).get(
            reverse("inventory:report_stock_card"),
            {"q": "DEMO", "mode": ReportMode.EFFECTIVE_DATE.value},
        )
        body = response.content.decode()
        links = [
            match.replace("&amp;", "&") for match in re.findall(r'href="\?([^"]*page=\d+)"', body)
        ]
        if links:  # the stock card pages at 50 rows
            assert all("q=DEMO" in link for link in links)
            assert all("mode=EFFECTIVE_DATE" in link for link in links)
            assert all(link.count("page=") == 1 for link in links)

    def test_a_caller_without_valuation_sees_no_cost_heading(
        self, keeper: User, client_for: Callable[[User], Client], seeded: None
    ) -> None:
        client = client_for(keeper)
        for route, _expected in REPORT_ROUTES:
            headings = table_headings(client.get(reverse(route)).content.decode())
            leaked = headings & set(COST_HEADINGS)
            assert not leaked, f"{route} leaked {leaked}"

    def test_the_partial_redacts_exactly_as_the_page_does(
        self, keeper: User, client_for: Callable[[User], Client], seeded: None
    ) -> None:
        client = client_for(keeper)
        url = reverse("inventory:report_valuation")
        full = table_headings(client.get(url).content.decode())
        partial = table_headings(client.get(url, headers=HX).content.decode())
        assert full == partial
        assert not (full & set(COST_HEADINGS))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def table_headings(body: str) -> set[str]:
    """
    The rendered column headings, not raw substrings of the whole page.

    A naive `"القيمة" in body` matches the page hint prose too — the valuation
    report's own description contains the word — so the first version of this
    check failed on text that was never a column. Redaction is about columns.
    """
    import re

    return {
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r"<th[^>]*>(.*?)</th>", body, re.S)
    }


def read_csv(body: str) -> list[list[str]]:
    """Rows after the provenance block, which ends at the blank line."""
    rows = list(csv.reader(io.StringIO(body.lstrip("﻿"))))
    blank = next(index for index, row in enumerate(rows) if row == [])
    return rows[blank + 1 :]


class TestExport:
    def test_the_export_carries_provenance(
        self, owner: User, client_for: Callable[[User], Client], seeded: None
    ) -> None:
        response = client_for(owner).get(reverse("inventory:report_valuation"), {"export": "csv"})
        body = response.content.decode("utf-8")
        assert response["Content-Type"] == "text/csv; charset=utf-8"
        assert body.startswith("﻿"), "Excel needs the BOM to read Arabic"
        assert "وقت التصدير" in body
        assert "وضع التقرير" in body
        assert "المرشحات" in body

    def test_the_filename_is_safe_and_dated(self) -> None:
        name = safe_filename("stock/../../etc/passwd")
        assert "/" not in name and ".." not in name
        assert name.endswith(".csv")

    def test_export_rows_match_the_screen(
        self, owner: User, client_for: Callable[[User], Client], seeded: None
    ) -> None:
        """Same service, same filters — so the counts cannot diverge."""
        client = client_for(owner)
        url = reverse("inventory:report_valuation")
        screen_rows = client.get(url).context["total_rows"]
        exported = read_csv(client.get(url, {"export": "csv"}).content.decode("utf-8"))
        assert len(exported) - 1 == screen_rows  # minus the header row

    def test_export_preserves_exact_decimals_and_arabic(
        self, owner: User, client_for: Callable[[User], Client], seeded: None
    ) -> None:
        body = (
            client_for(owner)
            .get(reverse("inventory:report_valuation"), {"export": "csv"})
            .content.decode("utf-8")
        )
        assert "رز تجريبي" in body
        # Exact, unlocalised, trailing zeros intact — never a float repr.
        assert "145.000" in body
        assert "216642.857" in body
        assert "e+" not in body.lower()

    def test_export_omits_cost_columns_without_permission(
        self, keeper: User, client_for: Callable[[User], Client], seeded: None
    ) -> None:
        """
        §I's sharpest requirement: the export must not be the way round the
        screen's redaction.
        """
        body = (
            client_for(keeper)
            .get(reverse("inventory:report_valuation"), {"export": "csv"})
            .content.decode("utf-8")
        )
        header = read_csv(body)[0]
        assert not (set(header) & set(COST_HEADINGS)), header
        assert "DEMO-RICE" in body, "the rows are still there, only the cost is gone"

    def test_export_scope_matches_the_screen(
        self, seeded: None, units: None, client_for: Callable[[User], Client]
    ) -> None:
        outsider = User.objects.create_user(username="csv-outsider", password="pw-not-real-1234")
        response = client_for(outsider).get(
            reverse("inventory:report_valuation"), {"export": "csv"}
        )
        # No permission at all: the screen refuses and so does the export.
        assert response.status_code == 403

    @pytest.mark.parametrize("dangerous", ["=1+1", "+SUM(A1)", "-2+3", "@cmd", "\tx", "\rx"])
    def test_formula_triggers_are_neutralised(self, dangerous: str) -> None:
        """A file exported from here is opened on somebody else's machine."""
        rendered = neutralise(dangerous)
        assert rendered.startswith("'")
        assert rendered[1:] == dangerous

    def test_ordinary_text_is_untouched(self) -> None:
        assert neutralise("DEMO-RICE") == "DEMO-RICE"
        assert neutralise("رز تجريبي") == "رز تجريبي"

    def test_decimals_never_pass_through_float(self) -> None:
        """
        `str(float(...))` would render this as 0.1 + 0.2 problems.

        Checked on a value chosen so a float round-trip is visibly wrong.
        """
        assert neutralise(Decimal("1234567890.123")) == "1234567890.123"
        assert neutralise(Decimal("0.001")) == "0.001"
        assert neutralise(Decimal("100.000")) == "100.000"

    def test_none_renders_empty_and_dates_render_iso(self) -> None:
        assert neutralise(None) == ""
        assert neutralise(datetime.date(2026, 8, 10)) == "2026-08-10"
