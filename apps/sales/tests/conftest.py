"""
Fixtures for the sales tests.

Hand-built rather than seeded from the demo dataset, for the reason the
procurement fixtures record: the menu, its prices and the channels touch no
ledger, so these tests need an organization, a branch, a cost centre and a few
people — not a day of posted trading. The checkpoints that *do* post bring
their own heavier fixtures.

`Role.CASHIER` appears here as a first-class fixture, which it has not been
anywhere else in this system: until Phase 4 the role existed and granted
nothing in any module, so there was never anything to test it against.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from datetime import time
from typing import Any

import pytest
from django.test import Client

from apps.accounting.models import CostCenter
from apps.accounting.services import create_cost_center
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    grant_organization_access,
)
from apps.users.models import User

PASSWORD = "pw-not-real-1234"


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def other_organization() -> Organization:
    return create_organization(code="RIVAL", name_ar="منافس", name_en="Rival")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def second_branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="KARRADA",
        name_ar="الكرادة",
        name_en="Karrada",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def hall_cost_center(organization: Organization) -> CostCenter:
    return create_cost_center(
        organization=organization, code="HALL", name_ar="الصالة", name_en="Hall"
    )


@pytest.fixture
def delivery_cost_center(organization: Organization) -> CostCenter:
    return create_cost_center(
        organization=organization, code="DELIVERY", name_ar="التوصيل", name_en="Delivery"
    )


def _user(username: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD)


@pytest.fixture
def manager(branch: Branch) -> User:
    """
    A branch manager, and the interesting case for menu scope.

    They hold no organization membership, and the menu is organization
    property — so this fixture proves that *reaching* an organization through a
    branch is enough to maintain its menu, which is what
    `ORGANIZATION_MASTER_DATA` means.
    """
    user = _user("branch-manager")
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def cashier(branch: Branch) -> User:
    """Enters the day's takings and counts a drawer. Nothing else."""
    user = _user("cashier")
    grant_branch_access(user=user, branch=branch, role=Role.CASHIER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def accounting_manager(organization: Organization) -> User:
    user = _user("accounting-manager")
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def outsider(other_organization: Organization) -> User:
    """Holds real authority — somewhere else. The 404-not-403 case."""
    user = _user("outsider")
    grant_organization_access(user=user, organization=other_organization, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def superuser() -> User:
    return User.objects.create_superuser(username="root", password=PASSWORD)


@pytest.fixture
def client_for() -> Callable[[User], Client]:
    def _login(user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    return _login


# ---------------------------------------------------------------------------
# Checkpoint 7 — a whole posted scenario, for the dashboard, the API and the
# verifier
# ---------------------------------------------------------------------------
#
# New fixture *names* rather than changes to the ones above, so nothing here can
# alter what checkpoints 1 to 6 already assert. Each earlier test file builds the
# slice it needs; these three need the whole module at once — a posted day with
# cash, card and application lines, a posted adjustment, an approved drawer and a
# posted settlement — because that is what a dashboard aggregates, an API serves
# and a verifier reconciles.

SCENARIO_DATE = datetime.date(2026, 8, 10)
SCENARIO_ADJUSTMENT_DATE = datetime.date(2026, 8, 14)
SCENARIO_JANUARY = datetime.date(2026, 1, 1)

#: The eleven accounts the whole module reaches, and only those. Written out
#: rather than imported from the chart seed: a test that used production
#: reference data would pass for a reason other than the one it states.
_SCENARIO_CHART: tuple[tuple[str, str, str], ...] = (
    ("1", "الأصول", "Assets"),
    ("1-01", "النقد", "Cash"),
    ("1-01-01", "الصناديق", "Cash boxes"),
    ("1-01-01-001", "الصندوق", "Cash"),
    ("1-01-02", "المصارف", "Banks"),
    ("1-01-02-001", "المصرف", "Bank"),
    ("1-01-03", "مقاصة البطاقات", "Card clearing"),
    ("1-01-03-001", "مقاصة البطاقات", "Card Clearing"),
    ("1-02", "الذمم", "Receivables"),
    ("1-02-01", "ذمم التطبيقات", "App receivables"),
    ("1-02-01-009", "ذمم تطبيقات التوصيل", "App Receivable"),
    ("4", "الإيرادات", "Revenue"),
    ("4-01", "المبيعات", "Sales"),
    ("4-01-01", "مبيعات مباشرة", "Direct"),
    ("4-01-01-001", "مبيعات الصالة", "Dine-in Sales"),
    ("4-02", "خصومات المبيعات", "Discounts"),
    ("4-02-01", "خصومات المطعم", "Restaurant discounts"),
    ("4-02-01-001", "خصومات المبيعات", "Sales Discount"),
    ("4-03", "المردودات", "Returns"),
    ("4-03-01", "مردودات المبيعات", "Sales returns"),
    ("4-03-01-001", "مردودات وإلغاءات المبيعات", "Sales Returns"),
    ("6", "المصروفات", "Expenses"),
    ("6-03", "مصروفات البيع", "Selling"),
    ("6-03-01", "عمولات التطبيقات", "App commissions"),
    ("6-03-01-001", "عمولات التوصيل", "Commission"),
    ("6-03-01-002", "رسوم أخرى", "Other Fees"),
    ("7", "الفروقات", "Differences"),
    ("7-09", "فروقات التشغيل", "Operating differences"),
    ("7-09-05", "فروقات التسوية", "Settlement differences"),
    ("7-09-05-001", "فروقات تسوية التطبيقات", "Settlement Variance"),
    ("7-09-06", "فروقات الصندوق", "Cash differences"),
    ("7-09-06-001", "فروقات الصندوق", "Cash Over and Short"),
)


@pytest.fixture
def sales_chart(
    organization: Organization,
    hall_cost_center: CostCenter,
    delivery_cost_center: CostCenter,
) -> dict[str, str]:
    """Every Sales role mapped, and the fiscal year the scenario posts into."""
    from apps.accounting.models import (
        DELIVERY_APP_RECEIVABLE,
        DELIVERY_COMMISSION_EXPENSE,
        DELIVERY_OTHER_FEE_EXPENSE,
        DELIVERY_SETTLEMENT_VARIANCE,
        SALES_CARD_CLEARING,
        SALES_CASH_ON_HAND,
        SALES_CASH_OVER_SHORT,
        SALES_DISCOUNT,
        SALES_RETURNS,
        SALES_REVENUE,
        SALES_SETTLEMENT_BANK,
        Account,
        AccountRole,
    )
    from apps.accounting.services import create_account, create_account_mapping, open_fiscal_year

    for code, name_ar, name_en in _SCENARIO_CHART:
        create_account(organization=organization, code=code, name_ar=name_ar, name_en=name_en)
    mappings = {
        SALES_REVENUE: "4-01-01-001",
        SALES_DISCOUNT: "4-02-01-001",
        SALES_RETURNS: "4-03-01-001",
        SALES_CASH_ON_HAND: "1-01-01-001",
        SALES_CARD_CLEARING: "1-01-03-001",
        DELIVERY_APP_RECEIVABLE: "1-02-01-009",
        DELIVERY_COMMISSION_EXPENSE: "6-03-01-001",
        DELIVERY_OTHER_FEE_EXPENSE: "6-03-01-002",
        DELIVERY_SETTLEMENT_VARIANCE: "7-09-05-001",
        SALES_SETTLEMENT_BANK: "1-01-02-001",
        SALES_CASH_OVER_SHORT: "7-09-06-001",
    }
    for role, code in mappings.items():
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=role),
            account=Account.objects.get(organization=organization, code=code),
            effective_from=SCENARIO_JANUARY,
        )
    open_fiscal_year(organization=organization, year=SCENARIO_DATE.year)
    return mappings


@pytest.fixture
def scenario_recipe(organization: Organization) -> Any:
    """One active recipe version with one serving, effective from January."""
    from decimal import Decimal

    from django.utils import timezone

    from apps.kitchen.models import (
        ApprovalEvidenceKind,
        Recipe,
        RecipeServing,
        RecipeType,
        RecipeVersion,
        RecipeVersionStatus,
    )
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
        code="DEMO-SCN-MANDI",
        name_ar="مندي",
        recipe_type=RecipeType.PORTION,
    )
    preparer = _user("scenario-preparer")
    approver = _user("scenario-approver")
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
        effective_from=SCENARIO_JANUARY,
    )
    version.refresh_from_db()
    return recipe


@pytest.fixture
def scenario(
    sales_chart: dict[str, str],
    organization: Organization,
    branch: Branch,
    scenario_recipe: Any,
    hall_cost_center: CostCenter,
    delivery_cost_center: CostCenter,
    manager: User,
    cashier: User,
    accounting_manager: User,
) -> dict[str, Any]:
    """
    One posted day with a cash line and an application line, plus a posted
    cancellation, an approved drawer with a shortage, and a posted settlement.

    Built through the real services for the reason the demo seed is: a dashboard
    figure or a verifier equation checked against hand-inserted rows would prove
    the arithmetic and nothing about the posting rules that produced it.
    """
    from decimal import Decimal

    from apps.sales.adjustment_posting import post_sales_adjustment
    from apps.sales.adjustment_services import add_adjustment_line, create_sales_adjustment
    from apps.sales.day_services import (
        add_sales_line,
        create_sales_day,
        set_tender_summary,
        submit_sales_day,
        totals_for,
    )
    from apps.sales.models import (
        ApplicationReceivableEntry,
        CommissionBasis,
        ReceivableSource,
        SalesAdjustmentReasonKind,
        SalesChannelCategory,
        TenderDestination,
    )
    from apps.sales.posting import post_sales_day
    from apps.sales.services import (
        create_delivery_agreement,
        create_delivery_application,
        create_menu_item,
        create_menu_price,
        create_sales_channel,
        set_branch_availability,
    )
    from apps.sales.settlement_posting import post_settlement
    from apps.sales.settlement_services import (
        add_settlement_adjustment,
        allocate_entry,
        create_settlement,
        reconcile_settlement,
    )
    from apps.sales.shift_posting import approve_cashier_shift
    from apps.sales.shift_services import (
        close_cashier_shift,
        expected_cash_for,
        open_cashier_shift,
        set_tender_count,
    )

    item = create_menu_item(
        organization=organization,
        code="SCN-MENU",
        name_ar="مندي",
        recipe=scenario_recipe,
        serving_code="WHOLE",
    )
    set_branch_availability(item=item, branch=branch)
    create_menu_price(
        menu_item=item,
        branch=branch,
        unit_price=Decimal("10000"),
        effective_from=SCENARIO_JANUARY,
    )
    hall = create_sales_channel(
        organization=organization,
        code="SCN-HALL",
        name_ar="الصالة",
        category=SalesChannelCategory.DINE_IN,
        cost_center=hall_cost_center,
        default_tender=TenderDestination.CASH,
    )
    apps_channel = create_sales_channel(
        organization=organization,
        code="SCN-APPS",
        name_ar="تطبيقات",
        category=SalesChannelCategory.DELIVERY_APPLICATION,
        cost_center=delivery_cost_center,
        default_tender=TenderDestination.APPLICATION_RECEIVABLE,
    )
    application = create_delivery_application(
        organization=organization, code="SCN-APP", name_ar="تطبيق تجريبي"
    )
    create_delivery_agreement(
        branch=branch,
        delivery_application=application,
        effective_from=SCENARIO_JANUARY,
        commission_percent=Decimal("15"),
        commission_basis=CommissionBasis.GROSS_LIST_AMOUNT,
        evidence_reference="SCN/AGREEMENT-01",
    )

    day = create_sales_day(
        organization=organization, branch=branch, business_date=SCENARIO_DATE, actor=manager
    )
    add_sales_line(day=day, menu_item=item, channel=hall, quantity=Decimal("4.000"), order_count=4)
    add_sales_line(
        day=day,
        menu_item=item,
        channel=apps_channel,
        quantity=Decimal("6.000"),
        order_count=6,
        delivery_application=application,
    )
    totals = totals_for(day)
    set_tender_summary(day=day, tender=TenderDestination.CASH, declared_amount=totals.net_cash)
    set_tender_summary(
        day=day,
        tender=TenderDestination.APPLICATION_RECEIVABLE,
        declared_amount=totals.net_application,
    )
    submit_sales_day(day=day, actor=manager)
    day = post_sales_day(day=day, actor=accounting_manager)

    # The drawer is counted before the correction is recorded, which is both the
    # chronology and the rule: `expected_cash` is stamped at close, so a
    # correction decided later cannot move a count already declared.
    shift = open_cashier_shift(
        organization=organization,
        branch=branch,
        business_date=SCENARIO_DATE,
        cashier=cashier,
        opening_float=Decimal("5000"),
        actor=manager,
    )
    # Attached in memory only, so the expectation can be read against the day the
    # shift will close on. An open shift with no day named answers zero.
    shift.sales_day = day
    set_tender_count(
        shift=shift,
        tender=TenderDestination.CASH,
        counted_amount=expected_cash_for(shift) - Decimal("750"),
        actor=cashier,
    )
    close_cashier_shift(shift=shift, sales_day=day, actor=cashier)
    shift = approve_cashier_shift(shift=shift, actor=manager)

    cash_line = day.lines.order_by("sequence").get(sequence=1)
    adjustment = create_sales_adjustment(
        sales_day=day,
        reason_kind=SalesAdjustmentReasonKind.CANCELLED_BEFORE_FULFILLMENT,
        business_date=SCENARIO_ADJUSTMENT_DATE,
        reason="طلب أُلغي قبل التحضير.",
        evidence_reference="SCN/ADJ-01",
        actor=accounting_manager,
    )
    add_adjustment_line(
        adjustment=adjustment,
        original_line=cash_line,
        adjusted_quantity=Decimal("1.000"),
        actor=accounting_manager,
    )
    adjustment = post_sales_adjustment(adjustment=adjustment, actor=accounting_manager)

    entry = ApplicationReceivableEntry.objects.get(
        organization=organization,
        delivery_application=application,
        source=ReceivableSource.SALE_POSTED,
    )
    settlement = create_settlement(
        organization=organization,
        branch=branch,
        delivery_application=application,
        period_start=SCENARIO_DATE,
        period_end=SCENARIO_ADJUSTMENT_DATE,
        business_date=SCENARIO_ADJUSTMENT_DATE,
        statement_reference="SCN/STMT-01",
        statement_date=SCENARIO_ADJUSTMENT_DATE,
        statement_amount=entry.debit - Decimal("1000"),
        remitted_amount=entry.debit - Decimal("1500"),
        statement_commission_amount=Decimal("8000"),
        remittance_destination="BANK",
        evidence_reference="SCN/EVIDENCE",
        actor=accounting_manager,
    )
    allocate_entry(
        settlement=settlement,
        receivable_entry=entry,
        allocated_amount=entry.debit,
        actor=accounting_manager,
    )
    add_settlement_adjustment(
        settlement=settlement,
        leg="STATEMENT",
        reason="COMMISSION_RATE_DIFFERENCE",
        amount=Decimal("1000"),
        explanation="فرق نسبة.",
        actor=accounting_manager,
    )
    add_settlement_adjustment(
        settlement=settlement,
        leg="REMITTANCE",
        reason="WITHHOLDING_OR_OFFSET",
        amount=Decimal("500"),
        explanation="حجز.",
        actor=accounting_manager,
    )
    reconcile_settlement(settlement=settlement, actor=accounting_manager)
    settlement = post_settlement(settlement=settlement, actor=accounting_manager)

    return {
        "organization": organization,
        "branch": branch,
        "menu_item": item,
        "hall": hall,
        "apps_channel": apps_channel,
        "application": application,
        "day": day,
        "shift": shift,
        "adjustment": adjustment,
        "settlement": settlement,
    }
