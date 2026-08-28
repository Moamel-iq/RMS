"""End-to-end posting evidence for menu items fulfilled directly from stock."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from apps.accounting.models import (
    INVENTORY_CONSUMPTION,
    SALES_CASH_ON_HAND,
    SALES_DISCOUNT,
    SALES_RETURNS,
    SALES_REVENUE,
    Account,
    AccountRole,
    CostCenter,
    JournalEntry,
    SourceEvent,
)
from apps.accounting.services import create_account, create_account_mapping, open_fiscal_year
from apps.core.context import audit_context
from apps.inventory.ledger import MovementInput, post_stock_entry
from apps.inventory.models import (
    InventoryItem,
    ItemType,
    MovementType,
    StockBalance,
    StockLedgerEntry,
    Warehouse,
)
from apps.inventory.services import create_item, create_item_category, create_warehouse
from apps.organizations.models import Branch, Organization
from apps.sales.adjustment_posting import (
    SOURCE_DOCUMENT_TYPE as ADJUSTMENT_SOURCE_DOCUMENT_TYPE,
)
from apps.sales.adjustment_posting import (
    post_sales_adjustment,
    reverse_sales_adjustment,
)
from apps.sales.adjustment_services import add_adjustment_line, create_sales_adjustment
from apps.sales.day_services import add_sales_line, create_sales_day, submit_sales_day
from apps.sales.models import (
    DirectStockDisposition,
    FulfillmentSource,
    MenuItem,
    SalesAdjustment,
    SalesAdjustmentReasonKind,
    SalesAdjustmentStatus,
    SalesAdjustmentStockPosting,
    SalesChannel,
    SalesChannelCategory,
    SalesDay,
    SalesDayLine,
    SalesDayStatus,
    SalesDayStockPosting,
    SalesDirectStockFulfillment,
    SalesDirectStockReturnFulfillment,
    TenderDestination,
)
from apps.sales.posting import SOURCE_DOCUMENT_TYPE, post_sales_day, reverse_sales_day
from apps.sales.services import (
    create_menu_item,
    create_menu_price,
    create_sales_channel,
    set_branch_availability,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

BUSINESS_DATE = datetime.date(2026, 8, 10)
JANUARY = datetime.date(2026, 1, 1)
UNIT_PRICE = Decimal("10000")

_CHART: tuple[tuple[str, str, str], ...] = (
    ("1", "الأصول", "Assets"),
    ("1-01", "النقد", "Cash"),
    ("1-01-01", "الصناديق", "Cash boxes"),
    ("1-01-01-001", "الصندوق", "Cash"),
    ("1-03", "المخزون", "Inventory"),
    ("1-03-01", "مراقبة المخزون", "Inventory control"),
    ("1-03-01-001", "مخزون البضاعة", "Merchandise inventory"),
    ("4", "الإيرادات", "Revenue"),
    ("4-01", "المبيعات", "Sales"),
    ("4-01-01", "مبيعات مباشرة", "Direct sales"),
    ("4-01-01-001", "مبيعات الصالة", "Dine-in sales"),
    ("4-02", "خصومات المبيعات", "Sales discounts"),
    ("4-02-01", "خصم المطعم", "Restaurant discounts"),
    ("4-02-01-001", "خصومات المبيعات", "Sales discount"),
    ("4-03", "مردودات المبيعات", "Sales returns"),
    ("4-03-01", "مردودات", "Returns"),
    ("4-03-01-001", "مردودات المبيعات", "Sales returns"),
    ("5", "كلفة المبيعات", "Cost of sales"),
    ("5-01", "كلفة النشاط", "Operating cost"),
    ("5-01-02", "كلفة البضاعة المباعة", "Cost of goods sold"),
    ("5-01-02-001", "كلفة إعادة البيع", "Resale COGS"),
)


def _error_codes(error: ValidationError) -> set[str]:
    if hasattr(error, "error_dict"):
        return {
            item.code
            for errors in error.error_dict.values()
            for item in errors
            if item.code is not None
        }
    return {item.code for item in error.error_list if item.code is not None}


@pytest.fixture
def direct_stock_chart(organization: Organization) -> dict[str, Account]:
    for code, name, name in _CHART:
        create_account(
            organization=organization,
            code=code,
            name=name,
        )
    accounts = {
        account.code: account
        for account in Account.objects.filter(organization=organization, is_postable=True)
    }
    for role, code in (
        (SALES_CASH_ON_HAND, "1-01-01-001"),
        (SALES_REVENUE, "4-01-01-001"),
        (SALES_DISCOUNT, "4-02-01-001"),
        (SALES_RETURNS, "4-03-01-001"),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=role),
            account=accounts[code],
            effective_from=JANUARY,
        )
    open_fiscal_year(organization=organization, year=BUSINESS_DATE.year)
    return accounts


@pytest.fixture
def consumption_mapping(
    organization: Organization,
    direct_stock_chart: dict[str, Account],
) -> Account:
    account = direct_stock_chart["5-01-02-001"]
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_CONSUMPTION),
        account=account,
        effective_from=JANUARY,
    )
    return account


@pytest.fixture
def direct_item(organization: Organization) -> InventoryItem:
    call_command("seed_units", verbosity=0)
    from apps.units.models import UnitOfMeasure

    category = create_item_category(
        organization=organization,
        code="RESALE",
        name="بضاعة إعادة البيع",
    )
    return create_item(
        organization=organization,
        code="WATER-500",
        name="ماء 500 مل",
        category=category,
        item_type=ItemType.GOODS_FOR_RESALE,
        base_unit=UnitOfMeasure.objects.get(code="PIECE"),
    )


@pytest.fixture
def source_warehouse(branch: Branch) -> Warehouse:
    return create_warehouse(branch=branch, code="MAIN", name="المخزن الرئيسي")


@pytest.fixture
def direct_menu_item(
    organization: Organization,
    branch: Branch,
    direct_item: InventoryItem,
    source_warehouse: Warehouse,
) -> MenuItem:
    menu_item = create_menu_item(
        organization=organization,
        code="MENU-WATER-500",
        name="ماء 500 مل",
        fulfillment_source=FulfillmentSource.DIRECT_STOCK,
        inventory_item=direct_item,
        direct_stock_base_quantity=Decimal("1"),
    )
    set_branch_availability(
        item=menu_item,
        branch=branch,
        source_warehouse=source_warehouse,
    )
    create_menu_price(
        menu_item=menu_item,
        branch=branch,
        unit_price=UNIT_PRICE,
        effective_from=JANUARY,
    )
    return menu_item


@pytest.fixture
def hall_channel(organization: Organization, hall_cost_center: CostCenter) -> SalesChannel:
    return create_sales_channel(
        organization=organization,
        code="DINE-IN-DIRECT",
        name="الصالة",
        category=SalesChannelCategory.DINE_IN,
        cost_center=hall_cost_center,
        default_tender=TenderDestination.CASH,
    )


def _open_stock(
    *,
    organization: Organization,
    warehouse: Warehouse,
    item: InventoryItem,
    control_account: Account,
    actor: User,
    quantity: Decimal = Decimal("10"),
    unit_cost: Decimal = Decimal("2000"),
    effect_key: str = "direct-stock-opening",
) -> StockLedgerEntry:
    with audit_context(actor=actor):
        return post_stock_entry(
            organization=organization,
            effects=[
                MovementInput(
                    warehouse=warehouse,
                    item=item,
                    movement_type=MovementType.RECEIPT,
                    quantity=quantity,
                    effect_key=effect_key,
                    unit_cost=unit_cost,
                    control_account=control_account,
                )
            ],
            idempotency_key=effect_key,
            effective_at=timezone.now(),
            business_date=BUSINESS_DATE,
            source_document_type="TEST_OPENING",
            source_document_id=effect_key,
            source_event=SourceEvent.POSTED,
        )


def _submitted_day(
    *,
    organization: Organization,
    branch: Branch,
    menu_item: MenuItem,
    channel: SalesChannel,
    manager: User,
    quantity: Decimal,
) -> tuple[SalesDay, SalesDayLine]:
    day = create_sales_day(
        organization=organization,
        branch=branch,
        business_date=BUSINESS_DATE,
        actor=manager,
    )
    line = add_sales_line(
        day=day,
        menu_item=menu_item,
        channel=channel,
        quantity=quantity,
    )
    return submit_sales_day(day=day, actor=manager), line


def _posted_direct_sale(
    *,
    organization: Organization,
    branch: Branch,
    chart: dict[str, Account],
    item: InventoryItem,
    warehouse: Warehouse,
    menu_item: MenuItem,
    channel: SalesChannel,
    manager: User,
    accounting_manager: User,
    quantity: Decimal = Decimal("2"),
) -> SalesDay:
    _open_stock(
        organization=organization,
        warehouse=warehouse,
        item=item,
        control_account=chart["1-03-01-001"],
        actor=manager,
    )
    day, _line = _submitted_day(
        organization=organization,
        branch=branch,
        menu_item=menu_item,
        channel=channel,
        manager=manager,
        quantity=quantity,
    )
    return post_sales_day(day=day, actor=accounting_manager)


def _quantity_adjustment(
    *,
    day: SalesDay,
    actor: User,
    reason_kind: str,
    disposition: str,
    quantity: Decimal,
) -> SalesAdjustment:
    adjustment = create_sales_adjustment(
        sales_day=day,
        reason_kind=reason_kind,
        direct_stock_disposition=disposition,
        business_date=BUSINESS_DATE + datetime.timedelta(days=1),
        reason="اختبار إرجاع بيع مباشر",
        evidence_reference="DIRECT-RETURN-TEST",
        actor=actor,
    )
    add_adjustment_line(
        adjustment=adjustment,
        original_line=day.lines.get(),
        adjusted_quantity=quantity,
        actor=actor,
    )
    return adjustment


class TestDirectStockSalesDayPosting:
    def test_posting_issues_stock_and_puts_cogs_in_the_same_sales_journal(
        self,
        organization: Organization,
        branch: Branch,
        direct_stock_chart: dict[str, Account],
        consumption_mapping: Account,
        direct_item: InventoryItem,
        source_warehouse: Warehouse,
        direct_menu_item: MenuItem,
        hall_channel: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        _open_stock(
            organization=organization,
            warehouse=source_warehouse,
            item=direct_item,
            control_account=direct_stock_chart["1-03-01-001"],
            actor=manager,
        )
        day, unresolved_line = _submitted_day(
            organization=organization,
            branch=branch,
            menu_item=direct_menu_item,
            channel=hall_channel,
            manager=manager,
            quantity=Decimal("2"),
        )
        sales_line = day.lines.get(pk=unresolved_line.pk)
        assert sales_line.fulfillment_source == FulfillmentSource.DIRECT_STOCK
        assert sales_line.recipe_id is None
        assert sales_line.inventory_item == direct_item
        assert sales_line.source_warehouse == source_warehouse
        assert sales_line.direct_stock_qty_per_unit == Decimal("1.000000000000")
        assert sales_line.direct_stock_total_base_qty == Decimal("2.000")

        posted = post_sales_day(day=day, actor=accounting_manager)

        assert posted.status == SalesDayStatus.POSTED
        stock_link = SalesDayStockPosting.objects.select_related("stock_entry").get(
            sales_day=posted
        )
        journal = JournalEntry.objects.get(
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(posted.public_id),
            source_event=SourceEvent.POSTED,
        )
        assert stock_link.stock_entry.journal_entry == journal
        assert stock_link.stock_entry.source_document_type == SOURCE_DOCUMENT_TYPE
        assert stock_link.stock_entry.source_document_id == str(posted.public_id)
        assert stock_link.stock_entry.movements.count() == 1

        fulfillment = SalesDirectStockFulfillment.objects.select_related(
            "stock_movement",
            "consumption_account",
            "cost_center",
        ).get(sales_line=sales_line)
        assert fulfillment.stock_movement.entry == stock_link.stock_entry
        assert fulfillment.stock_movement.movement_type == MovementType.ISSUE
        assert fulfillment.stock_movement.base_quantity == Decimal("-2.000")
        assert fulfillment.stock_movement.inventory_value == Decimal("-4000.000")
        assert fulfillment.base_quantity == Decimal("2.000")
        assert fulfillment.cogs_value == Decimal("4000.000")
        assert fulfillment.consumption_account == consumption_mapping
        assert fulfillment.cost_center == hall_channel.cost_center

        journal_lines = {
            row.account.code: row for row in journal.lines.select_related("account", "cost_center")
        }
        assert journal_lines["1-01-01-001"].debit == Decimal("20000.000")
        assert journal_lines["4-01-01-001"].credit == Decimal("20000.000")
        assert journal_lines["5-01-02-001"].debit == Decimal("4000.000")
        assert journal_lines["5-01-02-001"].cost_center == hall_channel.cost_center
        assert journal_lines["1-03-01-001"].credit == Decimal("4000.000")
        assert sum(row.debit for row in journal_lines.values()) == sum(
            row.credit for row in journal_lines.values()
        )

        balance = StockBalance.objects.get(
            warehouse=source_warehouse,
            item=direct_item,
            lot=None,
        )
        assert balance.quantity == Decimal("8.000")
        assert balance.value == Decimal("16000.000")

    def test_missing_consumption_mapping_leaves_the_submitted_day_and_stock_untouched(
        self,
        organization: Organization,
        branch: Branch,
        direct_stock_chart: dict[str, Account],
        direct_item: InventoryItem,
        source_warehouse: Warehouse,
        direct_menu_item: MenuItem,
        hall_channel: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        _open_stock(
            organization=organization,
            warehouse=source_warehouse,
            item=direct_item,
            control_account=direct_stock_chart["1-03-01-001"],
            actor=manager,
        )
        day, _line = _submitted_day(
            organization=organization,
            branch=branch,
            menu_item=direct_menu_item,
            channel=hall_channel,
            manager=manager,
            quantity=Decimal("2"),
        )

        with pytest.raises(ValidationError) as caught:
            post_sales_day(day=day, actor=accounting_manager)

        assert "account_role_unmapped" in _error_codes(caught.value)
        day.refresh_from_db()
        assert day.status == SalesDayStatus.SUBMITTED
        assert day.number == ""
        assert not SalesDayStockPosting.objects.filter(sales_day=day).exists()
        assert not SalesDirectStockFulfillment.objects.filter(sales_line__sales_day=day).exists()
        assert not JournalEntry.objects.filter(
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(day.public_id),
        ).exists()
        assert not StockLedgerEntry.objects.filter(
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(day.public_id),
        ).exists()
        balance = StockBalance.objects.get(
            warehouse=source_warehouse,
            item=direct_item,
            lot=None,
        )
        assert balance.quantity == Decimal("10.000")
        assert balance.value == Decimal("20000.000")

    def test_insufficient_stock_rolls_back_number_stock_journal_and_evidence(
        self,
        organization: Organization,
        branch: Branch,
        direct_stock_chart: dict[str, Account],
        consumption_mapping: Account,
        direct_item: InventoryItem,
        source_warehouse: Warehouse,
        direct_menu_item: MenuItem,
        hall_channel: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        _open_stock(
            organization=organization,
            warehouse=source_warehouse,
            item=direct_item,
            control_account=direct_stock_chart["1-03-01-001"],
            actor=manager,
        )
        day, _line = _submitted_day(
            organization=organization,
            branch=branch,
            menu_item=direct_menu_item,
            channel=hall_channel,
            manager=manager,
            quantity=Decimal("11"),
        )

        with pytest.raises(ValidationError) as caught:
            post_sales_day(day=day, actor=accounting_manager)

        assert "insufficient_stock" in _error_codes(caught.value)
        day.refresh_from_db()
        assert day.status == SalesDayStatus.SUBMITTED
        assert day.number == ""
        assert not SalesDayStockPosting.objects.filter(sales_day=day).exists()
        assert not SalesDirectStockFulfillment.objects.filter(sales_line__sales_day=day).exists()
        assert not JournalEntry.objects.filter(
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(day.public_id),
        ).exists()
        assert not StockLedgerEntry.objects.filter(
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(day.public_id),
        ).exists()
        balance = StockBalance.objects.get(
            warehouse=source_warehouse,
            item=direct_item,
            lot=None,
        )
        assert balance.quantity == Decimal("10.000")
        assert balance.value == Decimal("20000.000")

    # Transactional, as `test_direct_stock_db_guards` is: posting and reversal
    # commit separately, so every deferred guard is checked at the moment it
    # was written for — while the document is still POSTED — rather than all
    # at teardown, after reversal has moved it on.
    @pytest.mark.django_db(transaction=True)
    def test_reversal_restores_the_original_issue_value_after_average_cost_changes(
        self,
        organization: Organization,
        branch: Branch,
        direct_stock_chart: dict[str, Account],
        consumption_mapping: Account,
        direct_item: InventoryItem,
        source_warehouse: Warehouse,
        direct_menu_item: MenuItem,
        hall_channel: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        _open_stock(
            organization=organization,
            warehouse=source_warehouse,
            item=direct_item,
            control_account=direct_stock_chart["1-03-01-001"],
            actor=manager,
        )
        day, _line = _submitted_day(
            organization=organization,
            branch=branch,
            menu_item=direct_menu_item,
            channel=hall_channel,
            manager=manager,
            quantity=Decimal("2"),
        )
        posted = post_sales_day(day=day, actor=accounting_manager)
        original_stock = SalesDayStockPosting.objects.get(sales_day=posted).stock_entry
        original_journal = JournalEntry.objects.get(
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id=str(posted.public_id),
            source_event=SourceEvent.POSTED,
        )

        # This receipt moves the current moving average from 2,000 to
        # 2,333.333333. Reversal must still restore the sale's exact 4,000,
        # not two units at today's average.
        _open_stock(
            organization=organization,
            warehouse=source_warehouse,
            item=direct_item,
            control_account=direct_stock_chart["1-03-01-001"],
            actor=manager,
            quantity=Decimal("1"),
            unit_cost=Decimal("5000"),
            effect_key="direct-stock-later-receipt",
        )

        reversed_day = reverse_sales_day(
            day=posted,
            actor=accounting_manager,
            reason="عكس اختبار البيع المباشر",
        )

        assert reversed_day.status == SalesDayStatus.REVERSED
        stock_reversal = StockLedgerEntry.objects.get(
            reverses=original_stock,
            source_event=SourceEvent.REVERSED,
        )
        journal_reversal = JournalEntry.objects.get(
            reverses=original_journal,
            source_event=SourceEvent.REVERSED,
        )
        assert stock_reversal.journal_entry == journal_reversal
        reversing_movement = stock_reversal.movements.get()
        assert reversing_movement.movement_type == MovementType.REVERSAL
        assert reversing_movement.base_quantity == Decimal("2.000")
        assert reversing_movement.inventory_value == Decimal("4000.000")

        balance = StockBalance.objects.get(
            warehouse=source_warehouse,
            item=direct_item,
            lot=None,
        )
        assert balance.quantity == Decimal("11.000")
        assert balance.value == Decimal("25000.000")

        original_by_account = {
            row.account_id: (row.debit, row.credit) for row in original_journal.lines.all()
        }
        reversed_by_account = {
            row.account_id: (row.debit, row.credit) for row in journal_reversal.lines.all()
        }
        assert set(reversed_by_account) == set(original_by_account)
        for account_id, (debit, credit) in original_by_account.items():
            reversed_debit, reversed_credit = reversed_by_account[account_id]
            assert reversed_debit == credit
            assert reversed_credit == debit


class TestDirectStockSalesAdjustments:
    def test_restock_return_restores_stock_and_reverses_exact_cogs_in_one_journal(
        self,
        organization: Organization,
        branch: Branch,
        direct_stock_chart: dict[str, Account],
        consumption_mapping: Account,
        direct_item: InventoryItem,
        source_warehouse: Warehouse,
        direct_menu_item: MenuItem,
        hall_channel: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        day = _posted_direct_sale(
            organization=organization,
            branch=branch,
            chart=direct_stock_chart,
            item=direct_item,
            warehouse=source_warehouse,
            menu_item=direct_menu_item,
            channel=hall_channel,
            manager=manager,
            accounting_manager=accounting_manager,
        )
        source_fulfillment = SalesDirectStockFulfillment.objects.get(sales_line=day.lines.get())
        adjustment = _quantity_adjustment(
            day=day,
            actor=manager,
            reason_kind=SalesAdjustmentReasonKind.RETURNED_AFTER_FULFILLMENT,
            disposition=DirectStockDisposition.RESTOCK,
            quantity=Decimal("2"),
        )

        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)

        assert posted.status == SalesAdjustmentStatus.POSTED
        stock_link = SalesAdjustmentStockPosting.objects.select_related("stock_entry").get(
            adjustment=posted
        )
        journal = JournalEntry.objects.get(
            source_document_type=ADJUSTMENT_SOURCE_DOCUMENT_TYPE,
            source_document_id=str(posted.public_id),
            source_event=SourceEvent.POSTED,
        )
        assert stock_link.stock_entry.journal_entry == journal

        movement = stock_link.stock_entry.movements.get()
        assert movement.movement_type == MovementType.RETURN_IN
        assert movement.base_quantity == Decimal("2.000")
        assert movement.inventory_value == Decimal("4000.000")

        evidence = SalesDirectStockReturnFulfillment.objects.get(adjustment_line=posted.lines.get())
        assert evidence.source_fulfillment == source_fulfillment
        assert evidence.stock_movement == movement
        assert evidence.base_quantity == source_fulfillment.base_quantity == Decimal("2.000")
        assert evidence.cogs_value == source_fulfillment.cogs_value == Decimal("4000.000")

        lines = {row.account.code: row for row in journal.lines.select_related("account")}
        assert lines["4-03-01-001"].debit == Decimal("20000.000")
        assert lines["1-01-01-001"].credit == Decimal("20000.000")
        assert lines["1-03-01-001"].debit == Decimal("4000.000")
        assert lines["5-01-02-001"].credit == Decimal("4000.000")
        assert sum(row.debit for row in lines.values()) == sum(row.credit for row in lines.values())

        balance = StockBalance.objects.get(
            warehouse=source_warehouse,
            item=direct_item,
            lot=None,
        )
        assert balance.quantity == Decimal("10.000")
        assert balance.value == Decimal("20000.000")

    def test_no_restock_return_leaves_physical_stock_and_cogs_untouched(
        self,
        organization: Organization,
        branch: Branch,
        direct_stock_chart: dict[str, Account],
        consumption_mapping: Account,
        direct_item: InventoryItem,
        source_warehouse: Warehouse,
        direct_menu_item: MenuItem,
        hall_channel: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        day = _posted_direct_sale(
            organization=organization,
            branch=branch,
            chart=direct_stock_chart,
            item=direct_item,
            warehouse=source_warehouse,
            menu_item=direct_menu_item,
            channel=hall_channel,
            manager=manager,
            accounting_manager=accounting_manager,
        )
        adjustment = _quantity_adjustment(
            day=day,
            actor=manager,
            reason_kind=SalesAdjustmentReasonKind.RETURNED_AFTER_FULFILLMENT,
            disposition=DirectStockDisposition.NO_RESTOCK,
            quantity=Decimal("1"),
        )

        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)

        assert posted.direct_stock_disposition == DirectStockDisposition.NO_RESTOCK
        assert not SalesAdjustmentStockPosting.objects.filter(adjustment=posted).exists()
        assert not SalesDirectStockReturnFulfillment.objects.filter(
            adjustment_line__adjustment=posted
        ).exists()
        assert not StockLedgerEntry.objects.filter(
            source_document_type=ADJUSTMENT_SOURCE_DOCUMENT_TYPE,
            source_document_id=str(posted.public_id),
        ).exists()

        journal = JournalEntry.objects.get(
            source_document_type=ADJUSTMENT_SOURCE_DOCUMENT_TYPE,
            source_document_id=str(posted.public_id),
            source_event=SourceEvent.POSTED,
        )
        lines = {row.account.code: row for row in journal.lines.select_related("account")}
        assert set(lines) == {"1-01-01-001", "4-03-01-001"}
        assert lines["4-03-01-001"].debit == Decimal("10000.000")
        assert lines["1-01-01-001"].credit == Decimal("10000.000")

        balance = StockBalance.objects.get(
            warehouse=source_warehouse,
            item=direct_item,
            lot=None,
        )
        assert balance.quantity == Decimal("8.000")
        assert balance.value == Decimal("16000.000")

    def test_cancellation_forces_restock_even_when_no_restock_is_requested(
        self,
        organization: Organization,
        branch: Branch,
        direct_stock_chart: dict[str, Account],
        consumption_mapping: Account,
        direct_item: InventoryItem,
        source_warehouse: Warehouse,
        direct_menu_item: MenuItem,
        hall_channel: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        day = _posted_direct_sale(
            organization=organization,
            branch=branch,
            chart=direct_stock_chart,
            item=direct_item,
            warehouse=source_warehouse,
            menu_item=direct_menu_item,
            channel=hall_channel,
            manager=manager,
            accounting_manager=accounting_manager,
        )
        adjustment = _quantity_adjustment(
            day=day,
            actor=manager,
            reason_kind=SalesAdjustmentReasonKind.CANCELLED_BEFORE_FULFILLMENT,
            disposition=DirectStockDisposition.NO_RESTOCK,
            quantity=Decimal("1"),
        )
        assert adjustment.direct_stock_disposition == DirectStockDisposition.RESTOCK

        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)

        evidence = SalesDirectStockReturnFulfillment.objects.get(adjustment_line=posted.lines.get())
        assert evidence.base_quantity == Decimal("1.000")
        assert evidence.cogs_value == Decimal("2000.000")
        balance = StockBalance.objects.get(
            warehouse=source_warehouse,
            item=direct_item,
            lot=None,
        )
        assert balance.quantity == Decimal("9.000")
        assert balance.value == Decimal("18000.000")

    def test_cumulative_returns_cannot_exceed_the_original_fulfillment(
        self,
        organization: Organization,
        branch: Branch,
        direct_stock_chart: dict[str, Account],
        consumption_mapping: Account,
        direct_item: InventoryItem,
        source_warehouse: Warehouse,
        direct_menu_item: MenuItem,
        hall_channel: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        day = _posted_direct_sale(
            organization=organization,
            branch=branch,
            chart=direct_stock_chart,
            item=direct_item,
            warehouse=source_warehouse,
            menu_item=direct_menu_item,
            channel=hall_channel,
            manager=manager,
            accounting_manager=accounting_manager,
        )
        for quantity in (Decimal("0.500"), Decimal("1.500")):
            adjustment = _quantity_adjustment(
                day=day,
                actor=manager,
                reason_kind=SalesAdjustmentReasonKind.RETURNED_AFTER_FULFILLMENT,
                disposition=DirectStockDisposition.RESTOCK,
                quantity=quantity,
            )
            post_sales_adjustment(adjustment=adjustment, actor=manager)

        returned = list(
            SalesDirectStockReturnFulfillment.objects.filter(
                source_fulfillment__sales_line=day.lines.get()
            ).order_by("adjustment_line_id")
        )
        assert [row.base_quantity for row in returned] == [
            Decimal("0.500"),
            Decimal("1.500"),
        ]
        assert sum((row.cogs_value for row in returned), Decimal("0")) == Decimal("4000.000")

        balance = StockBalance.objects.get(
            warehouse=source_warehouse,
            item=direct_item,
            lot=None,
        )
        assert balance.quantity == Decimal("10.000")
        assert balance.value == Decimal("20000.000")

        third = create_sales_adjustment(
            sales_day=day,
            reason_kind=SalesAdjustmentReasonKind.RETURNED_AFTER_FULFILLMENT,
            direct_stock_disposition=DirectStockDisposition.RESTOCK,
            business_date=BUSINESS_DATE + datetime.timedelta(days=1),
            reason="محاولة إرجاع زائد",
            evidence_reference="DIRECT-OVER-RETURN",
            actor=manager,
        )
        with pytest.raises(ValidationError) as caught:
            add_adjustment_line(
                adjustment=third,
                original_line=day.lines.get(),
                adjusted_quantity=Decimal("0.001"),
                actor=manager,
            )
        assert "quantity_over_adjusted" in _error_codes(caught.value)

    # Transactional for the reason given on the day-posting reversal test.
    @pytest.mark.django_db(transaction=True)
    def test_reversing_a_restock_adjustment_reverses_its_return_movement(
        self,
        organization: Organization,
        branch: Branch,
        direct_stock_chart: dict[str, Account],
        consumption_mapping: Account,
        direct_item: InventoryItem,
        source_warehouse: Warehouse,
        direct_menu_item: MenuItem,
        hall_channel: SalesChannel,
        manager: User,
        accounting_manager: User,
    ) -> None:
        day = _posted_direct_sale(
            organization=organization,
            branch=branch,
            chart=direct_stock_chart,
            item=direct_item,
            warehouse=source_warehouse,
            menu_item=direct_menu_item,
            channel=hall_channel,
            manager=manager,
            accounting_manager=accounting_manager,
        )
        adjustment = _quantity_adjustment(
            day=day,
            actor=manager,
            reason_kind=SalesAdjustmentReasonKind.RETURNED_AFTER_FULFILLMENT,
            disposition=DirectStockDisposition.RESTOCK,
            quantity=Decimal("1"),
        )
        posted = post_sales_adjustment(adjustment=adjustment, actor=manager)
        stock_return = SalesAdjustmentStockPosting.objects.get(adjustment=posted).stock_entry
        adjustment_journal = JournalEntry.objects.get(
            source_document_type=ADJUSTMENT_SOURCE_DOCUMENT_TYPE,
            source_document_id=str(posted.public_id),
            source_event=SourceEvent.POSTED,
        )

        reversed_adjustment = reverse_sales_adjustment(
            adjustment=posted,
            actor=accounting_manager,
            reason="إلغاء اختبار الإرجاع",
        )

        assert reversed_adjustment.status == SalesAdjustmentStatus.REVERSED
        stock_reversal = StockLedgerEntry.objects.get(
            reverses=stock_return,
            source_event=SourceEvent.REVERSED,
        )
        journal_reversal = JournalEntry.objects.get(
            reverses=adjustment_journal,
            source_event=SourceEvent.REVERSED,
        )
        assert stock_reversal.journal_entry == journal_reversal
        movement = stock_reversal.movements.get()
        assert movement.movement_type == MovementType.REVERSAL
        assert movement.base_quantity == Decimal("-1.000")
        assert movement.inventory_value == Decimal("-2000.000")

        balance = StockBalance.objects.get(
            warehouse=source_warehouse,
            item=direct_item,
            lot=None,
        )
        assert balance.quantity == Decimal("8.000")
        assert balance.value == Decimal("16000.000")
