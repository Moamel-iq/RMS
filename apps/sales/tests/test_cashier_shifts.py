"""
The cashier closing and the daily reconciliation, end to end.

Everything asserted here is a claim ADR-027 §8 makes and that would be expensive
to discover was false:

* a closing posts **one thing** — the approved cash over/short variance — and
  the tests that matter most are the negative ones: no `SALES_REVENUE` line, no
  `SALES_CARD_CLEARING` line, no `ApplicationReceivableEntry`, and the day's
  takings not posted a second time;
* both journal directions, and the zero-variance case that legitimately posts
  no journal at all while still taking a number;
* maker-checker enforced twice — in the service *and* at the database, where a
  raw `update()` that walks past the service must still raise;
* a close refused against a draft day, because an expectation derived from a
  draft is a target that can move after the count;
* `APPLICATION_RECEIVABLE` refused as a tender count, by a check constraint;
* the frozen `CLOSED` figures;
* the cashier's 403 on approve, and the outsider's 404.

The fixtures reuse the shape `test_daily_sales_posting.py` and
`test_sales_adjustments.py` established, plus the `SALES_CASH_OVER_SHORT`
account and mapping this journal needs.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import Client
from django.utils import timezone

from apps.accounting.models import (
    DELIVERY_APP_RECEIVABLE,
    DELIVERY_COMMISSION_EXPENSE,
    SALES_CARD_CLEARING,
    SALES_CASH_ON_HAND,
    SALES_CASH_OVER_SHORT,
    SALES_DISCOUNT,
    SALES_RETURNS,
    SALES_REVENUE,
    Account,
    AccountingSettings,
    AccountRole,
    CostCenter,
    JournalEntry,
    JournalLine,
)
from apps.accounting.services import create_account, create_account_mapping, open_fiscal_year
from apps.core.automation import process_due_events
from apps.core.models import AutomationException, AutomationTask
from apps.kitchen.models import Recipe, RecipeServing, RecipeVersion
from apps.organizations.models import Branch, Organization
from apps.sales.adjustment_posting import post_sales_adjustment
from apps.sales.adjustment_services import add_adjustment_line, create_sales_adjustment
from apps.sales.daily_close_services import (
    approve_daily_financial_close,
    submit_daily_financial_close,
)
from apps.sales.daily_reconciliation import ADVISORY, COVERAGE_LIMITATION, reconcile_day
from apps.sales.day_services import (
    add_sales_line,
    create_sales_day,
    set_tender_summary,
    submit_sales_day,
)
from apps.sales.models import (
    ApplicationReceivableEntry,
    CashierShift,
    CashierShiftStatus,
    CashierTenderCount,
    DailyFinancialCloseStatus,
    MenuItem,
    SalesAdjustmentReasonKind,
    SalesChannel,
    SalesChannelCategory,
    SalesDay,
    SalesDayStatus,
    TenderDestination,
)
from apps.sales.posting import post_sales_day
from apps.sales.services import (
    create_menu_item,
    create_menu_price,
    create_sales_channel,
    set_branch_availability,
)
from apps.sales.shift_posting import (
    SOURCE_DOCUMENT_TYPE,
    approve_cashier_shift,
    reverse_cashier_shift,
)
from apps.sales.shift_services import (
    close_cashier_shift,
    expected_by_tender,
    expected_cash_for,
    open_cashier_shift,
    reopen_cashier_shift,
    set_tender_count,
)
from apps.users.models import User

BUSINESS_DATE = datetime.date(2026, 8, 10)
JANUARY = datetime.date(2026, 1, 1)

#: The chart a closing reaches. `7-09-05-002` — cash over and short — is the one
#: this checkpoint adds: a till difference is neither revenue nor an expense of
#: selling, it is a difference, and giving it its own account is what makes a
#: month of small shortages visible as a pattern rather than as noise inside
#: something else.
_CHART: tuple[tuple[str, str, str], ...] = (
    ("1", "الأصول", "Assets"),
    ("1-01", "النقد", "Cash"),
    ("1-01-01", "الصناديق", "Cash boxes"),
    ("1-01-01-001", "الصندوق", "Cash"),
    ("1-02", "الذمم", "Receivables"),
    ("1-02-01", "ذمم التطبيقات", "App receivables"),
    ("1-02-01-001", "ذمم تطبيقات التوصيل", "App Receivable"),
    ("1-03", "مقاصة البطاقات", "Card clearing"),
    ("1-03-01", "مقاصة", "Clearing"),
    ("1-03-01-001", "مقاصة البطاقات", "Card Clearing"),
    ("4", "الإيرادات", "Revenue"),
    ("4-01", "المبيعات", "Sales"),
    ("4-01-01", "مبيعات مباشرة", "Direct"),
    ("4-01-01-001", "مبيعات الصالة", "Dine-in Sales"),
    ("4-02", "خصومات المبيعات", "Discounts"),
    ("4-02-01", "خصومات المطعم", "Restaurant discounts"),
    ("4-02-01-001", "خصومات المبيعات", "Sales Discount"),
    # Added for the same-day refund case: an adjustment credits the drawer
    # account, so a drawer expectation that ignores it charges the difference
    # to `SALES_CASH_OVER_SHORT` as a shortage nobody was short of.
    ("4-03", "المردودات", "Returns"),
    ("4-03-01", "مردودات المبيعات", "Sales returns"),
    ("4-03-01-001", "مردودات وإلغاءات المبيعات", "Sales Returns"),
    ("6", "المصروفات", "Expenses"),
    ("6-03", "مصروفات البيع", "Selling"),
    ("6-03-01", "عمولات التطبيقات", "App commissions"),
    ("6-03-01-001", "عمولات التوصيل", "Commission"),
    ("7", "الفروقات", "Differences"),
    ("7-09", "فروقات التشغيل", "Operating differences"),
    ("7-09-05", "فروقات الصندوق", "Cash differences"),
    ("7-09-05-002", "فروقات الصندوق", "Cash over and short"),
)


@pytest.fixture
def chart(
    organization: Organization,
    hall_cost_center: CostCenter,
    delivery_cost_center: CostCenter,
) -> dict[str, str]:
    for code, name, name in _CHART:
        create_account(organization=organization, code=code, name=name)
    mappings = {
        SALES_REVENUE: "4-01-01-001",
        SALES_DISCOUNT: "4-02-01-001",
        SALES_RETURNS: "4-03-01-001",
        SALES_CASH_ON_HAND: "1-01-01-001",
        SALES_CARD_CLEARING: "1-03-01-001",
        DELIVERY_APP_RECEIVABLE: "1-02-01-001",
        DELIVERY_COMMISSION_EXPENSE: "6-03-01-001",
        SALES_CASH_OVER_SHORT: "7-09-05-002",
    }
    for role, code in mappings.items():
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=role),
            account=Account.objects.get(organization=organization, code=code),
            effective_from=JANUARY,
        )
    open_fiscal_year(organization=organization, year=BUSINESS_DATE.year)
    return mappings


@pytest.fixture
def recipe_setup(organization: Organization) -> tuple[Recipe, RecipeVersion, RecipeServing]:
    """A recipe with one active version and a WHOLE serving worth a tenth of a batch."""
    from apps.kitchen.models import ApprovalEvidenceKind, RecipeType, RecipeVersionStatus
    from apps.units.models import UnitOfMeasure

    unit = UnitOfMeasure.objects.filter(code="KG").first() or UnitOfMeasure.objects.create(
        code="KG",
        name="كيلوغرام",
        dimension="MASS",
        factor_to_base=Decimal("1"),
        is_base=True,
    )
    recipe = Recipe.objects.create(
        organization=organization,
        code="DEMO-MANDI",
        name="مندي",
        recipe_type=RecipeType.PORTION,
    )
    preparer = User.objects.create_user(username="recipe-preparer", password="pw-not-real-1234")
    approver = User.objects.create_user(username="recipe-approver", password="pw-not-real-1234")
    version = RecipeVersion.objects.create(
        recipe=recipe,
        version_number=1,
        output_unit=unit,
        expected_output_quantity=Decimal("10.000000"),
        status=RecipeVersionStatus.DRAFT,
    )
    RecipeServing.objects.create(
        version=version,
        code="WHOLE",
        name="حبة كاملة",
        serving_quantity=Decimal("1.000000"),
        serving_unit=unit,
        base_quantity=Decimal("1.000000"),
        factor_of_batch=Decimal("0.100000000000"),
        is_primary=True,
    )
    now = timezone.now()
    rows = RecipeVersion.objects.filter(pk=version.pk)
    rows.update(status=RecipeVersionStatus.SUBMITTED, submitted_by=preparer, submitted_at=now)
    rows.update(
        status=RecipeVersionStatus.APPROVED,
        approved_by=approver,
        approved_at=now,
        approval_reference="اعتماد تجريبي",
        approval_evidence_kind=ApprovalEvidenceKind.DEMO_FICTIONAL,
    )
    rows.update(
        status=RecipeVersionStatus.ACTIVE,
        activated_by=approver,
        activated_at=now,
        effective_from=JANUARY,
    )
    version.refresh_from_db()
    serving = version.servings.get(code="WHOLE")
    return recipe, version, serving


@pytest.fixture
def menu_item(
    organization: Organization,
    branch: Branch,
    recipe_setup: tuple[Recipe, RecipeVersion, RecipeServing],
) -> MenuItem:
    recipe, _version, _serving = recipe_setup
    item = create_menu_item(
        organization=organization,
        code="MENU-MANDI",
        name="مندي",
        recipe=recipe,
        serving_code="WHOLE",
    )
    set_branch_availability(item=item, branch=branch)
    create_menu_price(
        menu_item=item, branch=branch, unit_price=Decimal("10000"), effective_from=JANUARY
    )
    return item


@pytest.fixture
def hall(organization: Organization, hall_cost_center: CostCenter) -> SalesChannel:
    return create_sales_channel(
        organization=organization,
        code="DINE-IN",
        name="الصالة",
        category=SalesChannelCategory.DINE_IN,
        cost_center=hall_cost_center,
        default_tender=TenderDestination.CASH,
    )


@pytest.fixture
def card_channel(organization: Organization, hall_cost_center: CostCenter) -> SalesChannel:
    return create_sales_channel(
        organization=organization,
        code="CARD",
        name="بطاقة",
        category=SalesChannelCategory.TAKEAWAY,
        cost_center=hall_cost_center,
        default_tender=TenderDestination.CARD,
    )


@pytest.fixture
def cash_day(
    chart: dict[str, str],
    organization: Organization,
    branch: Branch,
    menu_item: MenuItem,
    hall: SalesChannel,
    manager: User,
    accounting_manager: User,
) -> SalesDay:
    """
    Two plates at 10,000 through the hall: 20,000 of cash, posted.

    The declaration is entered **before** the day is submitted, because a
    tender summary is a draft-only edit — which is itself the reason المطابقة
    اليومية compares it against the lines rather than trusting it: both were
    typed by the same person, on the same screen, before anything posted.
    """
    day = create_sales_day(
        organization=organization, branch=branch, business_date=BUSINESS_DATE, actor=manager
    )
    add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("2.000"))
    set_tender_summary(day=day, tender=TenderDestination.CASH, declared_amount=Decimal("20000"))
    submit_sales_day(day=day, actor=manager)
    return post_sales_day(day=day, actor=accounting_manager)


@pytest.fixture
def draft_day(
    chart: dict[str, str],
    organization: Organization,
    branch: Branch,
    menu_item: MenuItem,
    hall: SalesChannel,
    manager: User,
) -> SalesDay:
    day = create_sales_day(
        organization=organization, branch=branch, business_date=BUSINESS_DATE, actor=manager
    )
    add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("2.000"))
    return day


def _shift(
    organization: Organization,
    branch: Branch,
    cashier: User,
    actor: User,
    opening_float: Decimal = Decimal("5000"),
) -> CashierShift:
    return open_cashier_shift(
        organization=organization,
        branch=branch,
        business_date=BUSINESS_DATE,
        cashier=cashier,
        opening_float=opening_float,
        actor=actor,
    )


def _closed(shift: CashierShift, day: SalesDay, counted: Decimal, actor: User) -> CashierShift:
    set_tender_count(
        shift=shift, tender=TenderDestination.CASH, counted_amount=counted, actor=actor
    )
    return close_cashier_shift(shift=shift, sales_day=day, actor=actor)


def _lines_by_code(entry: JournalEntry) -> dict[str, JournalLine]:
    return {line.account.code: line for line in entry.lines.select_related("account")}


def _shift_entry(shift: CashierShift) -> JournalEntry:
    return JournalEntry.objects.get(
        source_document_type=SOURCE_DOCUMENT_TYPE, source_document_id=str(shift.public_id)
    )


# ---------------------------------------------------------------------------
# The expectation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheExpectation:
    def test_the_float_raises_the_expected_count_and_nothing_else(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        """
        The opening float belongs in what should be in the drawer and nowhere
        else. It is the restaurant's own money moved from a safe.
        """
        shift = _shift(organization, branch, cashier, manager)
        shift.sales_day = cash_day
        assert expected_by_tender(shift)[TenderDestination.CASH] == Decimal("20000.000")
        assert expected_cash_for(shift) == Decimal("25000.000")

    def test_an_application_line_is_not_countable(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        """A delivery application's debt is not in a drawer."""
        shift = _shift(organization, branch, cashier, manager)
        shift.sales_day = cash_day
        assert TenderDestination.APPLICATION_RECEIVABLE not in expected_by_tender(shift)

    def test_a_draft_day_contributes_nothing(
        self,
        draft_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        shift = _shift(organization, branch, cashier, manager)
        shift.sales_day = draft_day
        assert expected_by_tender(shift)[TenderDestination.CASH] == Decimal("0.000")


# ---------------------------------------------------------------------------
# A refund paid out of this drawer, on this date
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestASameDayRefundLeavesTheDrawer:
    """
    The audit finding this class exists for.

    `post_sales_adjustment` credits `SALES_CASH_ON_HAND` — the money is
    physically handed back out of the drawer. An expectation derived from the
    day's lines alone does not know that, so the count comes up short by exactly
    the refund and `approve_cashier_shift` posts the shortage: the same cash is
    credited twice, the ledger drawer goes negative against a box holding what
    it holds, and المطابقة اليومية reports an ADVISORY variance rather than the
    ERROR that would have made somebody look.
    """

    def _refund(
        self, day: SalesDay, manager: User, business_date: datetime.date, quantity: Decimal
    ) -> None:
        adjustment = create_sales_adjustment(
            sales_day=day,
            reason_kind=SalesAdjustmentReasonKind.RETURNED_AFTER_FULFILLMENT,
            business_date=business_date,
            reason="الزبون أعاد الطبق ونُقد ثمنه.",
            evidence_reference="محضر إرجاع ٩",
            actor=manager,
        )
        add_adjustment_line(
            adjustment=adjustment,
            original_line=day.lines.get(),
            adjusted_quantity=quantity,
            actor=manager,
        )
        post_sales_adjustment(adjustment=adjustment, actor=manager)

    def test_the_expectation_drops_by_what_was_refunded(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        """20,000 sold, one 10,000 plate handed back on the same date."""
        self._refund(cash_day, manager, BUSINESS_DATE, Decimal("1.000"))
        shift = _shift(organization, branch, cashier, manager)
        shift.sales_day = cash_day
        assert expected_by_tender(shift)[TenderDestination.CASH] == Decimal("10000.000")
        assert expected_cash_for(shift) == Decimal("15000.000")

    def test_an_honest_drawer_shows_no_variance_and_posts_no_journal(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        """
        The whole point, stated once.

        The drawer holds the float plus 20,000 taken minus 10,000 given back.
        Counting exactly that must be a variance of nothing — before the fix it
        was a 10,000 shortage, and approving it credited `SALES_CASH_ON_HAND` a
        second time for cash that had already left.
        """
        self._refund(cash_day, manager, BUSINESS_DATE, Decimal("1.000"))
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("15000"),
            cashier,
        )
        assert shift.expected_cash == Decimal("15000.000")
        assert shift.variance_amount == Decimal("0.000")

        approved = approve_cashier_shift(shift=shift, actor=manager)
        assert approved.status == CashierShiftStatus.APPROVED
        assert not JournalEntry.objects.filter(
            source_document_type=SOURCE_DOCUMENT_TYPE, source_document_id=str(shift.public_id)
        ).exists()

    def test_the_drawer_account_is_not_credited_twice(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        """`SALES_CASH_ON_HAND` ends at what is really in the box."""
        self._refund(cash_day, manager, BUSINESS_DATE, Decimal("1.000"))
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("15000"),
            cashier,
        )
        approve_cashier_shift(shift=shift, actor=manager)

        balance = sum(
            (
                line.debit - line.credit
                for line in JournalLine.objects.select_related("account").filter(
                    account__code="1-01-01-001"
                )
            ),
            Decimal("0"),
        )
        assert balance == Decimal("10000.000")

    def test_a_refund_dated_later_belongs_to_that_dates_drawer(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        """
        The scope is the date, not the day.

        A correction decided tomorrow is paid out of tomorrow's drawer, and
        subtracting it here would move a count somebody has already declared —
        which is the freeze `expected_cash` exists to provide.
        """
        self._refund(
            cash_day, manager, BUSINESS_DATE + datetime.timedelta(days=1), Decimal("1.000")
        )
        shift = _shift(organization, branch, cashier, manager)
        shift.sales_day = cash_day
        assert expected_by_tender(shift)[TenderDestination.CASH] == Decimal("20000.000")

    def test_the_reconciliation_does_not_call_the_expectation_stale(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        """
        `expected_now` is recomputed through the function that stamped it.

        Re-deriving it from the day's lines was a second implementation of the
        expectation, and a second implementation is a second thing that can
        disagree — every corrected day would have reported an ERROR saying the
        count was closed against arithmetic that had since moved, when nothing
        had moved at all.
        """
        self._refund(cash_day, manager, BUSINESS_DATE, Decimal("1.000"))
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("15000"),
            cashier,
        )
        approve_cashier_shift(shift=shift, actor=manager)

        row = reconcile_day(sales_day=cash_day)
        assert "cashier_shift_expectation_is_stale" not in {finding.code for finding in row.errors}
        assert "cashier_shift_variance" not in {finding.code for finding in row.advisories}


# ---------------------------------------------------------------------------
# The journal, and everything it must not contain
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheShortageJournal:
    def test_it_debits_over_short_and_credits_the_till(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        assert shift.variance_amount == Decimal("-1000.000")
        approved = approve_cashier_shift(shift=shift, actor=accounting_manager)

        lines = _lines_by_code(_shift_entry(approved))
        assert lines["7-09-05-002"].debit == Decimal("1000.000")
        assert lines["1-01-01-001"].credit == Decimal("1000.000")
        assert len(lines) == 2

    def test_it_posts_no_revenue_line(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """
        The single most important assertion in this module.

        The sale already recognised the revenue and already debited cash. A
        closing that posted takings again would double every cash sales figure
        in the system, and the duplication would be invisible because both
        entries would be individually defensible (ADR-027 §8).
        """
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        approved = approve_cashier_shift(shift=shift, actor=accounting_manager)
        codes = set(_lines_by_code(_shift_entry(approved)))
        assert "4-01-01-001" not in codes
        assert "4-02-01-001" not in codes

    def test_it_posts_no_card_clearing_line(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """
        Card takings sit in `SALES_CARD_CLEARING` until the acquirer remits.
        They are not in the drawer to be counted, and recognising a difference
        in them would be recognising a variance against money nobody has.
        """
        shift = _shift(organization, branch, cashier, manager)
        set_tender_count(
            shift=shift,
            tender=TenderDestination.CARD,
            counted_amount=Decimal("777"),
            actor=manager,
        )
        shift = _closed(shift, cash_day, Decimal("24000"), manager)
        approved = approve_cashier_shift(shift=shift, actor=accounting_manager)
        assert "1-03-01-001" not in _lines_by_code(_shift_entry(approved))

    def test_it_does_not_post_the_days_takings_a_second_time(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """
        The cash account moves by the variance, never by the takings. Twenty
        thousand posted when the day did; a thousand moves when the shift does.
        """
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        approved = approve_cashier_shift(shift=shift, actor=accounting_manager)
        cash_line = _lines_by_code(_shift_entry(approved))["1-01-01-001"]
        assert cash_line.credit == Decimal("1000.000")
        assert cash_line.debit == Decimal("0.000")

    def test_it_writes_no_receivable_entry(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """A shift never touches the application receivable ledger, ever."""
        before = ApplicationReceivableEntry.objects.count()
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        approve_cashier_shift(shift=shift, actor=accounting_manager)
        assert ApplicationReceivableEntry.objects.count() == before


@pytest.mark.django_db
class TestTheOverageJournal:
    def test_it_debits_the_till_and_credits_over_short(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("25500"),
            manager,
        )
        assert shift.variance_amount == Decimal("500.000")
        approved = approve_cashier_shift(shift=shift, actor=accounting_manager)

        lines = _lines_by_code(_shift_entry(approved))
        assert lines["1-01-01-001"].debit == Decimal("500.000")
        assert lines["7-09-05-002"].credit == Decimal("500.000")
        assert len(lines) == 2

    def test_an_overage_is_not_revenue(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """Money no sale explains is a difference. Calling it income would let
        a mis-rung sale look like a good day."""
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("25500"),
            manager,
        )
        approved = approve_cashier_shift(shift=shift, actor=accounting_manager)
        assert "4-01-01-001" not in _lines_by_code(_shift_entry(approved))


@pytest.mark.django_db
class TestTheZeroVariance:
    def test_it_posts_no_journal_and_still_takes_a_number(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """
        A legitimate outcome, not a failure. The document exists whether or not
        it moved money, and a till that counted right must be able to finish its
        day.
        """
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("25000"),
            manager,
        )
        assert shift.variance_amount == Decimal("0.000")
        approved = approve_cashier_shift(shift=shift, actor=accounting_manager)

        assert approved.status == CashierShiftStatus.APPROVED
        assert approved.number.startswith("CS-2026-")
        assert not JournalEntry.objects.filter(
            source_document_type=SOURCE_DOCUMENT_TYPE, source_document_id=str(approved.public_id)
        ).exists()

    def test_a_zero_variance_shift_can_still_be_reversed(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """Refusing would leave the only exit closed for exactly the shifts that
        had nothing wrong with them."""
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("25000"),
            manager,
        )
        approved = approve_cashier_shift(shift=shift, actor=accounting_manager)
        reversed_shift = reverse_cashier_shift(
            shift=approved, actor=accounting_manager, reason="اعتُمد بالخطأ"
        )
        assert reversed_shift.status == CashierShiftStatus.REVERSED


# ---------------------------------------------------------------------------
# Maker-checker, in both places it lives
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMakerChecker:
    def test_the_service_refuses_the_closer_as_approver(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        with pytest.raises(ValidationError) as caught:
            approve_cashier_shift(shift=shift, actor=manager)
        assert caught.value.code == "approver_is_the_closer"

    def test_the_database_refuses_it_too(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        """
        The control that survives a data fix applied through a shell. A raw
        `update()` walks past every service check in this repository; it does
        not walk past `sales_shift_approver_is_not_the_closer`.
        """
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        with pytest.raises(Exception) as caught:  # noqa: PT011 - IntegrityError or subclass
            with transaction.atomic():
                CashierShift.objects.filter(pk=shift.pk).update(
                    status=CashierShiftStatus.APPROVED,
                    number="CS-2026-99999",
                    approved_by=manager,
                    approved_at=timezone.now(),
                )
        assert "sales_shift_approver_is_not_the_closer" in str(caught.value)

    def test_a_different_approver_is_accepted(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """
        The point of enforcing on the actor rather than on the permission: a
        branch with one manager must still be able to run.
        """
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        approved = approve_cashier_shift(shift=shift, actor=accounting_manager)
        assert approved.approved_by == accounting_manager
        assert approved.closed_by == manager


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheRefusals:
    def test_a_shift_cannot_close_against_a_draft_day(
        self,
        draft_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        """
        An expectation derived from a draft is a target that can move after the
        count, and the variance would be a difference between a count and
        something still being edited.
        """
        shift = _shift(organization, branch, cashier, manager)
        set_tender_count(
            shift=shift,
            tender=TenderDestination.CASH,
            counted_amount=Decimal("24000"),
            actor=manager,
        )
        with pytest.raises(ValidationError) as caught:
            close_cashier_shift(shift=shift, sales_day=draft_day, actor=manager)
        assert caught.value.code == "day_not_submitted"

    def test_an_application_receivable_cannot_be_counted(
        self, organization: Organization, branch: Branch, cashier: User, manager: User
    ) -> None:
        """The service refuses it, and so does a check constraint."""
        shift = _shift(organization, branch, cashier, manager)
        with pytest.raises(ValidationError) as caught:
            set_tender_count(
                shift=shift,
                tender=TenderDestination.APPLICATION_RECEIVABLE,
                counted_amount=Decimal("1"),
                actor=manager,
            )
        assert caught.value.code == "tender_is_not_countable"

        with pytest.raises(Exception) as raw:  # noqa: PT011 - IntegrityError or subclass
            with transaction.atomic():
                CashierTenderCount.objects.create(
                    shift=shift,
                    tender=TenderDestination.APPLICATION_RECEIVABLE,
                    counted_amount=Decimal("1"),
                )
        assert "sales_shift_tender_is_countable" in str(raw.value)

    def test_a_closed_shift_freezes_its_figures(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        """
        A count that can be edited after the approval fails is not a count. The
        obvious move for somebody facing an awkward shortage is to adjust the
        figure until it disappears.
        """
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        with pytest.raises(Exception, match="frozen"), transaction.atomic():
            CashierShift.objects.filter(pk=shift.pk).update(
                counted_cash=Decimal("25000.000"), variance_amount=Decimal("0.000")
            )

    def test_an_approved_shift_may_only_become_reversed(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        approved = approve_cashier_shift(shift=shift, actor=accounting_manager)
        with pytest.raises(Exception, match="may only become REVERSED"), transaction.atomic():
            CashierShift.objects.filter(pk=approved.pk).update(status=CashierShiftStatus.OPEN)

    def test_a_tender_count_cannot_move_once_the_shift_is_closed(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        row = shift.tender_counts.get(tender=TenderDestination.CASH)
        with pytest.raises(Exception, match="while its shift is open"), transaction.atomic():
            CashierTenderCount.objects.filter(pk=row.pk).update(counted_amount=Decimal("99.000"))

    def test_reopening_restores_the_count_and_stays_on_the_record(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        reopened = reopen_cashier_shift(shift=shift, actor=manager, reason="خطأ في العدّ")
        assert reopened.status == CashierShiftStatus.OPEN
        assert reopened.closed_by is None
        set_tender_count(
            shift=reopened,
            tender=TenderDestination.CASH,
            counted_amount=Decimal("25000"),
            actor=manager,
        )
        closed_again = close_cashier_shift(shift=reopened, sales_day=cash_day, actor=manager)
        assert closed_again.variance_amount == Decimal("0.000")

    def test_reopening_needs_a_reason(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
    ) -> None:
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        with pytest.raises(ValidationError) as caught:
            reopen_cashier_shift(shift=shift, actor=manager, reason="   ")
        assert caught.value.code == "reason_required"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthorization:
    def test_a_cashier_cannot_approve_a_closing(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """
        The whole point of the second state is that somebody other than the
        person who counted agrees the count.
        """
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        response = client_for(cashier).post(f"/sales/cashier-shifts/{shift.pk}/approve/")
        assert response.status_code == 403
        assert CashierShift.objects.get(pk=shift.pk).status == CashierShiftStatus.CLOSED

    def test_a_cashier_may_close(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """`close_cashier_shift` reaches the cashier. Counting is their job."""
        shift = _shift(organization, branch, cashier, manager)
        set_tender_count(
            shift=shift,
            tender=TenderDestination.CASH,
            counted_amount=Decimal("24000"),
            actor=cashier,
        )
        response = client_for(cashier).post(
            f"/sales/cashier-shifts/{shift.pk}/close/", {"sales_day": cash_day.pk}
        )
        assert response.status_code == 302
        assert CashierShift.objects.get(pk=shift.pk).status == CashierShiftStatus.CLOSED

    def test_a_manager_cannot_reverse_an_approved_shift(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """
        Reversal is `reverse_daily_sales`, read off the migrated labels rather
        than chosen: neither shift permission says reverse.
        """
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        approved = approve_cashier_shift(shift=shift, actor=accounting_manager)
        response = client_for(manager).post(
            f"/sales/cashier-shifts/{approved.pk}/reverse/", {"reason": "خطأ"}
        )
        assert response.status_code == 403
        assert CashierShift.objects.get(pk=approved.pk).status == CashierShiftStatus.APPROVED

    def test_an_outsider_gets_a_404_and_not_a_403(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        outsider: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """A 403 about another organization's record confirms it exists."""
        shift = _shift(organization, branch, cashier, manager)
        response = client_for(outsider).get(f"/sales/cashier-shifts/{shift.pk}/")
        assert response.status_code == 404

    def test_the_screens_answer_as_a_page_and_as_a_fragment(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """An htmx fragment carrying a second shell renders well enough to be
        missed in review and is wrong in every accessibility tree."""
        shift = _shift(organization, branch, cashier, manager)
        client = client_for(manager)
        for path in (
            "/sales/cashier-shifts/",
            f"/sales/cashier-shifts/{shift.pk}/",
            "/sales/reports/daily-reconciliation/",
        ):
            page = client.get(path)
            fragment = client.get(path, headers={"HX-Request": "true"})
            assert page.status_code == 200, path
            assert fragment.status_code == 200, path
            assert b"<html" not in fragment.content.lower(), path


# ---------------------------------------------------------------------------
# المطابقة اليومية
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheDailyReconciliation:
    def test_a_clean_day_reports_clean(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("25000"),
            manager,
        )
        approve_cashier_shift(shift=shift, actor=accounting_manager)

        result = reconcile_day(sales_day=cash_day)
        assert result.is_clean
        assert result.errors == ()

    def test_a_declaration_that_disagrees_with_the_lines_is_an_advisory(
        self,
        chart: dict[str, str],
        organization: Organization,
        branch: Branch,
        menu_item: MenuItem,
        hall: SalesChannel,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """
        Both figures are reported, never just the gap: which of the two is wrong
        decides who fixes it.

        This day is built here rather than taken from the fixture because the
        disagreement has to be typed while the day is still a draft — which is
        the only moment a declaration can be entered at all, and exactly why the
        report never treats it as independent evidence.
        """
        day = create_sales_day(
            organization=organization, branch=branch, business_date=BUSINESS_DATE, actor=manager
        )
        add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("2.000"))
        set_tender_summary(day=day, tender=TenderDestination.CASH, declared_amount=Decimal("19500"))
        submit_sales_day(day=day, actor=manager)
        cash_day = post_sales_day(day=day, actor=accounting_manager)

        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("25000"),
            manager,
        )
        approve_cashier_shift(shift=shift, actor=accounting_manager)

        result = reconcile_day(sales_day=cash_day)
        cash_leg = next(leg for leg in result.legs if leg.tender == TenderDestination.CASH)
        assert cash_leg.declared == Decimal("19500.000")
        assert cash_leg.derived == Decimal("20000.000")
        assert cash_leg.difference == Decimal("-500.000")
        assert any(
            row.code == "sales_declaration_disagrees_with_lines" for row in result.advisories
        )
        assert not result.is_clean

    def test_a_till_shortage_is_an_advisory_and_not_an_error(
        self,
        cash_day: SalesDay,
        organization: Organization,
        branch: Branch,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """A shortage is something a human decides about, not something that
        should have been impossible."""
        shift = _closed(
            _shift(organization, branch, cashier, manager),
            cash_day,
            Decimal("24000"),
            manager,
        )
        approve_cashier_shift(shift=shift, actor=accounting_manager)

        result = reconcile_day(sales_day=cash_day)
        assert any(row.code == "cashier_shift_variance" for row in result.advisories)
        assert result.errors == ()
        assert result.cash_variance == Decimal("-1000.000")

    def test_a_missing_shift_is_a_coverage_limitation(self, cash_day: SalesDay) -> None:
        """
        A branch that has not counted its drawer yet has done nothing wrong, and
        reporting it as a failure would make the screen red every morning.
        """
        result = reconcile_day(sales_day=cash_day)
        assert any(row.code == "cashier_shift_is_missing" for row in result.limitations)
        assert result.errors == ()
        assert result.is_clean

    def test_the_severities_are_the_kitchens_own(self) -> None:
        """`verify_sales` composes these with the kitchen's verifiers in
        checkpoint 7, and neither side may invent a class."""
        from apps.kitchen.consumption_reconciliation import (
            ADVISORY as KITCHEN_ADVISORY,
        )
        from apps.kitchen.consumption_reconciliation import (
            COVERAGE_LIMITATION as KITCHEN_LIMITATION,
        )

        assert ADVISORY == KITCHEN_ADVISORY
        assert COVERAGE_LIMITATION == KITCHEN_LIMITATION


@pytest.mark.django_db
class TestDailyFinancialClose:
    """The new control is a pre-posting workflow, not a report annotation."""

    def _submitted_day(
        self,
        *,
        organization: Organization,
        branch: Branch,
        menu_item: MenuItem,
        hall: SalesChannel,
        cashier: User,
    ) -> SalesDay:
        day = create_sales_day(
            organization=organization,
            branch=branch,
            business_date=BUSINESS_DATE,
            actor=cashier,
        )
        add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("2.000"))
        set_tender_summary(day=day, tender=TenderDestination.CASH, declared_amount=Decimal("20000"))
        submit_sales_day(day=day, actor=cashier)
        return day

    def _enforce_from_the_test_day(self, organization: Organization) -> None:
        AccountingSettings.objects.filter(organization=organization).update(
            daily_close_enforced_from=BUSINESS_DATE
        )

    def test_clean_close_needs_an_independent_reviewer_before_sales_posts(
        self,
        chart: dict[str, str],
        organization: Organization,
        branch: Branch,
        menu_item: MenuItem,
        hall: SalesChannel,
        cashier: User,
        manager: User,
        accounting_manager: User,
    ) -> None:
        self._enforce_from_the_test_day(organization)
        day = self._submitted_day(
            organization=organization,
            branch=branch,
            menu_item=menu_item,
            hall=hall,
            cashier=cashier,
        )

        with pytest.raises(ValidationError) as same_actor:
            post_sales_day(day=day, actor=cashier)
        assert same_actor.value.code == "poster_is_submitter"

        with pytest.raises(ValidationError) as missing_close:
            post_sales_day(day=day, actor=accounting_manager)
        assert missing_close.value.code == "daily_financial_close_required"

        shift = _shift(organization, branch, cashier, cashier)
        set_tender_count(
            shift=shift,
            tender=TenderDestination.CASH,
            counted_amount=Decimal("25000"),
            actor=cashier,
        )
        closed = close_cashier_shift(shift=shift, sales_day=day, actor=cashier)
        assert closed.status == CashierShiftStatus.CLOSED
        assert closed.expected_cash == Decimal("25000.000")

        close = submit_daily_financial_close(sales_day=day, actor=cashier)
        assert close.status == DailyFinancialCloseStatus.SUBMITTED
        assert close.exception_count == 0

        with pytest.raises(ValidationError) as self_review:
            approve_daily_financial_close(close=close, actor=cashier)
        assert self_review.value.code == "daily_close_reviewer_is_submitter"

        approved = approve_daily_financial_close(close=close, actor=manager)
        assert approved.status == DailyFinancialCloseStatus.APPROVED
        assert approved.reviewed_by_id == manager.pk

        # The evidence snapshot cannot be rewritten after a reviewer relied on
        # it, even through a raw queryset update that bypasses the service.
        with pytest.raises(Exception, match="immutable"), transaction.atomic():
            type(approved).objects.filter(pk=approved.pk).update(
                reconciliation_snapshot={"rewritten": True}
            )

        posted = post_sales_day(day=day, actor=accounting_manager)
        assert posted.status == SalesDayStatus.POSTED

    def test_mismatched_declaration_is_saved_as_a_blocked_exception(
        self,
        chart: dict[str, str],
        organization: Organization,
        branch: Branch,
        menu_item: MenuItem,
        hall: SalesChannel,
        cashier: User,
        accounting_manager: User,
    ) -> None:
        self._enforce_from_the_test_day(organization)
        day = create_sales_day(
            organization=organization,
            branch=branch,
            business_date=BUSINESS_DATE,
            actor=cashier,
        )
        add_sales_line(day=day, menu_item=menu_item, channel=hall, quantity=Decimal("2.000"))
        # This is the live risk seen in the current data: sales lines exist but
        # the declared tender says zero. It must remain visible as evidence.
        set_tender_summary(day=day, tender=TenderDestination.CASH, declared_amount=Decimal("0"))
        submit_sales_day(day=day, actor=cashier)
        shift = _shift(organization, branch, cashier, cashier)
        set_tender_count(
            shift=shift,
            tender=TenderDestination.CASH,
            counted_amount=Decimal("25000"),
            actor=cashier,
        )
        close_cashier_shift(shift=shift, sales_day=day, actor=cashier)

        close = submit_daily_financial_close(sales_day=day, actor=cashier)
        assert close.status == DailyFinancialCloseStatus.BLOCKED
        assert close.exception_count == 1
        assert (
            close.reconciliation_snapshot["exceptions"][0]["code"] == "tender_declaration_mismatch"
        )

        # The frozen close remains the evidence, while the outbox creates one
        # current, owned exception and inbox task for the SalesDay. Retrying
        # the worker must not create another task for the same open condition.
        process_due_events(limit=10)
        persisted = AutomationException.objects.get(
            organization=organization,
            branch=branch,
            code="tender_declaration_mismatch",
            target_id=str(day.pk),
            status="OPEN",
        )
        assert persisted.is_blocking is True
        assert persisted.amount == Decimal("20000.000")
        assert AutomationTask.objects.filter(exception=persisted, status="OPEN").count() == 1
        process_due_events(limit=10)
        assert AutomationTask.objects.filter(exception=persisted, status="OPEN").count() == 1

        with pytest.raises(ValidationError) as refused:
            post_sales_day(day=day, actor=accounting_manager)
        assert refused.value.code == "daily_financial_close_not_approved"

    def test_missing_cashier_close_becomes_a_blocking_owned_exception(
        self,
        organization: Organization,
        branch: Branch,
        menu_item: MenuItem,
        hall: SalesChannel,
        cashier: User,
    ) -> None:
        self._enforce_from_the_test_day(organization)
        day = self._submitted_day(
            organization=organization,
            branch=branch,
            menu_item=menu_item,
            hall=hall,
            cashier=cashier,
        )

        close = submit_daily_financial_close(sales_day=day, actor=cashier)
        assert close.status == DailyFinancialCloseStatus.BLOCKED
        assert close.reconciliation_snapshot["exceptions"] == [{"code": "cashier_shift_missing"}]

        process_due_events(limit=10)
        persisted = AutomationException.objects.get(
            organization=organization,
            branch=branch,
            code="cashier_shift_missing",
            target_id=str(day.pk),
            status="OPEN",
        )
        assert persisted.is_blocking is True
        assert AutomationTask.objects.filter(exception=persisted, status="OPEN").count() == 1
