"""
Application receivables and settlements, end to end.

Everything asserted here is a claim ADR-028 §§6–7 makes and that would be
expensive to discover was false:

* the journal debits what actually arrived, recognises the difference in
  `DELIVERY_SETTLEMENT_VARIANCE`, and credits the receivable — and contains
  **no class-6 commission line**, which is the single most likely error in the
  whole module;
* the statement's commission column is *compared* against the accrual made at
  the sale and reported as a gap, never expensed a second time;
* a settlement allocates to **posted receivable entries**, so it can say which
  sales it paid for;
* every dinar of both gaps must be claimed before the settlement may reconcile,
  and the check is repeated under the row lock at posting;
* `UNEXPLAINED_APPROVED` costs a written explanation and a named approver, both
  enforced by check constraints;
* a reconciled settlement's three figures are frozen by a database trigger;
* over-allocating a receivable entry is refused **by the database** and not
  merely by a service;
* and aging is derived from the ledger every time, with no stored bucket
  anywhere.

The fixtures reuse the shape `test_sales_adjustments.py` established, plus the
settlement-bank and settlement-variance accounts this checkpoint's journal
needs.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from apps.accounting.models import (
    DELIVERY_APP_RECEIVABLE,
    DELIVERY_COMMISSION_EXPENSE,
    DELIVERY_SETTLEMENT_VARIANCE,
    SALES_CASH_ON_HAND,
    SALES_DISCOUNT,
    SALES_RETURNS,
    SALES_REVENUE,
    SALES_SETTLEMENT_BANK,
    Account,
    AccountRole,
    CostCenter,
    JournalEntry,
    JournalLine,
)
from apps.accounting.services import create_account, create_account_mapping, open_fiscal_year
from apps.kitchen.models import Recipe, RecipeServing, RecipeVersion
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_organization_access
from apps.sales.day_services import add_sales_line, create_sales_day, submit_sales_day
from apps.sales.models import (
    ApplicationReceivableEntry,
    CommissionBasis,
    DeliveryApplication,
    DeliveryApplicationSettlement,
    DeliveryApplicationSettlementAdjustment,
    DeliveryApplicationSettlementAllocation,
    MenuItem,
    ReceivableSource,
    SalesChannel,
    SalesChannelCategory,
    SalesDay,
    SettlementAdjustmentReason,
    SettlementRemittance,
    SettlementStatus,
    SettlementVarianceLeg,
)
from apps.sales.posting import post_sales_day
from apps.sales.receivables import positions_for, running_balance, unallocated_debit
from apps.sales.selectors import receivable_balance
from apps.sales.services import (
    create_delivery_agreement,
    create_delivery_application,
    create_menu_item,
    create_menu_price,
    create_sales_channel,
    set_application_branch_setting,
    set_branch_availability,
)
from apps.sales.settlement_posting import (
    SOURCE_DOCUMENT_TYPE,
    post_settlement,
    reverse_settlement,
)
from apps.sales.settlement_services import (
    accrued_commission_for,
    add_settlement_adjustment,
    allocate_entry,
    create_settlement,
    open_entries_for,
    reconcile_settlement,
    remove_allocation,
    return_settlement_to_draft,
    settled_days_for,
    three_way_for,
)
from apps.users.models import User

BUSINESS_DATE = datetime.date(2026, 8, 10)
PERIOD_START = datetime.date(2026, 8, 1)
PERIOD_END = datetime.date(2026, 8, 31)
REMITTANCE_DATE = datetime.date(2026, 9, 5)
JANUARY = datetime.date(2026, 1, 1)

#: The receivable one posted application day of two plates produces:
#: gross 20,000 less 15% commission.
EXPECTED_RECEIVABLE = Decimal("17000.000")

#: The chart this journal reaches. `1-03-01-001` — settlement receipts through
#: the bank — and `7-09-05-001` — the settlement variance — are the two this
#: checkpoint adds. The variance sits in class 7 rather than class 6 because it
#: is **bidirectional**: a debit when the application short-paid and a credit
#: when it over-paid.
_CHART: tuple[tuple[str, str, str], ...] = (
    ("1", "الأصول", "Assets"),
    ("1-01", "النقد", "Cash"),
    ("1-01-01", "الصناديق", "Cash boxes"),
    ("1-01-01-001", "الصندوق", "Cash"),
    ("1-02", "الذمم", "Receivables"),
    ("1-02-01", "ذمم التطبيقات", "App receivables"),
    ("1-02-01-001", "ذمم تطبيقات التوصيل", "App Receivable"),
    ("1-03", "المصارف", "Banks"),
    ("1-03-01", "تحصيلات التطبيقات", "Settlement receipts"),
    ("1-03-01-001", "تحصيلات التطبيقات عبر المصرف", "Settlement Bank"),
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
    ("7", "الفروقات", "Differences"),
    ("7-09", "فروقات التسويات", "Settlement differences"),
    ("7-09-05", "فروقات تطبيقات التوصيل", "Delivery differences"),
    ("7-09-05-001", "فروقات تسويات التطبيقات", "Settlement Variance"),
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
        SALES_SETTLEMENT_BANK: "1-03-01-001",
        DELIVERY_APP_RECEIVABLE: "1-02-01-001",
        DELIVERY_COMMISSION_EXPENSE: "6-03-01-001",
        DELIVERY_SETTLEMENT_VARIANCE: "7-09-05-001",
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
    serving = version.servings.get()
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
def app_channel(organization: Organization, delivery_cost_center: CostCenter) -> SalesChannel:
    return create_sales_channel(
        organization=organization,
        code="APPS",
        name="تطبيقات",
        category=SalesChannelCategory.DELIVERY_APPLICATION,
        cost_center=delivery_cost_center,
    )


@pytest.fixture
def application(organization: Organization, branch: Branch) -> DeliveryApplication:
    app = create_delivery_application(
        organization=organization, code="DEMO-APP", name="تطبيق تجريبي"
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


def _posted_application_day(
    organization: Organization,
    branch: Branch,
    menu_item: MenuItem,
    app_channel: SalesChannel,
    application: DeliveryApplication,
    manager: User,
    accounting_manager: User,
    business_date: datetime.date = BUSINESS_DATE,
) -> SalesDay:
    day = create_sales_day(
        organization=organization, branch=branch, business_date=business_date, actor=manager
    )
    add_sales_line(
        day=day,
        menu_item=menu_item,
        channel=app_channel,
        delivery_application=application,
        quantity=Decimal("2.000"),
        order_count=2,
    )
    submit_sales_day(day=day, actor=manager)
    return post_sales_day(day=day, actor=accounting_manager)


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
    return _posted_application_day(
        organization,
        branch,
        menu_item,
        app_channel,
        application,
        manager,
        accounting_manager,
    )


@pytest.fixture
def sale_entry(application_day: SalesDay) -> ApplicationReceivableEntry:
    """The one `SALE_POSTED` debit the day produced."""
    return ApplicationReceivableEntry.objects.get(source=ReceivableSource.SALE_POSTED)


def _settlement(
    organization: Organization,
    branch: Branch,
    application: DeliveryApplication,
    actor: User,
    *,
    statement_amount: Decimal = EXPECTED_RECEIVABLE,
    remitted_amount: Decimal = EXPECTED_RECEIVABLE,
    statement_commission_amount: Decimal = Decimal("3000.000"),
    reference: str = "ST-2026-08",
    destination: str = SettlementRemittance.BANK,
) -> DeliveryApplicationSettlement:
    return create_settlement(
        organization=organization,
        branch=branch,
        delivery_application=application,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        business_date=REMITTANCE_DATE,
        statement_reference=reference,
        statement_date=PERIOD_END,
        statement_amount=statement_amount,
        remitted_amount=remitted_amount,
        statement_commission_amount=statement_commission_amount,
        remittance_destination=destination,
        evidence_reference="إشعار مصرفي ٤٤",
        actor=actor,
    )


def _lines_by_code(entry: JournalEntry) -> dict[str, JournalLine]:
    return {line.account.code: line for line in entry.lines.select_related("account")}


def _settlement_entry(settlement: DeliveryApplicationSettlement) -> JournalEntry:
    return JournalEntry.objects.get(
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(settlement.public_id),
    )


# ---------------------------------------------------------------------------
# Allocations bind to entries, not to a period total
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAllocationsBindToEntries:
    def test_the_open_entries_are_the_applications_own_debits(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        application: DeliveryApplication,
    ) -> None:
        entries = list(
            open_entries_for(
                delivery_application=application,
                organization_id=organization.pk,
                up_to=PERIOD_END,
            )
        )
        assert entries == [sale_entry]
        assert sale_entry.debit == EXPECTED_RECEIVABLE

    def test_an_allocation_names_the_day_it_paid_for(
        self,
        application_day: SalesDay,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """
        ADR-028 §6's whole point. A settlement crediting "the balance as at the
        31st" could not answer this question, and the first disputed order
        would be unanswerable.
        """
        settlement = _settlement(organization, branch, application, accounting_manager)
        allocate_entry(
            settlement=settlement,
            receivable_entry=sale_entry,
            allocated_amount=EXPECTED_RECEIVABLE,
            actor=accounting_manager,
        )
        assert settled_days_for(settlement) == [application_day]

    def test_an_allocation_larger_than_the_entry_is_refused_with_a_sentence(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _settlement(organization, branch, application, accounting_manager)
        with pytest.raises(ValidationError) as caught:
            allocate_entry(
                settlement=settlement,
                receivable_entry=sale_entry,
                allocated_amount=EXPECTED_RECEIVABLE + Decimal("1.000"),
                actor=accounting_manager,
            )
        assert caught.value.code == "allocation_exceeds_the_entry"

    def test_an_entry_after_the_period_cannot_be_allocated(
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
        """A statement cannot pay for a sale that had not happened when it was issued."""
        later = _posted_application_day(
            organization,
            branch,
            menu_item,
            app_channel,
            application,
            manager,
            accounting_manager,
            business_date=datetime.date(2026, 9, 20),
        )
        entry = ApplicationReceivableEntry.objects.get(
            source=ReceivableSource.SALE_POSTED, business_date=later.business_date
        )
        settlement = _settlement(organization, branch, application, accounting_manager)
        with pytest.raises(ValidationError) as caught:
            allocate_entry(
                settlement=settlement,
                receivable_entry=entry,
                allocated_amount=Decimal("100.000"),
                actor=accounting_manager,
            )
        assert caught.value.code == "entry_after_the_period"


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------


def _reconciled(
    organization: Organization,
    branch: Branch,
    application: DeliveryApplication,
    entry: ApplicationReceivableEntry,
    actor: User,
    *,
    statement_amount: Decimal = EXPECTED_RECEIVABLE,
    remitted_amount: Decimal = EXPECTED_RECEIVABLE,
    claims: tuple[tuple[str, str, Decimal], ...] = (),
    reference: str = "ST-2026-08",
    destination: str = SettlementRemittance.BANK,
) -> DeliveryApplicationSettlement:
    settlement = _settlement(
        organization,
        branch,
        application,
        actor,
        statement_amount=statement_amount,
        remitted_amount=remitted_amount,
        reference=reference,
        destination=destination,
    )
    allocate_entry(
        settlement=settlement,
        receivable_entry=entry,
        allocated_amount=EXPECTED_RECEIVABLE,
        actor=actor,
    )
    for leg, reason, amount in claims:
        add_settlement_adjustment(
            settlement=settlement,
            leg=leg,
            reason=reason,
            amount=amount,
            actor=actor,
        )
    return reconcile_settlement(settlement=settlement, actor=actor)


@pytest.mark.django_db
class TestTheSettlementJournal:
    def test_a_clean_settlement_debits_the_bank_and_credits_the_receivable(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        posted = post_settlement(settlement=settlement, actor=accounting_manager)

        assert posted.status == SettlementStatus.POSTED
        assert posted.number.startswith("AS-2026-")

        lines = _lines_by_code(_settlement_entry(posted))
        assert lines["1-03-01-001"].debit == EXPECTED_RECEIVABLE
        assert lines["1-02-01-001"].credit == EXPECTED_RECEIVABLE
        # Nothing was withheld, so there is no variance line at all.
        assert "7-09-05-001" not in lines

    def test_a_short_payment_recognises_the_variance_as_a_debit(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _reconciled(
            organization,
            branch,
            application,
            sale_entry,
            accounting_manager,
            remitted_amount=Decimal("16000.000"),
            claims=(
                (
                    SettlementVarianceLeg.REMITTANCE,
                    SettlementAdjustmentReason.WITHHOLDING_OR_OFFSET,
                    Decimal("1000.000"),
                ),
            ),
        )
        posted = post_settlement(settlement=settlement, actor=accounting_manager)

        lines = _lines_by_code(_settlement_entry(posted))
        assert lines["1-03-01-001"].debit == Decimal("16000.000")
        assert lines["7-09-05-001"].debit == Decimal("1000.000")
        assert lines["1-02-01-001"].credit == EXPECTED_RECEIVABLE
        assert sum(row.debit for row in lines.values()) == sum(row.credit for row in lines.values())

    def test_an_over_payment_recognises_the_variance_as_a_credit(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """
        Bidirectional by design. One account, because the leg and the reason on
        the document already say where the difference arose.
        """
        settlement = _reconciled(
            organization,
            branch,
            application,
            sale_entry,
            accounting_manager,
            remitted_amount=Decimal("17500.000"),
            claims=(
                (
                    SettlementVarianceLeg.REMITTANCE,
                    SettlementAdjustmentReason.ROUNDING,
                    Decimal("-500.000"),
                ),
            ),
        )
        posted = post_settlement(settlement=settlement, actor=accounting_manager)

        lines = _lines_by_code(_settlement_entry(posted))
        assert lines["1-03-01-001"].debit == Decimal("17500.000")
        assert lines["7-09-05-001"].credit == Decimal("500.000")
        assert lines["1-02-01-001"].credit == EXPECTED_RECEIVABLE

    def test_a_cash_remittance_lands_in_the_till_and_not_the_bank(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _reconciled(
            organization,
            branch,
            application,
            sale_entry,
            accounting_manager,
            destination=SettlementRemittance.CASH,
        )
        posted = post_settlement(settlement=settlement, actor=accounting_manager)
        lines = _lines_by_code(_settlement_entry(posted))
        assert lines["1-01-01-001"].debit == EXPECTED_RECEIVABLE
        assert "1-03-01-001" not in lines

    def test_a_fully_offset_statement_posts_no_remittance_line(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """A remitted amount of zero is legal, and simply omits that line."""
        settlement = _reconciled(
            organization,
            branch,
            application,
            sale_entry,
            accounting_manager,
            statement_amount=Decimal("0.000"),
            remitted_amount=Decimal("0.000"),
            claims=(
                (
                    SettlementVarianceLeg.STATEMENT,
                    SettlementAdjustmentReason.PENALTY_OR_CHARGEBACK,
                    EXPECTED_RECEIVABLE,
                ),
            ),
        )
        posted = post_settlement(settlement=settlement, actor=accounting_manager)
        lines = _lines_by_code(_settlement_entry(posted))
        assert "1-03-01-001" not in lines
        assert lines["7-09-05-001"].debit == EXPECTED_RECEIVABLE
        assert lines["1-02-01-001"].credit == EXPECTED_RECEIVABLE


@pytest.mark.django_db
class TestCommissionIsNeverRecognisedTwice:
    """
    The single most likely error in the whole module, tested directly.

    Commission was accrued at the sale (ADR-028 §4). Expensing it again at
    settlement would overstate selling expense and understate gross margin by
    the same amount — both individually defensible, which is precisely why
    nobody finds it.
    """

    def test_the_journal_contains_no_commission_line(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        posted = post_settlement(settlement=settlement, actor=accounting_manager)
        lines = _lines_by_code(_settlement_entry(posted))
        assert "6-03-01-001" not in lines
        assert not [code for code in lines if code.startswith("6-")]

    def test_a_disputed_commission_rate_is_a_variance_and_not_an_expense(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """
        The application charged 20% where the agreement says 15%. The extra
        1,000 is claimed on the statement leg and reaches
        `DELIVERY_SETTLEMENT_VARIANCE` — never a second debit to commission.
        """
        settlement = _settlement(
            organization,
            branch,
            application,
            accounting_manager,
            statement_amount=Decimal("16000.000"),
            remitted_amount=Decimal("16000.000"),
            statement_commission_amount=Decimal("4000.000"),
        )
        allocate_entry(
            settlement=settlement,
            receivable_entry=sale_entry,
            allocated_amount=EXPECTED_RECEIVABLE,
            actor=accounting_manager,
        )
        comparison = three_way_for(settlement)
        assert comparison.accrued_commission == Decimal("3000.000")
        assert comparison.statement_commission == Decimal("4000.000")
        assert comparison.commission_gap == Decimal("-1000.000")

        add_settlement_adjustment(
            settlement=settlement,
            leg=SettlementVarianceLeg.STATEMENT,
            reason=SettlementAdjustmentReason.COMMISSION_RATE_DIFFERENCE,
            amount=Decimal("1000.000"),
            actor=accounting_manager,
        )
        reconcile_settlement(settlement=settlement, actor=accounting_manager)
        posted = post_settlement(settlement=settlement, actor=accounting_manager)

        lines = _lines_by_code(_settlement_entry(posted))
        assert "6-03-01-001" not in lines
        assert lines["7-09-05-001"].debit == Decimal("1000.000")

    def test_the_accrual_is_read_from_the_sales_lines(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """Read from what was actually charged, never re-rated from the agreement."""
        settlement = _settlement(organization, branch, application, accounting_manager)
        assert accrued_commission_for(settlement) == Decimal("0")
        allocate_entry(
            settlement=settlement,
            receivable_entry=sale_entry,
            allocated_amount=EXPECTED_RECEIVABLE,
            actor=accounting_manager,
        )
        assert accrued_commission_for(settlement) == Decimal("3000.000")


# ---------------------------------------------------------------------------
# The receivable ledger
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheReceivableLedger:
    def test_posting_credits_the_receivable_and_the_balance_falls_to_zero(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        assert (
            receivable_balance(
                delivery_application_id=application.pk, organization_id=organization.pk
            )
            == EXPECTED_RECEIVABLE
        )

        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        posted = post_settlement(settlement=settlement, actor=accounting_manager)

        entry = ApplicationReceivableEntry.objects.get(source=ReceivableSource.SETTLEMENT)
        assert entry.credit == EXPECTED_RECEIVABLE
        assert entry.source_document_id == str(posted.public_id)
        assert receivable_balance(
            delivery_application_id=application.pk, organization_id=organization.pk
        ) == Decimal("0.000")

    def test_the_reversal_mirrors_it_with_the_paired_source_and_no_suffix(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """
        Unlike an adjustment reversal, this one has a paired `source` value in
        ADR-027 §5's closed vocabulary, so the document id needs no suffix.
        """
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        posted = post_settlement(settlement=settlement, actor=accounting_manager)
        reverse_settlement(settlement=posted, actor=accounting_manager, reason="حُوِّل المبلغ خطأً")

        mirror = ApplicationReceivableEntry.objects.get(source=ReceivableSource.SETTLEMENT_REVERSED)
        assert mirror.debit == EXPECTED_RECEIVABLE
        assert mirror.source_document_id == str(posted.public_id)
        assert (
            receivable_balance(
                delivery_application_id=application.pk, organization_id=organization.pk
            )
            == EXPECTED_RECEIVABLE
        )

    def test_a_reversal_returns_the_allocated_entry_to_open(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """
        The allocation rows stay — they are evidence of what was claimed — and
        the entry becomes claimable again because every count is restricted to
        posted settlements.
        """
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        posted = post_settlement(settlement=settlement, actor=accounting_manager)
        assert unallocated_debit(sale_entry) == Decimal("0.000")

        reverse_settlement(settlement=posted, actor=accounting_manager, reason="خطأ")
        assert posted.allocations.count() == 1
        assert unallocated_debit(sale_entry) == EXPECTED_RECEIVABLE

    def test_the_running_balance_follows_the_movements(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        post_settlement(settlement=settlement, actor=accounting_manager)
        rows = ApplicationReceivableEntry.objects.filter(delivery_application=application).order_by(
            "business_date", "pk"
        )
        balances = [balance for _entry, balance in running_balance(rows)]
        assert balances == [EXPECTED_RECEIVABLE, Decimal("0.000")]


# ---------------------------------------------------------------------------
# The two leg equations
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUnexplainedVarianceBlocks:
    def test_an_unexplained_statement_gap_blocks_reconciliation(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _settlement(
            organization,
            branch,
            application,
            accounting_manager,
            statement_amount=Decimal("16000.000"),
            remitted_amount=Decimal("16000.000"),
        )
        allocate_entry(
            settlement=settlement,
            receivable_entry=sale_entry,
            allocated_amount=EXPECTED_RECEIVABLE,
            actor=accounting_manager,
        )
        comparison = three_way_for(settlement)
        assert comparison.statement_gap == Decimal("1000.000")
        assert comparison.unexplained_statement == Decimal("1000.000")
        assert not comparison.is_reconcilable

        with pytest.raises(ValidationError) as caught:
            reconcile_settlement(settlement=settlement, actor=accounting_manager)
        assert caught.value.code == "unexplained_variance"

    def test_an_unexplained_remittance_gap_blocks_reconciliation(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _settlement(
            organization,
            branch,
            application,
            accounting_manager,
            remitted_amount=Decimal("15000.000"),
        )
        allocate_entry(
            settlement=settlement,
            receivable_entry=sale_entry,
            allocated_amount=EXPECTED_RECEIVABLE,
            actor=accounting_manager,
        )
        assert three_way_for(settlement).unexplained_remittance == Decimal("2000.000")
        with pytest.raises(ValidationError) as caught:
            reconcile_settlement(settlement=settlement, actor=accounting_manager)
        assert caught.value.code == "unexplained_variance"

    def test_a_claim_on_the_wrong_leg_does_not_close_the_other(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """
        The two legs are the diagnosis. Claiming a remittance shortfall against
        the statement leg leaves both wrong, which is exactly what keeping them
        apart is for.
        """
        settlement = _settlement(
            organization,
            branch,
            application,
            accounting_manager,
            remitted_amount=Decimal("16000.000"),
        )
        allocate_entry(
            settlement=settlement,
            receivable_entry=sale_entry,
            allocated_amount=EXPECTED_RECEIVABLE,
            actor=accounting_manager,
        )
        add_settlement_adjustment(
            settlement=settlement,
            leg=SettlementVarianceLeg.STATEMENT,
            reason=SettlementAdjustmentReason.ROUNDING,
            amount=Decimal("1000.000"),
            actor=accounting_manager,
        )
        comparison = three_way_for(settlement)
        assert comparison.unexplained_statement == Decimal("-1000.000")
        assert comparison.unexplained_remittance == Decimal("1000.000")
        assert not comparison.is_reconcilable

    def test_posting_re_checks_both_legs_under_the_lock(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """
        A settlement that reconciled last Tuesday is not evidence about what it
        says today. Returning it to draft, withdrawing the claim and forcing the
        status back is the shell-session version of the same drift.
        """
        settlement = _reconciled(
            organization,
            branch,
            application,
            sale_entry,
            accounting_manager,
            remitted_amount=Decimal("16000.000"),
            claims=(
                (
                    SettlementVarianceLeg.REMITTANCE,
                    SettlementAdjustmentReason.WITHHOLDING_OR_OFFSET,
                    Decimal("1000.000"),
                ),
            ),
        )
        return_settlement_to_draft(
            settlement=settlement, actor=accounting_manager, reason="سحب التفسير"
        )
        settlement.refresh_from_db()
        settlement.adjustments.all().delete()
        DeliveryApplicationSettlement.objects.filter(pk=settlement.pk).update(
            status=SettlementStatus.RECONCILED,
            reconciled_by=accounting_manager,
            reconciled_at=timezone.now(),
        )
        settlement.refresh_from_db()

        with pytest.raises(ValidationError) as caught:
            post_settlement(settlement=settlement, actor=accounting_manager)
        assert caught.value.code == "unexplained_variance"

    def test_the_allocations_must_still_equal_the_reconciled_figure(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """
        `Σ allocations = receivable cleared`, ADR-028 §6's required equality,
        checked where it matters: the journal credits the stamp.
        """
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        return_settlement_to_draft(
            settlement=settlement, actor=accounting_manager, reason="سحب التخصيص"
        )
        settlement.refresh_from_db()
        remove_allocation(allocation=settlement.allocations.get(), actor=accounting_manager)
        DeliveryApplicationSettlement.objects.filter(pk=settlement.pk).update(
            status=SettlementStatus.RECONCILED,
            reconciled_by=accounting_manager,
            reconciled_at=timezone.now(),
        )
        settlement.refresh_from_db()

        with pytest.raises(ValidationError) as caught:
            post_settlement(settlement=settlement, actor=accounting_manager)
        assert caught.value.code == "allocations_do_not_match"

    def test_the_escape_hatch_needs_words_and_an_approver(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _settlement(
            organization,
            branch,
            application,
            accounting_manager,
            remitted_amount=Decimal("16000.000"),
        )
        allocate_entry(
            settlement=settlement,
            receivable_entry=sale_entry,
            allocated_amount=EXPECTED_RECEIVABLE,
            actor=accounting_manager,
        )
        with pytest.raises(ValidationError) as no_words:
            add_settlement_adjustment(
                settlement=settlement,
                leg=SettlementVarianceLeg.REMITTANCE,
                reason=SettlementAdjustmentReason.UNEXPLAINED_APPROVED,
                amount=Decimal("1000.000"),
                actor=accounting_manager,
                approver=accounting_manager,
            )
        assert no_words.value.code == "explanation_required"

        with pytest.raises(ValidationError) as no_approver:
            add_settlement_adjustment(
                settlement=settlement,
                leg=SettlementVarianceLeg.REMITTANCE,
                reason=SettlementAdjustmentReason.UNEXPLAINED_APPROVED,
                amount=Decimal("1000.000"),
                explanation="لم يوضّح التطبيق الحسم",
                actor=accounting_manager,
            )
        assert no_approver.value.code == "approver_required"

        add_settlement_adjustment(
            settlement=settlement,
            leg=SettlementVarianceLeg.REMITTANCE,
            reason=SettlementAdjustmentReason.UNEXPLAINED_APPROVED,
            amount=Decimal("1000.000"),
            explanation="لم يوضّح التطبيق الحسم",
            actor=accounting_manager,
            approver=accounting_manager,
        )
        reconciled = reconcile_settlement(settlement=settlement, actor=accounting_manager)
        assert reconciled.status == SettlementStatus.RECONCILED


# ---------------------------------------------------------------------------
# The database guards
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheDatabaseHoldsTheLine:
    def test_a_reconciled_settlements_figures_are_frozen(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """A statement that can be edited after it is declared is not one."""
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        settlement.statement_amount = Decimal("1.000")
        with pytest.raises(Exception, match="reconciled"), transaction.atomic():
            settlement.save(update_fields=["statement_amount"])

    def test_a_posted_settlement_is_frozen(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        posted = post_settlement(settlement=settlement, actor=accounting_manager)
        posted.statement_reference = "شيء آخر"
        with pytest.raises(Exception, match="frozen"), transaction.atomic():
            posted.save(update_fields=["statement_reference"])

    def test_a_posted_settlements_allocations_cannot_be_changed(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        posted = post_settlement(settlement=settlement, actor=accounting_manager)
        allocation = posted.allocations.get()
        allocation.allocated_amount = Decimal("1.000")
        with pytest.raises(Exception, match="draft"), transaction.atomic():
            allocation.save(update_fields=["allocated_amount"])

    def test_over_allocating_an_entry_is_refused_by_the_database(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """
        A raw `INSERT` walks straight past a service check, so this rule is a
        trigger. "The receivable was paid twice" is the failure it stops, and it
        surfaces mid-argument with the counterparty months later.
        """
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        post_settlement(settlement=settlement, actor=accounting_manager)

        second = _settlement(
            organization, branch, application, accounting_manager, reference="ST-2026-08-B"
        )
        with (
            pytest.raises(Exception, match="more than a receivable entry owes"),
            transaction.atomic(),
        ):
            DeliveryApplicationSettlementAllocation.objects.create(
                settlement=second,
                receivable_entry=sale_entry,
                allocated_amount=Decimal("1.000"),
            )

    def test_a_credit_entry_cannot_be_allocated(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """A credit entry is a payment, not something to be paid."""
        credit = ApplicationReceivableEntry.objects.create(
            organization=organization,
            branch=branch,
            delivery_application=application,
            business_date=BUSINESS_DATE,
            source=ReceivableSource.AUTHORIZED_ADJUSTMENT,
            source_document_type="SALES.SALESADJUSTMENT",
            source_document_id="not-a-real-document",
            credit=Decimal("500.000"),
        )
        settlement = _settlement(organization, branch, application, accounting_manager)
        with pytest.raises(Exception, match="not something to be paid"), transaction.atomic():
            DeliveryApplicationSettlementAllocation.objects.create(
                settlement=settlement,
                receivable_entry=credit,
                allocated_amount=Decimal("100.000"),
            )

    def test_an_unexplained_claim_without_words_is_refused_by_a_constraint(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _settlement(organization, branch, application, accounting_manager)
        with pytest.raises(IntegrityError, match="unexplained_needs_words"), transaction.atomic():
            DeliveryApplicationSettlementAdjustment.objects.create(
                settlement=settlement,
                leg=SettlementVarianceLeg.REMITTANCE,
                reason=SettlementAdjustmentReason.UNEXPLAINED_APPROVED,
                amount=Decimal("100.000"),
                explanation="",
                approved_by=accounting_manager,
                approved_at=timezone.now(),
            )

    def test_an_unexplained_claim_without_an_approver_is_refused_by_a_constraint(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _settlement(organization, branch, application, accounting_manager)
        with (
            pytest.raises(IntegrityError, match="unexplained_needs_an_approver"),
            transaction.atomic(),
        ):
            DeliveryApplicationSettlementAdjustment.objects.create(
                settlement=settlement,
                leg=SettlementVarianceLeg.REMITTANCE,
                reason=SettlementAdjustmentReason.UNEXPLAINED_APPROVED,
                amount=Decimal("100.000"),
                explanation="لا تفسير من الطرف الآخر",
            )

    def test_one_settlement_per_statement_reference(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        """
        Paying one statement twice is the failure this prevents.

        `full_clean` reaches the uniqueness first and answers with a sentence,
        which is what a screen needs; the unique constraint behind it is what
        catches the same duplicate arriving through a shell or an import.
        """
        _settlement(organization, branch, application, accounting_manager)
        with pytest.raises(ValidationError, match="already exists"), transaction.atomic():
            _settlement(organization, branch, application, accounting_manager)
        assert (
            DeliveryApplicationSettlement.objects.filter(
                delivery_application=application, statement_reference="ST-2026-08"
            ).count()
            == 1
        )

    def test_the_unique_constraint_catches_a_duplicate_that_skips_the_service(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        first = _settlement(organization, branch, application, accounting_manager)
        with (
            pytest.raises(IntegrityError, match="statement_unique_per_application"),
            transaction.atomic(),
        ):
            DeliveryApplicationSettlement.objects.create(
                organization=organization,
                branch=branch,
                delivery_application=application,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                business_date=REMITTANCE_DATE,
                statement_reference=first.statement_reference,
                statement_date=PERIOD_END,
                statement_amount=Decimal("1.000"),
                remitted_amount=Decimal("1.000"),
                evidence_reference="مكرر",
            )

    def test_a_reversed_settlement_cannot_be_posted_again(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        posted = post_settlement(settlement=settlement, actor=accounting_manager)
        reverse_settlement(settlement=posted, actor=accounting_manager, reason="خطأ")
        with pytest.raises(ValidationError) as caught:
            post_settlement(settlement=posted, actor=accounting_manager)
        assert caught.value.code == "settlement_reversed"


# ---------------------------------------------------------------------------
# Aging
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAging:
    def test_the_buckets_are_derived_from_the_entries_every_time(
        self,
        chart: dict[str, str],
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
    ) -> None:
        as_of = datetime.date(2026, 12, 31)
        for days, amount in ((10, "1000.000"), (45, "2000.000"), (75, "3000.000"), (200, "4000")):
            ApplicationReceivableEntry.objects.create(
                organization=organization,
                branch=branch,
                delivery_application=application,
                business_date=as_of - datetime.timedelta(days=days),
                source=ReceivableSource.SALE_POSTED,
                source_document_type="SALES.SALESDAY",
                source_document_id=f"aging-{days}",
                debit=Decimal(amount),
            )
        positions = positions_for(
            _viewer(organization), organization_id=organization.pk, as_of=as_of
        )
        buckets = {bucket.label: bucket.amount for bucket in positions[0].buckets}
        assert buckets == {
            "0-30": Decimal("1000.000"),
            "31-60": Decimal("2000.000"),
            "61-90": Decimal("3000.000"),
            "90+": Decimal("4000.000"),
        }

    def test_a_payment_clears_the_oldest_debt_first(
        self,
        chart: dict[str, str],
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
    ) -> None:
        """
        FIFO, not proportional. A company that settled January and skipped
        February must show February as the open bucket — averaging the payment
        across both would erase the only signal the report exists to give.
        """
        as_of = datetime.date(2026, 12, 31)
        for days, amount, marker in ((200, "4000.000", "old"), (10, "1000.000", "new")):
            ApplicationReceivableEntry.objects.create(
                organization=organization,
                branch=branch,
                delivery_application=application,
                business_date=as_of - datetime.timedelta(days=days),
                source=ReceivableSource.SALE_POSTED,
                source_document_type="SALES.SALESDAY",
                source_document_id=f"fifo-{marker}",
                debit=Decimal(amount),
            )
        ApplicationReceivableEntry.objects.create(
            organization=organization,
            branch=branch,
            delivery_application=application,
            business_date=as_of,
            source=ReceivableSource.SETTLEMENT,
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id="fifo-payment",
            credit=Decimal("4000.000"),
        )
        positions = positions_for(
            _viewer(organization), organization_id=organization.pk, as_of=as_of
        )
        buckets = {bucket.label: bucket.amount for bucket in positions[0].buckets}
        assert buckets["90+"] == Decimal("0")
        assert buckets["0-30"] == Decimal("1000.000")
        assert positions[0].balance == Decimal("1000.000")

    def test_the_expected_settlement_date_follows_the_contract_cycle(
        self,
        chart: dict[str, str],
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
    ) -> None:
        as_of = datetime.date(2026, 12, 31)
        oldest = datetime.date(2026, 12, 1)
        ApplicationReceivableEntry.objects.create(
            organization=organization,
            branch=branch,
            delivery_application=application,
            business_date=oldest,
            source=ReceivableSource.SALE_POSTED,
            source_document_type="SALES.SALESDAY",
            source_document_id="cycle-1",
            debit=Decimal("5000.000"),
        )
        position = positions_for(
            _viewer(organization), organization_id=organization.pk, as_of=as_of
        )[0]
        assert position.oldest_open_date == oldest
        assert position.expected_settlement_date == oldest + datetime.timedelta(
            days=application.settlement_cycle_days
        )

    def test_an_application_with_no_movement_still_appears(
        self,
        chart: dict[str, str],
        organization: Organization,
        application: DeliveryApplication,
    ) -> None:
        """
        "Owes nothing" and "is not on the report" are different statements, and
        only the first one is checkable.
        """
        positions = positions_for(
            _viewer(organization),
            organization_id=organization.pk,
            as_of=datetime.date(2026, 12, 31),
        )
        assert [row.delivery_application for row in positions] == [application]
        assert positions[0].balance == Decimal("0")
        assert positions[0].oldest_open_date is None


def _viewer(organization: Organization) -> User:
    """Somebody who may read the ledger and nothing more."""
    user = User.objects.create_user(username="ledger-viewer", password="pw-not-real-1234")
    grant_organization_access(user=user, organization=organization, role=Role.VIEWER)
    return User.objects.get(pk=user.pk)


# ---------------------------------------------------------------------------
# Authorization and the screens
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthorization:
    def test_a_branch_manager_cannot_post_a_settlement(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        manager: User,
        accounting_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """
        `MANAGER` is deliberately excluded even though it holds
        `manage_sales_agreements`. The person who agreed the commission rate
        must not also be the person who agrees the statement that applies it.
        """
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        response = client_for(manager).post(f"/sales/settlements/{settlement.pk}/post/")
        assert response.status_code == 403
        assert (
            DeliveryApplicationSettlement.objects.get(pk=settlement.pk).status
            == SettlementStatus.RECONCILED
        )

    def test_a_cashier_cannot_reach_the_settlement_screens(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        cashier: User,
        accounting_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        response = client_for(cashier).post(f"/sales/settlements/{settlement.pk}/reconcile/")
        assert response.status_code == 403

    def test_an_outsider_gets_a_404_and_not_a_403(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        outsider: User,
        accounting_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """A 403 about another organization's record confirms it exists."""
        settlement = _settlement(organization, branch, application, accounting_manager)
        response = client_for(outsider).get(f"/sales/settlements/{settlement.pk}/")
        assert response.status_code == 404

    def test_an_outsider_cannot_open_another_organizations_ledger(
        self,
        sale_entry: ApplicationReceivableEntry,
        application: DeliveryApplication,
        outsider: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(outsider).get(f"/sales/receivables/{application.pk}/")
        assert response.status_code == 404

    def test_the_screens_answer_as_page_and_as_fragment(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        settlement = _settlement(organization, branch, application, accounting_manager)
        client = client_for(accounting_manager)
        paths = (
            "/sales/receivables/",
            f"/sales/receivables/{application.pk}/",
            "/sales/settlements/",
            "/sales/settlements/new/",
            f"/sales/settlements/{settlement.pk}/",
        )
        for path in paths:
            page = client.get(path)
            fragment = client.get(path, headers={"HX-Request": "true"})
            assert page.status_code == 200, path
            assert fragment.status_code == 200, path
            # An htmx fragment carrying a second shell renders correctly enough
            # to be missed in review and is wrong in every accessibility tree.
            assert b"<html" not in fragment.content.lower(), path

    def test_the_detail_shows_three_figures_and_never_one(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        settlement = _settlement(
            organization,
            branch,
            application,
            accounting_manager,
            remitted_amount=Decimal("16000.000"),
        )
        allocate_entry(
            settlement=settlement,
            receivable_entry=sale_entry,
            allocated_amount=EXPECTED_RECEIVABLE,
            actor=accounting_manager,
        )
        body = (
            client_for(accounting_manager)
            .get(f"/sales/settlements/{settlement.pk}/")
            .content.decode("utf-8")
        )
        assert "المستحق لدينا" in body
        assert "كشف التطبيق" in body
        assert "المحوَّل فعلاً" in body
        # Both residuals are stated, because the residual is the only number on
        # the page that decides whether the settlement may move.
        assert "غير المفسَّر" in body


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestReads:
    def test_a_settlement_carries_no_variance_field(self) -> None:
        """
        The variance is derived from three stored figures, never stored beside
        them: a stored net would be a fourth number that can disagree with the
        three it came from.
        """
        fields = {field.name for field in DeliveryApplicationSettlement._meta.get_fields()}
        for absent in ("variance", "variance_amount", "net_amount", "total_variance"):
            assert absent not in fields

    def test_the_expected_figure_is_stamped_at_reconciliation(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _settlement(organization, branch, application, accounting_manager)
        assert settlement.expected_amount == Decimal("0.000")
        allocate_entry(
            settlement=settlement,
            receivable_entry=sale_entry,
            allocated_amount=EXPECTED_RECEIVABLE,
            actor=accounting_manager,
        )
        assert DeliveryApplicationSettlement.objects.get(
            pk=settlement.pk
        ).expected_amount == Decimal("0.000")
        reconciled = reconcile_settlement(settlement=settlement, actor=accounting_manager)
        assert reconciled.expected_amount == EXPECTED_RECEIVABLE

    def test_a_settlement_with_no_allocations_cannot_reconcile(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _settlement(
            organization,
            branch,
            application,
            accounting_manager,
            statement_amount=Decimal("0.000"),
            remitted_amount=Decimal("0.000"),
        )
        with pytest.raises(ValidationError) as caught:
            reconcile_settlement(settlement=settlement, actor=accounting_manager)
        assert caught.value.code == "no_allocations"

    def test_returning_to_draft_needs_a_reason_and_clears_the_stamp(
        self,
        sale_entry: ApplicationReceivableEntry,
        organization: Organization,
        branch: Branch,
        application: DeliveryApplication,
        accounting_manager: User,
    ) -> None:
        settlement = _reconciled(organization, branch, application, sale_entry, accounting_manager)
        with pytest.raises(ValidationError) as caught:
            return_settlement_to_draft(settlement=settlement, actor=accounting_manager, reason="  ")
        assert caught.value.code == "reason_required"

        returned = return_settlement_to_draft(
            settlement=settlement, actor=accounting_manager, reason="رقم الكشف خاطئ"
        )
        assert returned.status == SettlementStatus.DRAFT
        assert returned.reconciled_by is None
        assert returned.reconciled_at is None
