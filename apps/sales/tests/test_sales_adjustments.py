"""
Returns, cancellations and financial corrections, end to end.

Everything asserted here is a claim ADR-028 §8 makes and that would be
expensive to discover was false:

* the journal reaches `SALES_RETURNS` and **never** `SALES_REVENUE`, because a
  posted gross revenue figure is not restated by anything;
* the application-funded discount reaches neither journal, exactly as it
  reaches neither sale journal;
* the receivable is a ledger entry, and the reversal names itself in the one
  field the canonicaliser does not case-fold;
* a posted adjustment is frozen, and over-adjusting is refused **by the
  database** rather than only by a service;
* a `FINANCIAL_CORRECTION` may not touch quantity;
* and — the cheapest possible guard against the regression that matters most —
  a posted **return** leaves the kitchen's contributed quantity exactly where
  it was, while a **cancellation** reduces it.

The fixtures deliberately reuse the shape `test_daily_sales_posting.py`
established, plus the `SALES_RETURNS` account and mapping the adjustment
journal needs.
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
    SALES_CASH_ON_HAND,
    SALES_DISCOUNT,
    SALES_RETURNS,
    SALES_REVENUE,
    Account,
    AccountRole,
    CostCenter,
    JournalEntry,
    JournalLine,
)
from apps.accounting.services import create_account, create_account_mapping, open_fiscal_year
from apps.kitchen.models import Recipe, RecipeServing, RecipeVersion
from apps.organizations.models import Branch, Organization
from apps.sales.adjustment_posting import (
    REVERSAL_RECEIVABLE_ID_SUFFIX,
    SOURCE_DOCUMENT_TYPE,
    post_sales_adjustment,
    reverse_sales_adjustment,
)
from apps.sales.adjustment_services import (
    add_adjustment_line,
    adjustable_lines,
    create_sales_adjustment,
    proportional_amounts,
    totals_for,
)
from apps.sales.consumption_source import SalesQuantitySource, cancelled_quantities
from apps.sales.day_services import add_sales_line, create_sales_day, submit_sales_day
from apps.sales.models import (
    ApplicationReceivableEntry,
    CommissionBasis,
    DeliveryApplication,
    MenuItem,
    ReceivableSource,
    SalesAdjustment,
    SalesAdjustmentLine,
    SalesAdjustmentReasonKind,
    SalesAdjustmentStatus,
    SalesChannel,
    SalesChannelCategory,
    SalesDay,
    SalesDayLine,
    TenderDestination,
)
from apps.sales.posting import post_sales_day
from apps.sales.selectors import receivable_balance
from apps.sales.services import (
    create_delivery_agreement,
    create_delivery_application,
    create_discount_program,
    create_menu_item,
    create_menu_price,
    create_sales_channel,
    set_application_branch_setting,
    set_branch_availability,
)
from apps.users.models import User

BUSINESS_DATE = datetime.date(2026, 8, 10)
ADJUSTMENT_DATE = datetime.date(2026, 8, 11)
JANUARY = datetime.date(2026, 1, 1)

#: The chart the adjustment journal reaches. `4-03-01-001` — sales returns — is
#: the one this checkpoint adds beside checkpoint 3's five: a return is not a
#: discount, and netting the two would make a month of generous promotions
#: indistinguishable from a month of rejected food.
_CHART: tuple[tuple[str, str, str], ...] = (
    ("1", "الأصول", "Assets"),
    ("1-01", "النقد", "Cash"),
    ("1-01-01", "الصناديق", "Cash boxes"),
    ("1-01-01-001", "الصندوق", "Cash"),
    ("1-02", "الذمم", "Receivables"),
    ("1-02-01", "ذمم التطبيقات", "App receivables"),
    ("1-02-01-001", "ذمم تطبيقات التوصيل", "App Receivable"),
    ("4", "الإيرادات", "Revenue"),
    ("4-01", "المبيعات", "Sales"),
    ("4-01-01", "مبيعات مباشرة", "Direct"),
    ("4-01-01-001", "مبيعات الصالة", "Dine-in Sales"),
    ("4-02", "خصومات المبيعات", "Discounts"),
    ("4-02-01", "خصومات المطعم", "Restaurant discounts"),
    ("4-02-01-001", "خصومات المبيعات", "Sales Discount"),
    ("4-03", "مردودات المبيعات", "Returns"),
    ("4-03-01", "مردودات", "Returns"),
    ("4-03-01-001", "مردودات المبيعات", "Sales Returns"),
    ("6", "المصروفات", "Expenses"),
    ("6-03", "مصروفات البيع", "Selling"),
    ("6-03-01", "عمولات التطبيقات", "App commissions"),
    ("6-03-01-001", "عمولات التوصيل", "Commission"),
)


@pytest.fixture
def chart(
    organization: Organization,
    hall_cost_center: CostCenter,
    delivery_cost_center: CostCenter,
) -> dict[str, str]:
    for code, name_ar, name_en in _CHART:
        create_account(organization=organization, code=code, name_ar=name_ar, name_en=name_en)
    mappings = {
        SALES_REVENUE: "4-01-01-001",
        SALES_DISCOUNT: "4-02-01-001",
        SALES_RETURNS: "4-03-01-001",
        SALES_CASH_ON_HAND: "1-01-01-001",
        DELIVERY_APP_RECEIVABLE: "1-02-01-001",
        DELIVERY_COMMISSION_EXPENSE: "6-03-01-001",
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
        name_ar="كيلوغرام",
        name_en="Kilogram",
        dimension="MASS",
        factor_to_base=Decimal("1"),
        is_base=True,
    )
    recipe = Recipe.objects.create(
        organization=organization,
        code="DEMO-MANDI",
        name_ar="مندي",
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
    serving = RecipeServing.objects.create(
        version=version,
        code="WHOLE",
        name_ar="حبة كاملة",
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
        name_ar="مندي",
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
        name_ar="الصالة",
        category=SalesChannelCategory.DINE_IN,
        cost_center=hall_cost_center,
        default_tender=TenderDestination.CASH,
    )


@pytest.fixture
def app_channel(organization: Organization, delivery_cost_center: CostCenter) -> SalesChannel:
    return create_sales_channel(
        organization=organization,
        code="APPS",
        name_ar="تطبيقات",
        category=SalesChannelCategory.DELIVERY_APPLICATION,
        cost_center=delivery_cost_center,
    )


@pytest.fixture
def application(organization: Organization, branch: Branch) -> DeliveryApplication:
    app = create_delivery_application(
        organization=organization, code="DEMO-APP", name_ar="تطبيق تجريبي"
    )
    set_application_branch_setting(application=app, branch=branch)
    create_delivery_agreement(
        branch=branch,
        delivery_application=app,
        effective_from=JANUARY,
        commission_percent=Decimal("15"),
        commission_basis=CommissionBasis.GROSS_LIST_AMOUNT,
        evidence_reference="عقد تجريبي",
    )
    return app


def _posted_day(
    organization: Organization,
    branch: Branch,
    manager: User,
    accounting_manager: User,
    **line_kwargs: object,
) -> SalesDay:
    """A day with one line, walked through its real lifecycle to POSTED."""
    day = create_sales_day(
        organization=organization, branch=branch, business_date=BUSINESS_DATE, actor=manager
    )
    add_sales_line(day=day, quantity=Decimal("2.000"), **line_kwargs)  # type: ignore[arg-type]
    submit_sales_day(day=day, actor=manager)
    return post_sales_day(day=day, actor=accounting_manager)


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
    return _posted_day(
        organization, branch, manager, accounting_manager, menu_item=menu_item, channel=hall
    )


@pytest.fixture
def application_day(
    chart: dict[str, str],
    organization: Organization,
    branch: Branch,
    menu_item: MenuItem,
    app_channel: SalesChannel,
    application: DeliveryApplication,
    manager: User,
    accounting_manager: User,
) -> SalesDay:
    return _posted_day(
        organization,
        branch,
        manager,
        accounting_manager,
        menu_item=menu_item,
        channel=app_channel,
        delivery_application=application,
        order_count=2,
    )


def _adjustment(
    day: SalesDay,
    manager: User,
    kind: str = SalesAdjustmentReasonKind.RETURNED_AFTER_FULFILLMENT,
) -> SalesAdjustment:
    return create_sales_adjustment(
        sales_day=day,
        reason_kind=kind,
        business_date=ADJUSTMENT_DATE,
        reason="الزبون أعاد الطبق",
        evidence_reference="محضر إرجاع ٧",
        actor=manager,
    )


def _lines_by_code(entry: JournalEntry) -> dict[str, JournalLine]:
    return {line.account.code: line for line in entry.lines.select_related("account")}


def _adjustment_entry(adjustment: SalesAdjustment) -> JournalEntry:
    return JournalEntry.objects.get(
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(adjustment.public_id),
    )


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheProportionalArithmetic:
    def test_a_half_return_takes_back_half_of_every_figure(self, application_day: SalesDay) -> None:
        original = application_day.lines.get()
        amounts = proportional_amounts(original, adjusted_quantity=Decimal("1.000"))
        assert original.gross_amount == Decimal("20000.000")
        assert amounts.gross == Decimal("10000.000")
        assert amounts.commission == Decimal("1500.000")
        assert amounts.net_amount == Decimal("8500.000")

    def test_the_net_is_the_residual_and_the_credits_close(self, application_day: SalesDay) -> None:
        """
        The one arithmetic decision in this module, checked directly.

        Rating all seven figures independently and rounding each would let a
        return split into figures that do not sum, and the journal would be out
        by a fils for reasons nobody could reconstruct. Taking the residual
        makes the credits sum to the debit by construction.
        """
        original = application_day.lines.get()
        amounts = proportional_amounts(original, adjusted_quantity=Decimal("0.333"))
        credits = (
            amounts.restaurant_discount
            + amounts.commission
            + amounts.other_fees
            + amounts.net_amount
        )
        assert credits == amounts.gross

    def test_a_correction_by_amount_uses_the_gross_ratio(self, cash_day: SalesDay) -> None:
        original = cash_day.lines.get()
        amounts = proportional_amounts(
            original, adjusted_quantity=Decimal("0"), adjusted_gross=Decimal("5000.000")
        )
        assert amounts.gross == Decimal("5000.000")
        assert amounts.net_amount == Decimal("5000.000")

    def test_an_adjustment_larger_than_its_line_is_refused(self, cash_day: SalesDay) -> None:
        original = cash_day.lines.get()
        with pytest.raises(ValidationError) as caught:
            proportional_amounts(original, adjusted_quantity=Decimal("3.000"))
        assert caught.value.code == "adjustment_exceeds_line"


# ---------------------------------------------------------------------------
# The journals
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheCashJournal:
    def test_it_debits_returns_and_credits_the_till(
        self, cash_day: SalesDay, manager: User
    ) -> None:
        adjustment = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=cash_day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)

        assert posted.status == SalesAdjustmentStatus.POSTED
        assert posted.number.startswith("SA-2026-")

        lines = _lines_by_code(_adjustment_entry(posted))
        assert lines["4-03-01-001"].debit == Decimal("10000.000")
        assert lines["1-01-01-001"].credit == Decimal("10000.000")
        assert sum(row.debit for row in lines.values()) == sum(row.credit for row in lines.values())

    def test_sales_revenue_is_never_touched(self, cash_day: SalesDay, manager: User) -> None:
        """
        The single most consequential assertion in this module.

        Debiting revenue would restate a posted gross revenue figure and destroy
        ADR-027 §2's whole point — that revenue is gross and every deduction
        sits beside it as an identifiable claim.
        """
        adjustment = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=cash_day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)
        assert "4-01-01-001" not in _lines_by_code(_adjustment_entry(posted))

    def test_a_returns_line_carries_the_channels_cost_center(
        self, cash_day: SalesDay, manager: User, hall_cost_center: CostCenter
    ) -> None:
        """A sale never invents a cost centre and neither does its reversal."""
        adjustment = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=cash_day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)
        lines = _lines_by_code(_adjustment_entry(posted))
        assert lines["4-03-01-001"].cost_center == hall_cost_center
        # A balance-sheet account needs none and gets none.
        assert lines["1-01-01-001"].cost_center is None

    def test_a_restaurant_discount_is_credited_back_beside_the_return(
        self,
        chart: dict[str, str],
        organization: Organization,
        branch: Branch,
        menu_item: MenuItem,
        hall: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        day = _posted_day(
            organization,
            branch,
            manager,
            accounting_manager,
            menu_item=menu_item,
            channel=hall,
            manual_discount_amount=Decimal("2000"),
            manual_discount_reason="خصم ترويجي",
        )
        adjustment = _adjustment(day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)
        lines = _lines_by_code(_adjustment_entry(posted))
        assert lines["4-03-01-001"].debit == Decimal("10000.000")
        assert lines["4-02-01-001"].credit == Decimal("1000.000")
        assert lines["1-01-01-001"].credit == Decimal("9000.000")


@pytest.mark.django_db
class TestTheApplicationJournalAndReceivable:
    def _post(self, day: SalesDay, manager: User, **kwargs: object) -> SalesAdjustment:
        adjustment = _adjustment(day, manager, **kwargs)  # type: ignore[arg-type]
        add_adjustment_line(
            adjustment=adjustment,
            original_line=day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        return post_sales_adjustment(adjustment=adjustment, actor=manager)

    def test_the_journal_credits_commission_and_the_receivable(
        self, application_day: SalesDay, manager: User
    ) -> None:
        posted = self._post(application_day, manager)
        lines = _lines_by_code(_adjustment_entry(posted))
        assert lines["4-03-01-001"].debit == Decimal("10000.000")
        assert lines["6-03-01-001"].credit == Decimal("1500.000")
        assert lines["1-02-01-001"].credit == Decimal("8500.000")
        assert "4-01-01-001" not in lines
        assert sum(row.credit for row in lines.values()) == Decimal("10000.000")

    def test_the_application_funded_discount_reaches_neither_journal(
        self,
        chart: dict[str, str],
        organization: Organization,
        branch: Branch,
        menu_item: MenuItem,
        app_channel: SalesChannel,
        application: DeliveryApplication,
        manager: User,
        accounting_manager: User,
    ) -> None:
        """
        Stored, reported, never posted — exactly as on the sale (ADR-028 §3).

        The application reimburses it, so it reduces neither revenue nor what
        the application owes. Crediting it back here would understate both.
        """
        program = create_discount_program(
            organization=organization,
            code="DEMO-APPPROMO",
            name_ar="عرض التطبيق",
            effective_from=JANUARY,
            discount_percent=Decimal("20"),
            restaurant_funded_share=Decimal("0"),
            application_funded_share=Decimal("100"),
            delivery_application=application,
        )
        day = _posted_day(
            organization,
            branch,
            manager,
            accounting_manager,
            menu_item=menu_item,
            channel=app_channel,
            delivery_application=application,
            order_count=2,
            discount_program=program,
        )
        posted = self._post(day, manager)
        line = posted.lines.get()
        assert line.adjusted_application_discount == Decimal("2000.000")

        lines = _lines_by_code(_adjustment_entry(posted))
        assert "4-02-01-001" not in lines
        assert lines["4-03-01-001"].debit == Decimal("10000.000")

    def test_the_receivable_is_credited_and_the_balance_falls(
        self,
        application_day: SalesDay,
        organization: Organization,
        application: DeliveryApplication,
        manager: User,
    ) -> None:
        before = receivable_balance(
            delivery_application_id=application.pk, organization_id=organization.pk
        )
        assert before == Decimal("17000.000")
        posted = self._post(application_day, manager)
        entry = ApplicationReceivableEntry.objects.get(
            source_document_id=str(posted.public_id),
            source=ReceivableSource.AUTHORIZED_ADJUSTMENT,
        )
        assert entry.credit == Decimal("8500.000")
        assert receivable_balance(
            delivery_application_id=application.pk, organization_id=organization.pk
        ) == Decimal("8500.000")

    def test_the_reversal_names_itself_in_the_document_id(
        self,
        application_day: SalesDay,
        organization: Organization,
        application: DeliveryApplication,
        manager: User,
    ) -> None:
        """
        The suffix is necessary, not decorative.

        `ReceivableSource` is a closed set of five (ADR-027 §5) with no
        `ADJUSTMENT_REVERSED`, so the mirror entry shares the original's
        `source` and the only free component of the uniqueness key is the
        document id — the one field the canonicaliser deliberately does not
        case-fold.
        """
        posted = self._post(application_day, manager)
        reverse_sales_adjustment(
            adjustment=posted, actor=self._reverser(organization), reason="ألغيت التسوية"
        )

        rows = ApplicationReceivableEntry.objects.filter(
            source=ReceivableSource.AUTHORIZED_ADJUSTMENT
        ).order_by("pk")
        assert [row.source_document_id for row in rows] == [
            str(posted.public_id),
            f"{posted.public_id}{REVERSAL_RECEIVABLE_ID_SUFFIX}",
        ]
        assert rows[0].credit == rows[1].debit == Decimal("8500.000")
        # The original entry is untouched; the balance is back where it was.
        assert receivable_balance(
            delivery_application_id=application.pk, organization_id=organization.pk
        ) == Decimal("17000.000")

    @staticmethod
    def _reverser(organization: Organization) -> User:
        """
        Reversal needs `REVERSE_DAILY_SALES`, not `manage_sales_adjustments`.

        Built here rather than taken from a fixture so the test states the
        authority it needs out loud.
        """
        from apps.organizations.models import Role
        from apps.organizations.services import grant_organization_access

        user = User.objects.create_user(username="reverser", password="pw-not-real-1234")
        grant_organization_access(
            user=user, organization=organization, role=Role.ACCOUNTING_MANAGER
        )
        return User.objects.get(pk=user.pk)


# ---------------------------------------------------------------------------
# The database guards
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheDatabaseHoldsTheLine:
    def test_a_posted_adjustment_is_frozen(self, cash_day: SalesDay, manager: User) -> None:
        adjustment = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=cash_day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)

        posted.reason = "سبب آخر"
        with pytest.raises(Exception, match="frozen"), transaction.atomic():
            posted.save(update_fields=["reason"])

    def test_a_posted_adjustments_lines_cannot_be_changed(
        self, cash_day: SalesDay, manager: User
    ) -> None:
        adjustment = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=cash_day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)
        line = posted.lines.get()
        line.adjusted_quantity = Decimal("2.000")
        with pytest.raises(Exception, match="draft"), transaction.atomic():
            line.save(update_fields=["adjusted_quantity"])

    def test_over_adjusting_is_refused_by_the_database(
        self, cash_day: SalesDay, manager: User
    ) -> None:
        """
        A raw `INSERT` walks straight past a service check, so this rule is a
        trigger. Two full returns of the same line is the failure it stops.
        """
        original = cash_day.lines.get()
        first = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=first,
            original_line=original,
            adjusted_quantity=Decimal("2.000"),
            actor=manager,
        )
        post_sales_adjustment(adjustment=first, actor=manager)

        second = _adjustment(cash_day, manager)
        with pytest.raises(Exception, match="more quantity"), transaction.atomic():
            SalesAdjustmentLine.objects.create(
                adjustment=second,
                sequence=1,
                original_line=original,
                adjusted_quantity=Decimal("2.000"),
                unit_price=original.unit_price,
                adjusted_gross=Decimal("20000.000"),
                adjusted_customer_charge=Decimal("20000.000"),
                adjusted_net_amount=Decimal("20000.000"),
            )

    def test_a_financial_correction_may_not_touch_quantity(
        self, cash_day: SalesDay, manager: User
    ) -> None:
        """
        ADR-028 §8: a money correction is not a claim that less food was sold.

        Enforced by a trigger rather than only by a service, because letting a
        pricing fix silently rewrite sold quantity would change what the kitchen
        is measured against for a reason that has nothing to do with the kitchen.
        """
        original = cash_day.lines.get()
        correction = _adjustment(
            cash_day, manager, kind=SalesAdjustmentReasonKind.FINANCIAL_CORRECTION
        )
        with pytest.raises(Exception, match="may not change sold quantity"), transaction.atomic():
            SalesAdjustmentLine.objects.create(
                adjustment=correction,
                sequence=1,
                original_line=original,
                adjusted_quantity=Decimal("1.000"),
                unit_price=original.unit_price,
                adjusted_gross=Decimal("5000.000"),
                adjusted_customer_charge=Decimal("5000.000"),
                adjusted_net_amount=Decimal("5000.000"),
            )

    def test_a_draft_day_cannot_be_adjusted(
        self, organization: Organization, branch: Branch, manager: User
    ) -> None:
        draft = create_sales_day(
            organization=organization,
            branch=branch,
            business_date=datetime.date(2026, 8, 12),
            actor=manager,
        )
        with pytest.raises(ValidationError) as caught:
            _adjustment(draft, manager)
        assert caught.value.code == "day_not_posted"

    def test_the_service_refuses_over_adjustment_with_a_sentence(
        self, cash_day: SalesDay, manager: User
    ) -> None:
        original = cash_day.lines.get()
        first = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=first,
            original_line=original,
            adjusted_quantity=Decimal("1.500"),
            actor=manager,
        )
        post_sales_adjustment(adjustment=first, actor=manager)

        second = _adjustment(cash_day, manager)
        with pytest.raises(ValidationError) as caught:
            add_adjustment_line(
                adjustment=second,
                original_line=original,
                adjusted_quantity=Decimal("1.000"),
                actor=manager,
            )
        assert caught.value.code == "quantity_over_adjusted"

    def test_two_drafts_may_each_propose_the_whole_line_and_only_one_posts(
        self, cash_day: SalesDay, manager: User
    ) -> None:
        """
        The trigger counts only posted adjustments, so both drafts are legal.
        The second becomes wrong the moment the first posts, and the re-check
        under the row lock at posting is what catches it.
        """
        original = cash_day.lines.get()
        first = _adjustment(cash_day, manager)
        second = _adjustment(cash_day, manager)
        for draft in (first, second):
            add_adjustment_line(
                adjustment=draft,
                original_line=original,
                adjusted_quantity=Decimal("2.000"),
                actor=manager,
            )
        post_sales_adjustment(adjustment=first, actor=manager)
        with pytest.raises(ValidationError) as caught:
            post_sales_adjustment(adjustment=second, actor=manager)
        assert caught.value.code == "quantity_over_adjusted"

    def test_an_empty_adjustment_cannot_be_posted(self, cash_day: SalesDay, manager: User) -> None:
        adjustment = _adjustment(cash_day, manager)
        with pytest.raises(ValidationError) as caught:
            post_sales_adjustment(adjustment=adjustment, actor=manager)
        assert caught.value.code == "no_lines"

    def test_a_number_is_assigned_only_at_posting(self, cash_day: SalesDay, manager: User) -> None:
        adjustment = _adjustment(cash_day, manager)
        assert adjustment.number == ""
        add_adjustment_line(
            adjustment=adjustment,
            original_line=cash_day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        assert SalesAdjustment.objects.get(pk=adjustment.pk).number == ""
        assert post_sales_adjustment(adjustment=adjustment, actor=manager).number != ""


# ---------------------------------------------------------------------------
# The kitchen hook — the trap
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOnlyACancellationReachesTheKitchen:
    """
    The asymmetry ADR-028 §8 turns on, and the cheapest possible guard against
    the regression that matters most.

    The intuitive implementation — subtract every posted adjustment — is one
    filter shorter than the correct one, reads perfectly well, and produces a
    usage-variance report that is wrong in a direction nobody questions.
    """

    def test_a_cancellation_reduces_the_contributed_quantity(
        self, cash_day: SalesDay, manager: User
    ) -> None:
        original = cash_day.lines.get()
        adjustment = _adjustment(
            cash_day, manager, kind=SalesAdjustmentReasonKind.CANCELLED_BEFORE_FULFILLMENT
        )
        add_adjustment_line(
            adjustment=adjustment,
            original_line=original,
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        post_sales_adjustment(adjustment=adjustment, actor=manager)
        assert cancelled_quantities([original.pk]) == {original.pk: Decimal("1.000")}

    def test_a_return_does_not_reduce_the_contributed_quantity(
        self, cash_day: SalesDay, manager: User
    ) -> None:
        """
        The food was cooked and its ingredients left. Subtracting it would lower
        theoretical while actual stayed exactly where it was, manufacturing an
        unexplained variance of precisely the returned quantity — the same
        signal the usage-variance report exists to detect.
        """
        original = cash_day.lines.get()
        adjustment = _adjustment(
            cash_day, manager, kind=SalesAdjustmentReasonKind.RETURNED_AFTER_FULFILLMENT
        )
        add_adjustment_line(
            adjustment=adjustment,
            original_line=original,
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        post_sales_adjustment(adjustment=adjustment, actor=manager)
        assert cancelled_quantities([original.pk]) == {}

    def test_a_financial_correction_does_not_reduce_it_either(
        self, cash_day: SalesDay, manager: User
    ) -> None:
        original = cash_day.lines.get()
        adjustment = _adjustment(
            cash_day, manager, kind=SalesAdjustmentReasonKind.FINANCIAL_CORRECTION
        )
        add_adjustment_line(
            adjustment=adjustment,
            original_line=original,
            adjusted_quantity=Decimal("0"),
            adjusted_gross=Decimal("5000.000"),
            actor=manager,
        )
        post_sales_adjustment(adjustment=adjustment, actor=manager)
        assert cancelled_quantities([original.pk]) == {}

    def test_a_draft_cancellation_subtracts_nothing(
        self, cash_day: SalesDay, manager: User
    ) -> None:
        """A draft is a proposal. It cancelled nothing."""
        original = cash_day.lines.get()
        adjustment = _adjustment(
            cash_day, manager, kind=SalesAdjustmentReasonKind.CANCELLED_BEFORE_FULFILLMENT
        )
        add_adjustment_line(
            adjustment=adjustment,
            original_line=original,
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        assert cancelled_quantities([original.pk]) == {}

    def test_the_source_still_runs_against_a_cancelled_line(
        self, cash_day: SalesDay, organization: Organization, branch: Branch, manager: User
    ) -> None:
        original = cash_day.lines.get()
        adjustment = _adjustment(
            cash_day, manager, kind=SalesAdjustmentReasonKind.CANCELLED_BEFORE_FULFILLMENT
        )
        add_adjustment_line(
            adjustment=adjustment,
            original_line=original,
            adjusted_quantity=Decimal("2.000"),
            actor=manager,
        )
        post_sales_adjustment(adjustment=adjustment, actor=manager)
        contributions = SalesQuantitySource().contributions(
            organization_id=organization.pk,
            branch_ids=[branch.pk],
            date_from=None,
            date_to=None,
            recipe_id=None,
        )
        # A fully cancelled line contributes nothing at all, rather than a
        # negative ingredient requirement.
        assert contributions == []


# ---------------------------------------------------------------------------
# Authorization and the screens
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthorization:
    def test_a_cashier_cannot_post_an_adjustment(
        self,
        cash_day: SalesDay,
        manager: User,
        cashier: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """
        A till that can credit back its own takings is the oldest fraud in the
        trade, which is why `manage_sales_adjustments` reaches neither the
        cashier nor the accountant.
        """
        adjustment = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=cash_day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        response = client_for(cashier).post(f"/sales/adjustments/{adjustment.pk}/post/")
        assert response.status_code == 403
        assert SalesAdjustment.objects.get(pk=adjustment.pk).status == SalesAdjustmentStatus.DRAFT

    def test_a_manager_cannot_reverse_a_posted_adjustment(
        self, cash_day: SalesDay, manager: User, client_for: Callable[[User], Client]
    ) -> None:
        """
        Reversal is `reverse_daily_sales`, read off the migrated labels rather
        than chosen: `manage_sales_adjustments` says nothing about reversal.
        """
        adjustment = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=cash_day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)
        response = client_for(manager).post(
            f"/sales/adjustments/{posted.pk}/reverse/", {"reason": "خطأ"}
        )
        assert response.status_code == 403
        assert SalesAdjustment.objects.get(pk=posted.pk).status == SalesAdjustmentStatus.POSTED

    def test_the_reversal_screen_takes_a_typed_reason(
        self,
        cash_day: SalesDay,
        manager: User,
        accounting_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """
        `reversal_reason` may not be blank — a check constraint says so — and
        the screen asks for it rather than sending a canned constant.
        """
        adjustment = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=cash_day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)

        client = client_for(accounting_manager)
        page = client.get(f"/sales/adjustments/{posted.pk}/")
        assert "سبب العكس" in page.content.decode("utf-8")

        client.post(f"/sales/adjustments/{posted.pk}/reverse/", {"reason": "سُجّلت بالخطأ"})
        reversed_row = SalesAdjustment.objects.get(pk=posted.pk)
        assert reversed_row.status == SalesAdjustmentStatus.REVERSED
        assert reversed_row.reversal_reason == "سُجّلت بالخطأ"

    def test_a_reversal_with_no_reason_is_refused(
        self, cash_day: SalesDay, manager: User, accounting_manager: User
    ) -> None:
        adjustment = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=cash_day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)
        with pytest.raises(ValidationError) as caught:
            reverse_sales_adjustment(adjustment=posted, actor=accounting_manager, reason="   ")
        assert caught.value.code == "reason_required"

    def test_an_outsider_gets_a_404_and_not_a_403(
        self,
        cash_day: SalesDay,
        manager: User,
        outsider: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """A 403 about another organization's record confirms it exists."""
        adjustment = _adjustment(cash_day, manager)
        response = client_for(outsider).get(f"/sales/adjustments/{adjustment.pk}/")
        assert response.status_code == 404

    def test_the_list_and_the_detail_answer_as_page_and_as_fragment(
        self, cash_day: SalesDay, manager: User, client_for: Callable[[User], Client]
    ) -> None:
        adjustment = _adjustment(cash_day, manager)
        client = client_for(manager)
        for path in ("/sales/adjustments/", f"/sales/adjustments/{adjustment.pk}/"):
            page = client.get(path)
            fragment = client.get(path, headers={"HX-Request": "true"})
            assert page.status_code == 200
            assert fragment.status_code == 200
            # An htmx fragment carrying a second shell renders correctly enough
            # to be missed in review and is wrong in every accessibility tree.
            assert b"<html" not in fragment.content.lower()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestReads:
    def test_totals_are_derived_from_the_lines(
        self, application_day: SalesDay, manager: User
    ) -> None:
        adjustment = _adjustment(application_day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=application_day.lines.get(),
            adjusted_quantity=Decimal("1.000"),
            actor=manager,
        )
        totals = totals_for(adjustment)
        assert totals.line_count == 1
        assert totals.gross == Decimal("10000.000")
        assert totals.net_application == Decimal("8500.000")
        assert totals.net_cash == Decimal("0.000")

    def test_a_fully_adjusted_line_leaves_the_adjustable_list(
        self, cash_day: SalesDay, manager: User
    ) -> None:
        original = cash_day.lines.get()
        assert adjustable_lines(cash_day) == [original]
        adjustment = _adjustment(cash_day, manager)
        add_adjustment_line(
            adjustment=adjustment,
            original_line=original,
            adjusted_quantity=Decimal("2.000"),
            actor=manager,
        )
        post_sales_adjustment(adjustment=adjustment, actor=manager)
        assert adjustable_lines(cash_day) == []

    def test_an_adjustment_carries_no_total_field(self) -> None:
        """A stored total is a second source of truth for what the lines say."""
        fields = {field.name for field in SalesAdjustment._meta.get_fields()}
        for absent in ("total", "gross_total", "net_total", "total_amount"):
            assert absent not in fields

    def test_a_resolved_line_is_scoped_to_the_caller(
        self, cash_day: SalesDay, manager: User, outsider: User
    ) -> None:
        from apps.organizations.authorization import OutOfScope
        from apps.sales.selectors import resolve_sales_day_line

        line: SalesDayLine = cash_day.lines.get()
        assert resolve_sales_day_line(manager, line.pk) == line
        with pytest.raises(OutOfScope):
            resolve_sales_day_line(outsider, line.pk)
