"""Certified stock and COGS evidence for a direct-stock sales document."""

from __future__ import annotations

import datetime
import zoneinfo
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from apps.accounting.models import (
    INVENTORY_CONSUMPTION,
    Account,
    AccountRole,
    CostCenter,
    SourceEvent,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
    post_entry,
    reverse_entry,
)
from apps.accounting.validators import PostingLine
from apps.core.context import audit_context
from apps.inventory.ledger import MovementInput, post_stock_entry
from apps.inventory.models import (
    InventoryItem,
    MovementType,
    StockBalance,
    StockLedgerEntry,
    StockMovement,
    Warehouse,
)
from apps.inventory.sales import (
    DirectStockReturnLine,
    DirectStockSaleLine,
    SalesStockPlan,
    SalesStockPosting,
    link_sales_stock_journal,
    plan_sales_restock,
    plan_sales_stock,
    post_sales_restock,
    post_sales_stock,
    reverse_sales_stock,
)
from apps.organizations.models import Branch, Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
BUSINESS_DATE = datetime.date(2026, 3, 15)
EFFECTIVE_AT = datetime.datetime(2026, 3, 15, 20, 0, tzinfo=BAGHDAD)
SOURCE_TYPE = "SALES_DAY"
SOURCE_ID = "sales-day-public-id"
DIRECT_SOURCE_TYPE = "SALES.SALESDAY"
ADJUSTMENT_SOURCE_TYPE = "SALES.SALESADJUSTMENT"


def codes_of(error: ValidationError) -> set[str]:
    if hasattr(error, "error_dict"):
        return {
            item.code
            for errors in error.error_dict.values()
            for item in errors
            if item.code is not None
        }
    return {item.code for item in error.error_list if item.code is not None}


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=BUSINESS_DATE.year)
    call_command("seed_chart_of_accounts", organization=organization.code, verbosity=0)


@pytest.fixture
def control_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="1-03-01-001")


@pytest.fixture
def consumption_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="5-01-02-001")


@pytest.fixture
def consumption_mapping(organization: Organization, consumption_account: Account) -> None:
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=INVENTORY_CONSUMPTION),
        account=consumption_account,
        effective_from=datetime.date(BUSINESS_DATE.year, 1, 1),
    )


@pytest.fixture
def hall(organization: Organization, accounting: None) -> CostCenter:
    return CostCenter.objects.get(organization=organization, code="HALL")


@pytest.fixture
def delivery(organization: Organization, accounting: None) -> CostCenter:
    return CostCenter.objects.get(organization=organization, code="DELIVERY")


@pytest.fixture
def stocked(
    organization: Organization,
    main_store: Warehouse,
    rice: InventoryItem,
    control_account: Account,
    manager: User,
) -> StockLedgerEntry:
    """Ten base units at 2,000 IQD each, posted through the real kernel."""
    with audit_context(actor=manager):
        return post_stock_entry(
            organization=organization,
            effects=[
                MovementInput(
                    warehouse=main_store,
                    item=rice,
                    movement_type=MovementType.RECEIPT,
                    quantity=Decimal("10"),
                    effect_key="sales-adapter-opening",
                    unit_cost=Decimal("2000"),
                    control_account=control_account,
                )
            ],
            idempotency_key="sales-adapter-opening",
            effective_at=EFFECTIVE_AT,
            business_date=BUSINESS_DATE,
            source_document_type="TEST_OPENING",
            source_document_id="sales-adapter",
            source_event=SourceEvent.POSTED,
        )


def sale_line(
    *,
    key: str,
    warehouse: Warehouse,
    item: InventoryItem,
    quantity: str,
    cost_center: CostCenter,
) -> DirectStockSaleLine:
    return DirectStockSaleLine(
        line_key=key,
        warehouse=warehouse,
        item=item,
        quantity=Decimal(quantity),
        cost_center=cost_center,
    )


def plan_for(
    *,
    organization: Organization,
    branch: Branch,
    lines: list[DirectStockSaleLine],
) -> SalesStockPlan:
    return plan_sales_stock(
        organization=organization,
        branch=branch,
        business_date=BUSINESS_DATE,
        lines=lines,
    )


def direct_return_line(
    *,
    key: str,
    source: StockMovement,
    fulfilled_quantity: str,
    fulfilled_value: str,
    quantity: str,
    control_account: Account | None,
    consumption_account: Account,
    cost_center: CostCenter,
) -> DirectStockReturnLine:
    return DirectStockReturnLine(
        line_key=key,
        source_movement=source,
        fulfilled_quantity=Decimal(fulfilled_quantity),
        fulfilled_cogs_value=Decimal(fulfilled_value),
        quantity=Decimal(quantity),
        control_account=control_account,
        consumption_account=consumption_account,
        cost_center=cost_center,
    )


def linked_direct_sale(
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    item: InventoryItem,
    quantity: str,
    opening_value: str,
    source_id: str,
    control_account: Account,
    consumption_account: Account,
    cost_center: CostCenter,
    manager: User,
) -> SalesStockPosting:
    base_quantity = Decimal(quantity)
    value = Decimal(opening_value)
    unit_cost = Decimal("0") if value == 0 else value / base_quantity
    with audit_context(actor=manager):
        post_stock_entry(
            organization=organization,
            effects=[
                MovementInput(
                    warehouse=warehouse,
                    item=item,
                    movement_type=MovementType.RECEIPT,
                    quantity=base_quantity,
                    effect_key=f"return-opening:{source_id}",
                    unit_cost=unit_cost,
                    inbound_value=value,
                    control_account=control_account,
                )
            ],
            idempotency_key=f"return-opening:{source_id}",
            effective_at=EFFECTIVE_AT,
            business_date=BUSINESS_DATE,
            source_document_type="TEST_OPENING",
            source_document_id=source_id,
            source_event=SourceEvent.POSTED,
        )
        plan = plan_for(
            organization=organization,
            branch=branch,
            lines=[
                sale_line(
                    key=f"source-line:{source_id}",
                    warehouse=warehouse,
                    item=item,
                    quantity=quantity,
                    cost_center=cost_center,
                )
            ],
        )
        posted = post_sales_stock(
            plan=plan,
            effective_at=EFFECTIVE_AT,
            idempotency_key=f"direct-source:{source_id}",
            source_document_type=DIRECT_SOURCE_TYPE,
            source_document_id=source_id,
        )
        journal_lines = posted.posting_lines or (
            PostingLine(account=control_account, branch=branch, debit=Decimal("1")),
            PostingLine(
                account=consumption_account,
                branch=branch,
                credit=Decimal("1"),
                cost_center=cost_center,
            ),
        )
        journal = post_entry(
            organization=organization,
            accounting_date=BUSINESS_DATE,
            document_date=BUSINESS_DATE,
            lines=journal_lines,
            idempotency_key=f"direct-source-journal:{source_id}",
            source_document_type=DIRECT_SOURCE_TYPE,
            source_document_id=source_id,
            source_event=SourceEvent.POSTED,
            posting_rule_version="sales-direct-stock-return-test",
        )
        link_sales_stock_journal(entry=posted.entry, journal=journal)
    return posted


class TestPlan:
    def test_an_incomplete_nullable_sales_snapshot_is_a_named_refusal(
        self,
        organization: Organization,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            plan_for(
                organization=organization,
                branch=branch,
                lines=[
                    DirectStockSaleLine(
                        line_key="incomplete",
                        warehouse=None,
                        item=None,
                        quantity=None,
                        cost_center=hall,
                    )
                ],
            )

        assert "sales_stock_line_incomplete" in codes_of(caught.value)

    def test_account_resolution_happens_before_stock_mutation(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        hall: CostCenter,
        stocked: StockLedgerEntry,
        consumption_mapping: None,
        consumption_account: Account,
    ) -> None:
        before = StockBalance.objects.get(warehouse=main_store, item=rice, lot=None)

        plan = plan_for(
            organization=organization,
            branch=branch,
            lines=[
                sale_line(
                    key="line-1",
                    warehouse=main_store,
                    item=rice,
                    quantity="2",
                    cost_center=hall,
                )
            ],
        )

        after = StockBalance.objects.get(pk=before.pk)
        assert plan.lines[0].consumption.account == consumption_account
        assert plan.lines[0].quantity == Decimal("2.000")
        assert after.quantity == before.quantity
        assert after.value == before.value

    def test_an_unmapped_consumption_account_refuses_before_any_issue(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        hall: CostCenter,
        stocked: StockLedgerEntry,
    ) -> None:
        before = StockBalance.objects.get(warehouse=main_store, item=rice, lot=None)

        with pytest.raises(ValidationError) as caught:
            plan_for(
                organization=organization,
                branch=branch,
                lines=[
                    sale_line(
                        key="line-unmapped",
                        warehouse=main_store,
                        item=rice,
                        quantity="2",
                        cost_center=hall,
                    )
                ],
            )

        after = StockBalance.objects.get(pk=before.pk)
        assert "account_role_unmapped" in codes_of(caught.value)
        assert after.quantity == before.quantity
        assert after.value == before.value
        assert not StockLedgerEntry.objects.filter(
            source_document_type=SOURCE_TYPE, source_document_id=SOURCE_ID
        ).exists()


class TestPosting:
    def test_zero_book_cost_is_evidence_without_zero_journal_lines(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        hall: CostCenter,
        control_account: Account,
        consumption_mapping: None,
        manager: User,
    ) -> None:
        with audit_context(actor=manager):
            post_stock_entry(
                organization=organization,
                effects=[
                    MovementInput(
                        warehouse=main_store,
                        item=rice,
                        movement_type=MovementType.RECEIPT,
                        quantity=Decimal("2"),
                        effect_key="free-stock-opening",
                        unit_cost=Decimal("0"),
                        control_account=control_account,
                    )
                ],
                idempotency_key="free-stock-opening",
                effective_at=EFFECTIVE_AT,
                business_date=BUSINESS_DATE,
                source_document_type="TEST_OPENING",
                source_document_id="free-sales-adapter",
                source_event=SourceEvent.POSTED,
            )
            plan = plan_for(
                organization=organization,
                branch=branch,
                lines=[
                    sale_line(
                        key="free-line",
                        warehouse=main_store,
                        item=rice,
                        quantity="1",
                        cost_center=hall,
                    )
                ],
            )
            posted = post_sales_stock(
                plan=plan,
                effective_at=EFFECTIVE_AT,
                idempotency_key="free-stock-sale",
                source_document_type=SOURCE_TYPE,
                source_document_id="free-sales-day",
            )

        assert posted.evidence[0].cogs_value == Decimal("0.000")
        assert posted.total_cost == Decimal("0.000")
        assert posted.posting_lines == ()
        assert posted.movements["free-line"].base_quantity == Decimal("-1.000")

    def test_one_issue_per_sales_line_and_exact_cogs_evidence(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        hall: CostCenter,
        delivery: CostCenter,
        stocked: StockLedgerEntry,
        consumption_mapping: None,
        consumption_account: Account,
        control_account: Account,
        manager: User,
    ) -> None:
        plan = plan_for(
            organization=organization,
            branch=branch,
            lines=[
                sale_line(
                    key="hall-line",
                    warehouse=main_store,
                    item=rice,
                    quantity="2",
                    cost_center=hall,
                ),
                sale_line(
                    key="delivery-line",
                    warehouse=main_store,
                    item=rice,
                    quantity="3",
                    cost_center=delivery,
                ),
            ],
        )

        with audit_context(actor=manager):
            posted = post_sales_stock(
                plan=plan,
                effective_at=EFFECTIVE_AT,
                idempotency_key="sales-stock-post-1",
                source_document_type=SOURCE_TYPE,
                source_document_id=SOURCE_ID,
                reference="Z report",
                reason="Direct-stock sales",
            )

        assert posted.entry.source_document_type == SOURCE_TYPE
        assert posted.entry.source_document_id == SOURCE_ID
        assert posted.entry.source_event == SourceEvent.POSTED
        assert posted.entry.journal_entry_id is None
        assert posted.total_cost == Decimal("10000.000")

        assert set(posted.movements) == {"hall-line", "delivery-line"}
        assert {row.movement.movement_type for row in posted.evidence} == {MovementType.ISSUE}
        assert posted.movements["hall-line"].base_quantity == Decimal("-2.000")
        assert posted.movements["hall-line"].inventory_value == Decimal("-4000.000")
        assert posted.movements["delivery-line"].base_quantity == Decimal("-3.000")
        assert posted.movements["delivery-line"].inventory_value == Decimal("-6000.000")

        assert len(posted.posting_lines) == 3
        inventory = next(row for row in posted.posting_lines if row.credit > Decimal("0"))
        assert inventory.account == control_account
        assert inventory.credit == Decimal("10000.000")
        assert inventory.cost_center is None
        cogs = {
            row.cost_center.code: row
            for row in posted.posting_lines
            if row.debit > Decimal("0") and row.cost_center is not None
        }
        assert cogs["HALL"].account == consumption_account
        assert cogs["HALL"].debit == Decimal("4000.000")
        assert cogs["DELIVERY"].debit == Decimal("6000.000")

        balance = StockBalance.objects.get(warehouse=main_store, item=rice, lot=None)
        assert balance.quantity == Decimal("5.000")
        assert balance.value == Decimal("10000.000")

    def test_insufficient_stock_rolls_the_whole_stock_event_back(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        hall: CostCenter,
        stocked: StockLedgerEntry,
        consumption_mapping: None,
        manager: User,
    ) -> None:
        plan = plan_for(
            organization=organization,
            branch=branch,
            lines=[
                sale_line(
                    key="too-much-1",
                    warehouse=main_store,
                    item=rice,
                    quantity="7",
                    cost_center=hall,
                ),
                sale_line(
                    key="too-much-2",
                    warehouse=main_store,
                    item=rice,
                    quantity="7",
                    cost_center=hall,
                ),
            ],
        )

        with audit_context(actor=manager), pytest.raises(ValidationError) as caught:
            post_sales_stock(
                plan=plan,
                effective_at=EFFECTIVE_AT,
                idempotency_key="sales-stock-too-much",
                source_document_type=SOURCE_TYPE,
                source_document_id=SOURCE_ID,
            )

        assert "insufficient_stock" in codes_of(caught.value)
        balance = StockBalance.objects.get(warehouse=main_store, item=rice, lot=None)
        assert balance.quantity == Decimal("10.000")
        assert balance.value == Decimal("20000.000")
        assert not StockLedgerEntry.objects.filter(
            source_document_type=SOURCE_TYPE, source_document_id=SOURCE_ID
        ).exists()

    def test_the_adapter_fingerprint_includes_the_cost_center(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        hall: CostCenter,
        delivery: CostCenter,
        stocked: StockLedgerEntry,
        consumption_mapping: None,
        manager: User,
    ) -> None:
        first = plan_for(
            organization=organization,
            branch=branch,
            lines=[
                sale_line(
                    key="same-line",
                    warehouse=main_store,
                    item=rice,
                    quantity="1",
                    cost_center=hall,
                )
            ],
        )
        changed = plan_for(
            organization=organization,
            branch=branch,
            lines=[
                sale_line(
                    key="same-line",
                    warehouse=main_store,
                    item=rice,
                    quantity="1",
                    cost_center=delivery,
                )
            ],
        )
        assert first.fingerprint != changed.fingerprint

        with audit_context(actor=manager):
            post_sales_stock(
                plan=first,
                effective_at=EFFECTIVE_AT,
                idempotency_key="same-sales-stock-key",
                source_document_type=SOURCE_TYPE,
                source_document_id=SOURCE_ID,
            )
            with pytest.raises(ValidationError) as caught:
                post_sales_stock(
                    plan=changed,
                    effective_at=EFFECTIVE_AT,
                    idempotency_key="same-sales-stock-key",
                    source_document_type=SOURCE_TYPE,
                    source_document_id=SOURCE_ID,
                )

        assert "idempotency_key_conflict" in codes_of(caught.value)


class TestDirectSalesRestock:
    def test_partial_then_final_return_uses_the_exact_value_remainder(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        hall: CostCenter,
        delivery: CostCenter,
        control_account: Account,
        consumption_mapping: None,
        consumption_account: Account,
        manager: User,
    ) -> None:
        sold = linked_direct_sale(
            organization=organization,
            branch=branch,
            warehouse=main_store,
            item=rice,
            quantity="3",
            opening_value="1",
            source_id="rounding-source",
            control_account=control_account,
            consumption_account=consumption_account,
            cost_center=hall,
            manager=manager,
        )
        source = sold.evidence[0].movement

        with pytest.raises(ValidationError) as caught:
            plan_sales_restock(
                organization=organization,
                branch=branch,
                business_date=BUSINESS_DATE,
                source_document_type=ADJUSTMENT_SOURCE_TYPE,
                source_document_id="forged-adjustment",
                lines=[
                    direct_return_line(
                        key="forged-return",
                        source=source,
                        fulfilled_quantity="3",
                        fulfilled_value="1",
                        quantity="1",
                        control_account=control_account,
                        consumption_account=consumption_account,
                        cost_center=delivery,
                    )
                ],
            )
        assert "sales_restock_cogs_evidence_mismatch" in codes_of(caught.value)

        first = plan_sales_restock(
            organization=organization,
            branch=branch,
            business_date=BUSINESS_DATE,
            source_document_type=ADJUSTMENT_SOURCE_TYPE,
            source_document_id="adjustment-one",
            lines=[
                direct_return_line(
                    key="return-one",
                    source=source,
                    fulfilled_quantity="3",
                    fulfilled_value="1",
                    quantity="1",
                    control_account=control_account,
                    consumption_account=consumption_account,
                    cost_center=hall,
                )
            ],
        )
        assert first.lines[0].value == Decimal("0.333")
        with audit_context(actor=manager):
            first_posted = post_sales_restock(
                plan=first,
                effective_at=EFFECTIVE_AT,
                idempotency_key="restock-one",
            )
            replayed = post_sales_restock(
                plan=first,
                effective_at=EFFECTIVE_AT,
                idempotency_key="restock-one",
            )

        assert first_posted.evidence[0].cogs_value == Decimal("0.333")
        assert replayed.entry.pk == first_posted.entry.pk
        assert first_posted.posting_lines[0].account == control_account
        assert first_posted.posting_lines[0].debit == Decimal("0.333")
        assert first_posted.posting_lines[1].account == consumption_account
        assert first_posted.posting_lines[1].credit == Decimal("0.333")

        final = plan_sales_restock(
            organization=organization,
            branch=branch,
            business_date=BUSINESS_DATE,
            source_document_type=ADJUSTMENT_SOURCE_TYPE,
            source_document_id="adjustment-two",
            lines=[
                direct_return_line(
                    key="return-two",
                    source=source,
                    fulfilled_quantity="3",
                    fulfilled_value="1",
                    quantity="2",
                    control_account=control_account,
                    consumption_account=consumption_account,
                    cost_center=hall,
                )
            ],
        )
        assert final.lines[0].returned_before_value == Decimal("0.333")
        assert final.lines[0].value == Decimal("0.667")
        with audit_context(actor=manager):
            final_posted = post_sales_restock(
                plan=final,
                effective_at=EFFECTIVE_AT,
                idempotency_key="restock-two",
            )

        assert final_posted.total_cost == Decimal("0.667")
        balance = StockBalance.objects.get(warehouse=main_store, item=rice, lot=None)
        assert balance.quantity == Decimal("3.000")
        assert balance.value == Decimal("1.000")

        with pytest.raises(ValidationError) as caught:
            plan_sales_restock(
                organization=organization,
                branch=branch,
                business_date=BUSINESS_DATE,
                source_document_type=ADJUSTMENT_SOURCE_TYPE,
                source_document_id="adjustment-over",
                lines=[
                    direct_return_line(
                        key="return-over",
                        source=source,
                        fulfilled_quantity="3",
                        fulfilled_value="1",
                        quantity="0.001",
                        control_account=control_account,
                        consumption_account=consumption_account,
                        cost_center=hall,
                    )
                ],
            )
        assert "sales_restock_over_return" in codes_of(caught.value)

        with audit_context(actor=manager):
            final_journal = post_entry(
                organization=organization,
                accounting_date=BUSINESS_DATE,
                document_date=BUSINESS_DATE,
                lines=final_posted.posting_lines,
                idempotency_key="restock-two-journal",
                source_document_type=ADJUSTMENT_SOURCE_TYPE,
                source_document_id="adjustment-two",
                source_event=SourceEvent.POSTED,
                posting_rule_version="sales-direct-stock-return-test",
            )
            linked = link_sales_stock_journal(entry=final_posted.entry, journal=final_journal)
            reversed_restock = reverse_sales_stock(
                entry=linked,
                idempotency_key="restock-two-reversal",
                reason="Reverse the physical return",
                effective_at=EFFECTIVE_AT + datetime.timedelta(days=1),
                business_date=BUSINESS_DATE + datetime.timedelta(days=1),
            )

        assert set(reversed_restock.movements.values_list("movement_type", flat=True)) == {
            MovementType.REVERSAL
        }
        balance.refresh_from_db()
        assert balance.quantity == Decimal("1.000")
        assert balance.value == Decimal("0.333")

    def test_zero_cost_return_has_evidence_and_no_journal_lines(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        hall: CostCenter,
        control_account: Account,
        consumption_mapping: None,
        consumption_account: Account,
        manager: User,
    ) -> None:
        sold = linked_direct_sale(
            organization=organization,
            branch=branch,
            warehouse=main_store,
            item=rice,
            quantity="1",
            opening_value="0",
            source_id="zero-return-source",
            control_account=control_account,
            consumption_account=consumption_account,
            cost_center=hall,
            manager=manager,
        )
        source = sold.evidence[0].movement
        plan = plan_sales_restock(
            organization=organization,
            branch=branch,
            business_date=BUSINESS_DATE,
            source_document_type=ADJUSTMENT_SOURCE_TYPE,
            source_document_id="zero-return-adjustment",
            lines=[
                direct_return_line(
                    key="zero-return",
                    source=source,
                    fulfilled_quantity="1",
                    fulfilled_value="0",
                    quantity="1",
                    control_account=control_account,
                    consumption_account=consumption_account,
                    cost_center=hall,
                )
            ],
        )
        with audit_context(actor=manager):
            posted = post_sales_restock(
                plan=plan,
                effective_at=EFFECTIVE_AT,
                idempotency_key="zero-restock",
            )

        assert posted.total_cost == Decimal("0.000")
        assert posted.evidence[0].cogs_value == Decimal("0.000")
        assert posted.posting_lines == ()
        assert posted.evidence[0].movement.inventory_value == Decimal("0.000")


class TestJournalLinkAndReversal:
    def test_the_sales_journal_links_and_the_reversal_mirrors_both_ledgers(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        hall: CostCenter,
        stocked: StockLedgerEntry,
        consumption_mapping: None,
        manager: User,
    ) -> None:
        plan = plan_for(
            organization=organization,
            branch=branch,
            lines=[
                sale_line(
                    key="reversible-line",
                    warehouse=main_store,
                    item=rice,
                    quantity="4",
                    cost_center=hall,
                )
            ],
        )

        with audit_context(actor=manager):
            posted = post_sales_stock(
                plan=plan,
                effective_at=EFFECTIVE_AT,
                idempotency_key="sales-stock-reversible",
                source_document_type=SOURCE_TYPE,
                source_document_id=SOURCE_ID,
            )
            journal = post_entry(
                organization=organization,
                accounting_date=BUSINESS_DATE,
                document_date=BUSINESS_DATE,
                lines=posted.posting_lines,
                idempotency_key="sales-day-journal",
                source_document_type=SOURCE_TYPE,
                source_document_id=SOURCE_ID,
                source_event=SourceEvent.POSTED,
                posting_rule_version="sales-direct-stock-test",
            )
            linked = link_sales_stock_journal(entry=posted.entry, journal=journal)

        assert linked.journal_entry_id == journal.pk

        reversal_at = EFFECTIVE_AT + datetime.timedelta(days=1)
        reversal_date = BUSINESS_DATE + datetime.timedelta(days=1)
        with audit_context(actor=manager):
            reversed_stock = reverse_sales_stock(
                entry=linked,
                idempotency_key="sales-stock-reversal",
                reason="Reverse the sales day",
                effective_at=reversal_at,
                business_date=reversal_date,
            )
            reversed_journal = reverse_entry(
                entry=journal,
                idempotency_key="sales-day-journal-reversal",
                reason="Reverse the sales day",
                accounting_date=reversal_date,
            )
            linked_reversal = link_sales_stock_journal(
                entry=reversed_stock, journal=reversed_journal
            )

        assert linked_reversal.source_document_type == SOURCE_TYPE
        assert linked_reversal.source_document_id == SOURCE_ID
        assert linked_reversal.source_event == SourceEvent.REVERSED
        assert linked_reversal.journal_entry_id == reversed_journal.pk
        balance = StockBalance.objects.get(warehouse=main_store, item=rice, lot=None)
        assert balance.quantity == Decimal("10.000")
        assert balance.value == Decimal("20000.000")

    def test_a_journal_for_another_source_cannot_be_linked(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        hall: CostCenter,
        stocked: StockLedgerEntry,
        consumption_mapping: None,
        manager: User,
    ) -> None:
        plan = plan_for(
            organization=organization,
            branch=branch,
            lines=[
                sale_line(
                    key="wrong-journal-line",
                    warehouse=main_store,
                    item=rice,
                    quantity="1",
                    cost_center=hall,
                )
            ],
        )
        with audit_context(actor=manager):
            posted = post_sales_stock(
                plan=plan,
                effective_at=EFFECTIVE_AT,
                idempotency_key="sales-stock-wrong-journal",
                source_document_type=SOURCE_TYPE,
                source_document_id=SOURCE_ID,
            )
            other = post_entry(
                organization=organization,
                accounting_date=BUSINESS_DATE,
                document_date=BUSINESS_DATE,
                lines=posted.posting_lines,
                idempotency_key="other-sales-journal",
                source_document_type=SOURCE_TYPE,
                source_document_id="another-sales-day",
                source_event=SourceEvent.POSTED,
                posting_rule_version="sales-direct-stock-test",
            )
            with pytest.raises(ValidationError) as caught:
                link_sales_stock_journal(entry=posted.entry, journal=other)

        assert "sales_stock_journal_source_mismatch" in codes_of(caught.value)
        posted.entry.refresh_from_db()
        assert posted.entry.journal_entry_id is None
