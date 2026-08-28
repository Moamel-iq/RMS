"""
Transfers, in-transit custody, partial receipts and shortages (Task 1.5 §X).

Every event is created and posted through the command layer with a real actor,
so each test also exercises the authorization protecting the act it tests.
"""

from __future__ import annotations

import datetime
import zoneinfo
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction

from apps.accounting.models import (
    INTER_BRANCH_CLEARING,
    INVENTORY_CONTROL,
    INVENTORY_IN_TRANSIT,
    INVENTORY_SHORTAGE_LOSS,
    Account,
    AccountingPeriod,
    AccountRole,
    CostCenter,
    PeriodState,
)
from apps.accounting.services import (
    archive_account_mapping,
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.inventory.commands import (
    add_document_line,
    add_transfer_line,
    create_document,
    create_transfer,
    create_transfer_receipt,
    create_transfer_shortage,
    dispatch_transfer,
    post_document,
    post_transfer_receipt,
    post_transfer_shortage,
    replace_transfer_receipt_lines,
    resolve_receipt,
    resolve_transfer,
    reverse_dispatch,
    reverse_transfer_receipt,
    reverse_transfer_shortage,
    visible_transfers,
)
from apps.inventory.models import (
    ConversionType,
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryItem,
    InventoryLot,
    ItemPackageConversion,
    ItemType,
    MovementType,
    PackageUnit,
    StockBalance,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    StockTransferReceipt,
    StockTransferShortage,
    StockTransferStatus,
    Warehouse,
    WarehouseType,
)
from apps.inventory.operations import DocumentLineInput
from apps.inventory.services import create_item, create_item_conversion, create_warehouse
from apps.inventory.tests.stock_seed import seed_stock
from apps.inventory.transfers import ReceiptLineInput, TransferLineInput, allocate
from apps.organizations.authorization import OutOfScope
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access, grant_organization_access
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
TEST_YEAR = 2026
WHEN = datetime.datetime(TEST_YEAR, 3, 15, 10, 0, tzinfo=BAGHDAD)
JAN_1 = datetime.date(TEST_YEAR, 1, 1)
ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)


@pytest.fixture
def control_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="1-03-01-001")


@pytest.fixture
def transit_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="1-03-02-001")


@pytest.fixture
def clearing_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="8-01-01-001")


@pytest.fixture
def shortage_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="6-02-01-001")


@pytest.fixture
def grni_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="2-01-02-001")


@pytest.fixture
def warehouse_center(organization: Organization, accounting: None) -> CostCenter:
    return CostCenter.objects.get(organization=organization, code="WAREHOUSE")


@pytest.fixture
def mapped(
    organization: Organization,
    control_account: Account,
    transit_account: Account,
    clearing_account: Account,
    shortage_account: Account,
    grni_account: Account,
) -> None:
    """Every role a transfer touches, plus the receipt role that seeds stock."""
    from apps.accounting.models import GOODS_RECEIVED_NOT_INVOICED

    for code, account in (
        (INVENTORY_CONTROL, control_account),
        (INVENTORY_IN_TRANSIT, transit_account),
        (INTER_BRANCH_CLEARING, clearing_account),
        (INVENTORY_SHORTAGE_LOSS, shortage_account),
        (GOODS_RECEIVED_NOT_INVOICED, grni_account),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=account,
            effective_from=JAN_1,
        )


@pytest.fixture
def far_store(second_branch: Branch) -> Warehouse:
    """A warehouse in the organization's *other* branch."""
    return create_warehouse(branch=second_branch, code="MAIN", name="مخزن الكرادة")


@pytest.fixture
def group_manager(organization: Organization, branch: Branch, second_branch: Branch) -> User:
    """Organization authority, so both branches are in reach."""
    user = User.objects.create_user(username="group-manager", password="pw-not-real-1234")
    grant_organization_access(user=user, organization=organization, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def keeper(branch: Branch) -> User:
    """A storekeeper: moves goods, never sees cost, never closes a shortage."""
    user = User.objects.create_user(username="keeper", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


def _seed_stock(
    actor: User,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    item: InventoryItem,
    quantity: str,
    cost: str,
    *,
    lot: InventoryLot | None = None,
    reference: str = "DN-SEED",
) -> None:
    """Put stock on a shelf, through the kernel."""
    seed_stock(
        actor=actor,
        organization=organization,
        warehouse=warehouse,
        item=item,
        quantity=quantity,
        unit_cost=cost,
        control_account=Account.objects.get(organization=organization, code="1-03-01-001"),
        lot=lot,
        effective_at=WHEN,
    )


@pytest.fixture
def stocked(
    group_manager: User,
    organization: Organization,
    branch: Branch,
    main_store: Warehouse,
    rice: InventoryItem,
    mapped: None,
) -> None:
    """100 kg of rice at 1500 in the main store — 150,000 on the shelf."""
    _seed_stock(group_manager, organization, branch, main_store, rice, "100", "1500")


def _transfer(
    actor: User,
    organization: Organization,
    source: Warehouse,
    destination: Warehouse,
    item: InventoryItem,
    quantity: str,
    *,
    lot: InventoryLot | None = None,
    conversion: ItemPackageConversion | None = None,
    packages: str | None = None,
    measured: str | None = None,
) -> StockTransfer:
    transfer = create_transfer(
        actor=actor,
        organization=organization,
        source_warehouse=source,
        destination_warehouse=destination,
        effective_at=WHEN,
        evidence_reference="TN-001",
    )
    add_transfer_line(
        actor=actor,
        transfer=transfer,
        line=TransferLineInput(
            item=item,
            lot=lot,
            package_conversion=conversion,
            entered_package_quantity=Decimal(packages) if packages else None,
            measured_base_quantity=Decimal(measured) if measured else None,
            base_quantity=Decimal(quantity) if quantity else None,
        ),
    )
    return transfer


def _receive(
    actor: User,
    transfer: StockTransfer,
    quantity: str,
    *,
    line: StockTransferLine | None = None,
    reference: str = "GRN-001",
) -> StockTransferReceipt:
    target = line or transfer.lines.order_by("sequence").first()
    assert target is not None
    receipt = create_transfer_receipt(
        actor=actor, transfer=transfer, effective_at=WHEN, evidence_reference=reference
    )
    replace_transfer_receipt_lines(
        actor=actor,
        receipt=receipt,
        lines=[ReceiptLineInput(transfer_line=target, base_quantity=Decimal(quantity))],
    )
    return post_transfer_receipt(actor=actor, receipt=receipt)


def _close_short(
    actor: User, transfer: StockTransfer, center: CostCenter, *, reason: str = "لم تصل الشحنة"
) -> StockTransferShortage:
    shortage = create_transfer_shortage(
        actor=actor,
        transfer=transfer,
        effective_at=WHEN,
        reason=reason,
        evidence_reference="CLAIM-1",
        cost_center=center,
    )
    return post_transfer_shortage(actor=actor, shortage=shortage)


def _maybe_balance(warehouse: Warehouse, item: InventoryItem) -> StockBalance | None:
    return StockBalance.objects.filter(warehouse=warehouse, item=item, lot=None).first()


def _balance(warehouse: Warehouse, item: InventoryItem) -> StockBalance:
    """The position, insisting it exists — most assertions are about figures."""
    balance = _maybe_balance(warehouse, item)
    assert balance is not None, f"{item.code} has no position in {warehouse.code}"
    return balance


def _value(amount: Decimal | None) -> Decimal:
    """A stored money column the posting always fills, narrowed for arithmetic."""
    assert amount is not None
    return amount


def _transit_of(branch: Branch) -> Warehouse:
    return Warehouse.objects.get(branch=branch, warehouse_type=WarehouseType.IN_TRANSIT)


def _transit_balance(branch: Branch, item: InventoryItem) -> StockBalance | None:
    """
    What a branch holds in transit — `None` when it holds nothing at all.

    A branch's in-transit warehouse is created the first time it dispatches,
    so a destination branch that has only ever received legitimately has no
    such warehouse. "No warehouse" and "an empty one" are the same fact here.
    """
    warehouse = Warehouse.objects.filter(
        branch=branch, warehouse_type=WarehouseType.IN_TRANSIT
    ).first()
    if warehouse is None:
        return None
    return _maybe_balance(warehouse, item)


# ---------------------------------------------------------------------------
# Endpoints and scope
# ---------------------------------------------------------------------------


class TestEndpoints:
    def test_source_and_destination_cannot_match(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_transfer(
                actor=group_manager,
                organization=organization,
                source_warehouse=main_store,
                destination_warehouse=main_store,
                effective_at=WHEN,
                evidence_reference="TN-1",
            )
        assert caught.value.code == "transfer_endpoints_identical"

    def test_a_cross_organization_transfer_is_refused(
        self,
        superuser: User,
        organization: Organization,
        main_store: Warehouse,
        other_warehouse: Warehouse,
        stocked: None,
    ) -> None:
        """
        Not an internal transfer at all: two organizations are two sets of
        books, and goods crossing between them is a sale and a purchase.
        """
        with pytest.raises(ValidationError) as caught:
            create_transfer(
                actor=superuser,
                organization=organization,
                source_warehouse=main_store,
                destination_warehouse=other_warehouse,
                effective_at=WHEN,
                evidence_reference="TN-1",
            )
        assert caught.value.code == "warehouse_organization_mismatch"

    def test_the_in_transit_warehouse_cannot_be_chosen(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        from apps.inventory.services import ensure_in_transit_warehouse

        transit = ensure_in_transit_warehouse(branch=branch)
        with pytest.raises(ValidationError) as caught:
            create_transfer(
                actor=group_manager,
                organization=organization,
                source_warehouse=transit,
                destination_warehouse=kitchen_store,
                effective_at=WHEN,
                evidence_reference="TN-1",
            )
        assert caught.value.code == "system_warehouse_not_selectable"

    def test_the_database_refuses_a_raw_in_transit_endpoint(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        stocked: None,
    ) -> None:
        """The trigger holds when the service is bypassed."""
        from apps.inventory.services import ensure_in_transit_warehouse

        transit = ensure_in_transit_warehouse(branch=branch)
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO inventory_stocktransfer (
                        organization_id, source_warehouse_id, destination_warehouse_id,
                        public_id, transfer_number, status, evidence_reference,
                        narration, effective_at, business_date,
                        business_date_timezone, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, gen_random_uuid(), '', 'DRAFT', 'X', '',
                        now(), current_date, '', now(), now()
                    )
                    """,
                    [organization.pk, main_store.pk, transit.pk],
                )

    def test_a_foreign_transfer_is_a_404(
        self,
        keeper: User,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        """A branch storekeeper cannot reach a transfer between two other branches."""
        second = create_warehouse(branch=far_store.branch, code="COLD", name="مخزن بارد")
        transfer = create_transfer(
            actor=group_manager,
            organization=organization,
            source_warehouse=far_store,
            destination_warehouse=second,
            effective_at=WHEN,
            evidence_reference="TN-9",
        )
        with pytest.raises(OutOfScope):
            resolve_transfer(keeper, transfer.pk)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_a_same_branch_dispatch_moves_value_into_transit(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        control_account: Account,
        transit_account: Account,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatched = dispatch_transfer(actor=group_manager, transfer=transfer)

        assert dispatched.status == StockTransferStatus.DISPATCHED
        assert dispatched.transfer_number.startswith("TRF-2026-")

        source = _balance(main_store, rice)
        assert source.quantity == Decimal("60.000")
        assert source.value == Decimal("90000.000")

        transit = _balance(_transit_of(branch), rice)
        assert transit.quantity == Decimal("40.000")
        # Exactly what left, to the dinar. No gain, no loss.
        assert transit.value == Decimal("60000.000")
        assert transit.control_account_id == transit_account.pk

        journal = dispatched.journal_entry
        assert journal is not None
        debits = {line.account.code: line.debit for line in journal.lines.all() if line.debit}
        credits = {line.account.code: line.credit for line in journal.lines.all() if line.credit}
        assert debits == {transit_account.code: Decimal("60000.000")}
        assert credits == {control_account.code: Decimal("60000.000")}

    def test_a_cross_branch_dispatch_stays_on_the_source_branch_books(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        second_branch: Branch,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        """Ownership does not move at dispatch — that is the whole point."""
        transfer = _transfer(group_manager, organization, main_store, far_store, rice, "40")
        dispatched = dispatch_transfer(actor=group_manager, transfer=transfer)

        assert _balance(_transit_of(branch), rice).quantity == Decimal("40.000")
        assert _transit_balance(second_branch, rice) is None
        assert _maybe_balance(far_store, rice) is None

        journal = dispatched.journal_entry
        assert journal is not None
        assert {line.branch_id for line in journal.lines.all()} == {branch.pk}

    def test_a_full_depletion_carries_its_entire_remaining_value(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """
        Three receipts at awkward costs leave an average that does not divide
        evenly. Transferring the lot must carry the exact book value, not
        `quantity x average`, or the source keeps a residual against no stock.
        """
        for quantity, cost, reference in (
            ("10", "333.333333", "A"),
            ("7", "777.777777", "B"),
            ("3", "111.111111", "C"),
        ):
            _seed_stock(
                group_manager,
                organization,
                branch,
                main_store,
                rice,
                quantity,
                cost,
                reference=f"DN-{reference}",
            )
        book_value = _balance(main_store, rice).value

        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "20")
        dispatch_transfer(actor=group_manager, transfer=transfer)

        after = _balance(main_store, rice)
        assert after.quantity == ZERO
        assert after.value == ZERO
        assert _balance(_transit_of(branch), rice).value == book_value

    def test_dispatching_more_than_is_there_is_refused(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "500")
        with pytest.raises(ValidationError) as caught:
            dispatch_transfer(actor=group_manager, transfer=transfer)
        assert caught.value.code == "insufficient_stock"

    def test_an_empty_transfer_cannot_be_dispatched(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        stocked: None,
    ) -> None:
        transfer = create_transfer(
            actor=group_manager,
            organization=organization,
            source_warehouse=main_store,
            destination_warehouse=kitchen_store,
            effective_at=WHEN,
            evidence_reference="TN-1",
        )
        with pytest.raises(ValidationError) as caught:
            dispatch_transfer(actor=group_manager, transfer=transfer)
        assert caught.value.code == "no_lines"

    def test_one_item_cannot_take_two_lines(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "10")
        with pytest.raises(ValidationError) as caught:
            add_transfer_line(
                actor=group_manager,
                transfer=transfer,
                line=TransferLineInput(item=rice, base_quantity=Decimal("5")),
            )
        assert caught.value.code == "duplicate_valuation_key"


# ---------------------------------------------------------------------------
# Partial receipt and value allocation
# ---------------------------------------------------------------------------


class TestAllocation:
    def test_the_final_share_takes_the_exact_remainder(self) -> None:
        """
        The pure rule, without a database: three partial takes of an
        indivisible value must sum back to it.
        """
        remaining_quantity = Decimal("3")
        remaining_value = Decimal("100.000")
        taken = []
        for quantity in (Decimal("1"), Decimal("1"), Decimal("1")):
            value = allocate(
                remaining_quantity=remaining_quantity,
                remaining_value=remaining_value,
                taken_quantity=quantity,
            )
            taken.append(value)
            remaining_quantity -= quantity
            remaining_value -= value
        assert sum(taken) == Decimal("100.000")
        assert remaining_value == ZERO


class TestReceipt:
    def test_a_partial_receipt_leaves_the_rest_in_transit(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        _receive(group_manager, transfer, "25")

        transfer.refresh_from_db()
        assert transfer.status == StockTransferStatus.PARTIALLY_RECEIVED
        line = transfer.lines.get()
        assert line.remaining_quantity == Decimal("15.000")
        assert line.remaining_value == Decimal("22500.000")

        assert _balance(kitchen_store, rice).quantity == Decimal("25.000")
        assert _balance(kitchen_store, rice).value == Decimal("37500.000")
        assert _balance(_transit_of(branch), rice).quantity == Decimal("15.000")

    def test_several_partial_receipts_complete_the_transfer(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        _receive(group_manager, transfer, "10", reference="GRN-1")
        _receive(group_manager, transfer, "10", reference="GRN-2")
        _receive(group_manager, transfer, "20", reference="GRN-3")

        transfer.refresh_from_db()
        assert transfer.status == StockTransferStatus.COMPLETED
        line = transfer.lines.get()
        assert line.remaining_quantity == ZERO
        assert line.remaining_value == ZERO

        assert _balance(kitchen_store, rice).quantity == Decimal("40.000")
        assert _balance(kitchen_store, rice).value == Decimal("60000.000")
        transit = _balance(_transit_of(branch), rice)
        assert transit.quantity == ZERO
        assert transit.value == ZERO

    def test_the_final_receipt_takes_the_exact_remaining_value(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """A value that does not divide by three must still sum back exactly."""
        _seed_stock(group_manager, organization, branch, main_store, rice, "3", "333.333333")
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "3")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        dispatched = _value(transfer.lines.get().total_value)

        received = ZERO
        for quantity in ("1", "1", "1"):
            receipt = _receive(group_manager, transfer, quantity, reference=f"GRN-{quantity}")
            received += _value(receipt.lines.get().allocated_value)
        assert received == dispatched
        assert _balance(kitchen_store, rice).value == dispatched
        assert _balance(_transit_of(branch), rice).value == ZERO

    def test_over_receipt_is_refused(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        with pytest.raises(ValidationError) as caught:
            _receive(group_manager, transfer, "41")
        assert caught.value.code == "receipt_exceeds_remaining"

    def test_the_pooled_in_transit_average_is_not_used(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """
        Two transfers of the same item at different costs sit in one in-transit
        position, so its average is a blend of both. Each receipt must still
        take **its own** transfer's value, not the blend — otherwise one
        branch's goods arrive carrying the other's cost.
        """
        _seed_stock(
            group_manager,
            organization,
            branch,
            main_store,
            rice,
            "10",
            "1000",
            reference="DN-CHEAP",
        )
        cheap = _transfer(group_manager, organization, main_store, kitchen_store, rice, "10")
        dispatch_transfer(actor=group_manager, transfer=cheap)

        _seed_stock(
            group_manager,
            organization,
            branch,
            main_store,
            rice,
            "10",
            "3000",
            reference="DN-DEAR",
        )
        dear = _transfer(group_manager, organization, main_store, kitchen_store, rice, "10")
        dispatch_transfer(actor=group_manager, transfer=dear)

        pooled = _balance(_transit_of(branch), rice)
        assert pooled.quantity == Decimal("20.000")
        assert pooled.average_cost == Decimal("2000.000000")  # the blend

        receipt = _receive(group_manager, cheap, "10", reference="GRN-CHEAP")
        assert receipt.lines.get().allocated_value == Decimal("10000.000")
        assert _balance(kitchen_store, rice).value == Decimal("10000.000")
        # ...and the dear transfer's value is untouched, still in transit.
        assert _balance(_transit_of(branch), rice).value == Decimal("30000.000")

    def test_the_same_branch_receipt_journal_is_one_branch_local_entry(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        control_account: Account,
        transit_account: Account,
        clearing_account: Account,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        receipt = _receive(group_manager, transfer, "40")

        assert receipt.source_journal_entry_id == receipt.destination_journal_entry_id
        journal = receipt.destination_journal_entry
        assert journal is not None
        lines = list(journal.lines.all())
        assert {line.account.code for line in lines} == {
            control_account.code,
            transit_account.code,
        }
        assert clearing_account.code not in {line.account.code for line in lines}
        assert {line.branch_id for line in lines} == {branch.pk}

    def test_a_cross_branch_receipt_writes_two_balanced_journals(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        second_branch: Branch,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        control_account: Account,
        transit_account: Account,
        clearing_account: Account,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, far_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        receipt = _receive(group_manager, transfer, "40")

        assert receipt.source_journal_entry_id != receipt.destination_journal_entry_id

        source = receipt.source_journal_entry
        destination = receipt.destination_journal_entry
        assert source is not None and destination is not None

        source_lines = list(source.lines.all())
        assert {line.branch_id for line in source_lines} == {branch.pk}
        assert sum(line.debit for line in source_lines) == sum(line.credit for line in source_lines)
        assert {line.account.code for line in source_lines if line.debit} == {clearing_account.code}
        assert {line.account.code for line in source_lines if line.credit} == {transit_account.code}

        destination_lines = list(destination.lines.all())
        assert {line.branch_id for line in destination_lines} == {second_branch.pk}
        assert sum(line.debit for line in destination_lines) == sum(
            line.credit for line in destination_lines
        )
        assert {line.account.code for line in destination_lines if line.debit} == {
            control_account.code
        }
        assert {line.account.code for line in destination_lines if line.credit} == {
            clearing_account.code
        }

    def test_inter_branch_clearing_nets_to_zero(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        clearing_account: Account,
    ) -> None:
        from apps.accounting.models import JournalLine

        transfer = _transfer(group_manager, organization, main_store, far_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        _receive(group_manager, transfer, "40")

        lines = JournalLine.objects.filter(account=clearing_account)
        assert sum(line.debit - line.credit for line in lines) == ZERO

    def test_an_unmapped_clearing_role_rolls_everything_back(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        clearing_account: Account,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, far_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)

        # Withdraw the mapping rather than closing its range: the dispatch
        # never touched inter-branch clearing, so the row is unused and this is
        # the correction path for one recorded in error.
        archive_account_mapping(
            mapping=organization.account_mappings.get(account_role__code=INTER_BRANCH_CLEARING),
            reason="recorded in error",
        )

        before = _balance(_transit_of(branch), rice).value
        with pytest.raises(ValidationError) as caught:
            _receive(group_manager, transfer, "40")
        assert caught.value.code == "account_role_unmapped"

        transfer.refresh_from_db()
        assert transfer.status == StockTransferStatus.DISPATCHED
        assert transfer.lines.get().remaining_quantity == Decimal("40.000")
        assert _balance(_transit_of(branch), rice).value == before
        assert _maybe_balance(far_store, rice) is None

    def test_a_receipt_id_under_another_transfer_is_a_404(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        first = _transfer(group_manager, organization, main_store, kitchen_store, rice, "10")
        dispatch_transfer(actor=group_manager, transfer=first)
        receipt = create_transfer_receipt(
            actor=group_manager,
            transfer=first,
            effective_at=WHEN,
            evidence_reference="GRN-1",
        )
        second = _transfer(group_manager, organization, main_store, kitchen_store, rice, "10")
        dispatch_transfer(actor=group_manager, transfer=second)

        assert resolve_receipt(group_manager, receipt.pk, transfer=first).pk == receipt.pk
        with pytest.raises(OutOfScope):
            resolve_receipt(group_manager, receipt.pk, transfer=second)


# ---------------------------------------------------------------------------
# Business dates and periods
# ---------------------------------------------------------------------------


class TestBusinessDates:
    def test_the_two_branches_may_resolve_to_different_dates(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        second_branch: Branch,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        """
        A late-night arrival: the destination branch's day has already started
        at 02:00 while the source's has not. Two different operating days for
        one physical moment, and each side is dated by its own.
        """
        second_branch.business_day_start_time = datetime.time(2, 0)
        second_branch.save(update_fields=["business_day_start_time"])

        late = datetime.datetime(TEST_YEAR, 3, 16, 3, 0, tzinfo=BAGHDAD)
        transfer = _transfer(group_manager, organization, main_store, far_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)

        receipt = create_transfer_receipt(
            actor=group_manager,
            transfer=transfer,
            effective_at=late,
            evidence_reference="GRN-LATE",
        )
        replace_transfer_receipt_lines(
            actor=group_manager,
            receipt=receipt,
            lines=[
                ReceiptLineInput(transfer_line=transfer.lines.get(), base_quantity=Decimal("40"))
            ],
        )
        posted = post_transfer_receipt(actor=group_manager, receipt=receipt)

        # Source cutoff 09:00 -> still the 15th. Destination cutoff 02:00 -> the 16th.
        assert posted.source_business_date == datetime.date(TEST_YEAR, 3, 15)
        assert posted.business_date == datetime.date(TEST_YEAR, 3, 16)
        source_journal = posted.source_journal_entry
        destination_journal = posted.destination_journal_entry
        assert source_journal is not None and destination_journal is not None
        assert source_journal.accounting_date == datetime.date(TEST_YEAR, 3, 15)
        assert destination_journal.accounting_date == datetime.date(TEST_YEAR, 3, 16)

    def test_a_closed_source_period_refuses_the_whole_receipt(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, far_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        _close_period(organization, datetime.date(TEST_YEAR, 3, 1))

        with pytest.raises(ValidationError) as caught:
            _receive(group_manager, transfer, "40")
        assert caught.value.code in {"period_not_open", "period_closed"}
        assert _maybe_balance(far_store, rice) is None
        assert _balance(_transit_of(branch), rice).quantity == Decimal("40.000")

    def test_a_closed_destination_period_refuses_the_whole_receipt(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        second_branch: Branch,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        """
        The destination's day falls in April while the source's is still in
        March, so closing April refuses the receipt from the far side alone.
        """
        second_branch.business_day_start_time = datetime.time(2, 0)
        second_branch.save(update_fields=["business_day_start_time"])
        transfer = _transfer(group_manager, organization, main_store, far_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)

        _close_period(organization, datetime.date(TEST_YEAR, 4, 1))
        late = datetime.datetime(TEST_YEAR, 4, 1, 3, 0, tzinfo=BAGHDAD)
        receipt = create_transfer_receipt(
            actor=group_manager,
            transfer=transfer,
            effective_at=late,
            evidence_reference="GRN-LATE",
        )
        replace_transfer_receipt_lines(
            actor=group_manager,
            receipt=receipt,
            lines=[
                ReceiptLineInput(transfer_line=transfer.lines.get(), base_quantity=Decimal("40"))
            ],
        )
        with pytest.raises(ValidationError) as caught:
            post_transfer_receipt(actor=group_manager, receipt=receipt)
        assert caught.value.code in {"period_not_open", "period_closed"}
        assert _maybe_balance(far_store, rice) is None
        assert _balance(_transit_of(branch), rice).quantity == Decimal("40.000")


def _close_period(organization: Organization, first_day: datetime.date) -> None:
    period = AccountingPeriod.objects.get(
        fiscal_year__organization=organization, start_date=first_day
    )
    AccountingPeriod.objects.filter(pk=period.pk).update(state=PeriodState.CLOSED)


# ---------------------------------------------------------------------------
# Shortage
# ---------------------------------------------------------------------------


class TestShortage:
    def test_a_closure_takes_the_exact_remaining_value(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        warehouse_center: CostCenter,
        shortage_account: Account,
        transit_account: Account,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        _receive(group_manager, transfer, "25")
        shortage = _close_short(group_manager, transfer, warehouse_center)

        transfer.refresh_from_db()
        assert transfer.status == StockTransferStatus.CLOSED_WITH_SHORTAGE
        line = transfer.lines.get()
        assert line.remaining_quantity == ZERO
        assert line.remaining_value == ZERO

        shortage_line = shortage.lines.get()
        assert shortage_line.base_quantity == Decimal("15.000")
        assert shortage_line.allocated_value == Decimal("22500.000")

        transit = _balance(_transit_of(branch), rice)
        assert transit.quantity == ZERO
        assert transit.value == ZERO

        journal = shortage.journal_entry
        assert journal is not None
        debit = journal.lines.get(debit__gt=0)
        assert debit.account_id == shortage_account.pk
        assert debit.cost_center_id == warehouse_center.pk
        assert debit.debit == Decimal("22500.000")
        credit = journal.lines.get(credit__gt=0)
        assert credit.account_id == transit_account.pk

    def test_receipts_plus_shortage_equal_the_dispatch(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        mapped: None,
        warehouse_center: CostCenter,
    ) -> None:
        _seed_stock(group_manager, organization, branch, main_store, rice, "7", "777.777777")
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "7")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        dispatched = _value(transfer.lines.get().total_value)

        first = _receive(group_manager, transfer, "2", reference="GRN-1")
        second = _receive(group_manager, transfer, "2", reference="GRN-2")
        shortage = _close_short(group_manager, transfer, warehouse_center)

        total = (
            _value(first.lines.get().allocated_value)
            + _value(second.lines.get().allocated_value)
            + _value(shortage.lines.get().allocated_value)
        )
        assert total == dispatched
        assert _balance(_transit_of(branch), rice).value == ZERO

    def test_a_closure_needs_a_reason(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        warehouse_center: CostCenter,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        with pytest.raises(ValidationError) as caught:
            create_transfer_shortage(
                actor=group_manager,
                transfer=transfer,
                effective_at=WHEN,
                reason="   ",
                evidence_reference="CLAIM-1",
                cost_center=warehouse_center,
            )
        assert caught.value.code == "shortage_reason_required"

    def test_a_closure_cannot_exist_without_a_cost_center(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        """
        The column is NOT NULL, so there is no default to silently fall back
        on and no code path that could invent one. Asserted at the database
        rather than through the service, because the service's signature
        already makes the argument mandatory — the question worth asking is
        whether anything *else* could write a row without one.
        """
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO inventory_stocktransfershortage (
                        transfer_id, public_id, shortage_number, status, reason,
                        evidence_reference, cost_center_id, effective_at,
                        business_date, business_date_timezone,
                        created_at, updated_at
                    ) VALUES (
                        %s, gen_random_uuid(), '', 'DRAFT', 'ضاعت', 'CLAIM-1',
                        NULL, now(), current_date, '', now(), now()
                    )
                    """,
                    [transfer.pk],
                )

    def test_a_storekeeper_cannot_close_a_shortage(
        self,
        group_manager: User,
        keeper: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        warehouse_center: CostCenter,
    ) -> None:
        """Turning missing stock into an expense is not a custody act."""
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        with pytest.raises(PermissionDenied):
            create_transfer_shortage(
                actor=keeper,
                transfer=transfer,
                effective_at=WHEN,
                reason="ضاعت",
                evidence_reference="CLAIM-1",
                cost_center=warehouse_center,
            )

    def test_only_one_closure_can_be_active(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        warehouse_center: CostCenter,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        _close_short(group_manager, transfer, warehouse_center)
        with pytest.raises(ValidationError) as caught:
            _close_short(group_manager, transfer, warehouse_center)
        assert caught.value.code == "transfer_not_open"


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


class TestReversal:
    def test_reversing_a_receipt_restores_transit_exactly(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        receipt = _receive(group_manager, transfer, "25")
        reverse_transfer_receipt(actor=group_manager, receipt=receipt, reason="سُجل بالخطأ")

        transfer.refresh_from_db()
        assert transfer.status == StockTransferStatus.DISPATCHED
        line = transfer.lines.get()
        assert line.remaining_quantity == Decimal("40.000")
        assert line.remaining_value == Decimal("60000.000")

        transit = _balance(_transit_of(branch), rice)
        assert transit.quantity == Decimal("40.000")
        assert transit.value == Decimal("60000.000")
        destination = _balance(kitchen_store, rice)
        assert destination.quantity == ZERO
        assert destination.value == ZERO

    def test_consumed_destination_stock_blocks_a_receipt_reversal(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        warehouse_center: CostCenter,
    ) -> None:
        from apps.accounting.models import INVENTORY_CONSUMPTION

        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=INVENTORY_CONSUMPTION),
            account=Account.objects.get(organization=organization, code="5-01-02-001"),
            effective_from=JAN_1,
        )
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        receipt = _receive(group_manager, transfer, "40")

        issue = create_document(
            actor=group_manager,
            organization=organization,
            branch=branch,
            warehouse=kitchen_store,
            document_type=InventoryDocumentType.ISSUE,
            effective_at=WHEN,
            evidence_reference="REQ-1",
            cost_center=warehouse_center,
        )
        add_document_line(
            actor=group_manager,
            document=issue,
            line=DocumentLineInput(item=rice, base_quantity=Decimal("40")),
        )
        post_document(actor=group_manager, document=issue)

        with pytest.raises(ValidationError) as caught:
            reverse_transfer_receipt(actor=group_manager, receipt=receipt, reason="خطأ")
        assert caught.value.code == "insufficient_stock"

    def test_reversing_a_shortage_reopens_the_transfer(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        warehouse_center: CostCenter,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        _receive(group_manager, transfer, "25")
        shortage = _close_short(group_manager, transfer, warehouse_center)
        reverse_transfer_shortage(actor=group_manager, shortage=shortage, reason="عُثر على البضاعة")

        transfer.refresh_from_db()
        assert transfer.status == StockTransferStatus.PARTIALLY_RECEIVED
        line = transfer.lines.get()
        assert line.remaining_quantity == Decimal("15.000")
        assert line.remaining_value == Decimal("22500.000")
        assert _balance(_transit_of(branch), rice).quantity == Decimal("15.000")

    def test_a_dispatch_cannot_be_reversed_while_a_receipt_stands(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        _receive(group_manager, transfer, "10")
        transfer.refresh_from_db()
        with pytest.raises(ValidationError) as caught:
            reverse_dispatch(actor=group_manager, transfer=transfer, reason="خطأ")
        assert caught.value.code in {
            "transfer_has_active_receipts",
            "dispatch_not_reversible",
        }

    def test_a_dispatch_cannot_be_reversed_while_a_shortage_stands(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        warehouse_center: CostCenter,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        _close_short(group_manager, transfer, warehouse_center)
        transfer.refresh_from_db()
        with pytest.raises(ValidationError) as caught:
            reverse_dispatch(actor=group_manager, transfer=transfer, reason="خطأ")
        assert caught.value.code in {
            "transfer_has_active_shortage",
            "dispatch_not_reversible",
        }

    def test_a_dispatch_reverses_when_everything_is_still_in_transit(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        reversed_transfer = reverse_dispatch(
            actor=group_manager, transfer=transfer, reason="أُلغي التحويل"
        )

        assert reversed_transfer.status == StockTransferStatus.REVERSED
        source = _balance(main_store, rice)
        assert source.quantity == Decimal("100.000")
        assert source.value == Decimal("150000.000")
        transit = _balance(_transit_of(branch), rice)
        assert transit.quantity == ZERO
        assert transit.value == ZERO

    def test_a_dispatch_reversal_after_a_reversed_receipt_is_allowed(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        """A reversed receipt is no longer active, so the transfer reopens fully."""
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        receipt = _receive(group_manager, transfer, "40")
        reverse_transfer_receipt(actor=group_manager, receipt=receipt, reason="خطأ")
        transfer.refresh_from_db()
        reverse_dispatch(actor=group_manager, transfer=transfer, reason="أُلغي")

        assert _balance(main_store, rice).quantity == Decimal("100.000")
        assert _balance(_transit_of(branch), rice).quantity == ZERO

    def test_double_reversal_is_refused(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        receipt = _receive(group_manager, transfer, "10")
        reverse_transfer_receipt(actor=group_manager, receipt=receipt, reason="خطأ")
        with pytest.raises(ValidationError) as caught:
            reverse_transfer_receipt(actor=group_manager, receipt=receipt, reason="خطأ")
        assert caught.value.code == "already_reversed"

    def test_a_cross_branch_receipt_reversal_mirrors_both_journals(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        clearing_account: Account,
    ) -> None:
        from apps.accounting.models import JournalLine

        transfer = _transfer(group_manager, organization, main_store, far_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        receipt = _receive(group_manager, transfer, "40")
        reversed_receipt = reverse_transfer_receipt(
            actor=group_manager, receipt=receipt, reason="خطأ"
        )

        assert (
            reversed_receipt.source_reversal_journal_entry_id
            != reversed_receipt.destination_reversal_journal_entry_id
        )
        lines = JournalLine.objects.filter(account=clearing_account)
        assert sum(line.debit - line.credit for line in lines) == ZERO


# ---------------------------------------------------------------------------
# Conversions and lots
# ---------------------------------------------------------------------------


class TestConversionsAndLots:
    @pytest.fixture
    def sacked(
        self, organization: Organization, rice: InventoryItem, sack: PackageUnit
    ) -> ItemPackageConversion:
        return create_item_conversion(
            item=rice,
            package_unit=sack,
            factor_to_base=Decimal("25"),
            conversion_type=ConversionType.FIXED,
            effective_from=JAN_1,
        )

    def test_the_dispatch_conversion_snapshot_survives_a_new_factor(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        sack: PackageUnit,
        sacked: ItemPackageConversion,
        stocked: None,
    ) -> None:
        """
        Two sacks dispatched at 25 kg each. The factor is then re-versioned to
        30, and the receipt still means 50 kg — the shipment is what it was.
        """
        transfer = _transfer(
            group_manager,
            organization,
            main_store,
            kitchen_store,
            rice,
            "",
            conversion=sacked,
            packages="2",
        )
        dispatch_transfer(actor=group_manager, transfer=transfer)
        line = transfer.lines.get()
        assert line.base_quantity == Decimal("50.000")
        assert line.package_conversion_id == sacked.pk

        from apps.inventory.services import supersede_item_conversion

        supersede_item_conversion(
            conversion=sacked,
            factor_to_base=Decimal("30"),
            effective_from=datetime.date(TEST_YEAR, 3, 16),
        )
        receipt = _receive(group_manager, transfer, "50")
        assert receipt.lines.get().base_quantity == Decimal("50.000")

    def test_a_variable_package_receipt_needs_its_measurement(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        carton: PackageUnit,
        stocked: None,
    ) -> None:
        variable = create_item_conversion(
            item=rice,
            package_unit=carton,
            factor_to_base=Decimal("12"),
            conversion_type=ConversionType.VARIABLE,
            effective_from=JAN_1,
        )
        transfer = _transfer(
            group_manager,
            organization,
            main_store,
            kitchen_store,
            rice,
            "",
            conversion=variable,
            packages="2",
            measured="23.4",
        )
        dispatch_transfer(actor=group_manager, transfer=transfer)
        assert transfer.lines.get().base_quantity == Decimal("23.400")

        receipt = create_transfer_receipt(
            actor=group_manager,
            transfer=transfer,
            effective_at=WHEN,
            evidence_reference="GRN-1",
        )
        with pytest.raises(ValidationError) as caught:
            replace_transfer_receipt_lines(
                actor=group_manager,
                receipt=receipt,
                lines=[
                    ReceiptLineInput(
                        transfer_line=transfer.lines.get(),
                        entered_package_quantity=Decimal("2"),
                    )
                ],
            )
        assert caught.value.code == "measured_quantity_required"

    def test_the_lot_survives_the_journey(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        leaf_category: object,
        kilogram: UnitOfMeasure,
        mapped: None,
    ) -> None:
        chicken = create_item(
            organization=organization,
            code="CHK-1",
            name="دجاج",
            category=leaf_category,  # type: ignore[arg-type]
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
            tracks_lots=True,
        )
        lot = InventoryLot.objects.create(organization=organization, item=chicken, code="L-1")
        _seed_stock(group_manager, organization, branch, main_store, chicken, "20", "5000", lot=lot)
        transfer = _transfer(
            group_manager, organization, main_store, kitchen_store, chicken, "20", lot=lot
        )
        dispatch_transfer(actor=group_manager, transfer=transfer)
        _receive(group_manager, transfer, "20")

        arrived = StockBalance.objects.get(warehouse=kitchen_store, item=chicken, lot=lot)
        assert arrived.quantity == Decimal("20.000")
        assert (
            StockBalance.objects.filter(warehouse=kitchen_store, item=chicken, lot=None).count()
            == 0
        )


# ---------------------------------------------------------------------------
# Identity, immutability, and the conditional control-account invariant
# ---------------------------------------------------------------------------


class TestIdentityAndImmutability:
    def test_the_source_identities_are_distinct_per_side(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, far_store, rice, "40")
        dispatched = dispatch_transfer(actor=group_manager, transfer=transfer)
        receipt = _receive(group_manager, transfer, "40")

        dispatch_entry = dispatched.stock_entry
        release = receipt.source_stock_entry
        arrival = receipt.destination_stock_entry
        assert dispatch_entry is not None
        assert release is not None and arrival is not None
        assert dispatch_entry.source_document_type == "INVENTORY_TRANSFER_DISPATCH"
        assert dispatch_entry.source_document_id == str(transfer.public_id)
        assert release.source_document_type == "INVENTORY_TRANSFER_RECEIPT_SOURCE"
        assert arrival.source_document_type == "INVENTORY_TRANSFER_RECEIPT_DESTINATION"
        assert release.source_document_id == str(receipt.public_id)

    def test_reposting_a_posted_receipt_is_refused(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        receipt = _receive(group_manager, transfer, "20")
        with pytest.raises(ValidationError) as caught:
            post_transfer_receipt(actor=group_manager, receipt=receipt)
        assert caught.value.code == "already_posted"

    def test_a_dispatched_transfer_cannot_be_edited(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        with pytest.raises(IntegrityError), transaction.atomic():
            StockTransfer.objects.filter(pk=transfer.pk).update(evidence_reference="tampered")

    def test_a_dispatched_transfer_cannot_be_deleted(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        with pytest.raises(IntegrityError), transaction.atomic():
            StockTransfer.objects.filter(pk=transfer.pk).delete()

    def test_a_posted_receipt_line_is_frozen(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        receipt = _receive(group_manager, transfer, "20")
        line = receipt.lines.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            type(line).objects.filter(pk=line.pk).update(base_quantity=Decimal("30"))

    def test_a_journalled_posting_names_an_account_for_every_dinar(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        """
        §S, at the database: a posting that reached the general ledger must
        name an account for every dinar it moved.

        Planted as a fresh unaccounted movement under an already-journalled
        entry, which is the shape a future posting path could produce by
        forgetting to resolve one. The check is a deferred constraint trigger —
        posting legitimately writes the entry, then its movements, then the
        journal — so it is forced with `SET CONSTRAINTS ALL IMMEDIATE` rather
        than waiting for a commit the test transaction never makes.
        """
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatched = dispatch_transfer(actor=group_manager, transfer=transfer)
        entry = dispatched.stock_entry
        assert entry is not None
        assert entry.journal_entry_id is not None
        original = entry.movements.first()
        assert original is not None
        assert original.control_account_id is not None

        with pytest.raises(IntegrityError), transaction.atomic():
            StockMovement.objects.create(
                entry=entry,
                organization_id=original.organization_id,
                branch_id=original.branch_id,
                warehouse=original.warehouse,
                item=original.item,
                lot=original.lot,
                movement_type=MovementType.RECEIPT,
                control_account=None,
                effect_key="planted-without-an-account",
                base_quantity=Decimal("1.000"),
                inventory_value=Decimal("1.000"),
                unit_cost=Decimal("1.000000"),
                quantity_before=Decimal("0.000"),
                quantity_after=Decimal("1.000"),
                value_before=Decimal("0.000"),
                value_after=Decimal("1.000"),
                average_before=Decimal("0.000000"),
                average_after=Decimal("1.000000"),
                posted_sequence=900000,
                effective_at=entry.effective_at,
            )
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_a_bare_kernel_posting_still_needs_no_account(
        self,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        """
        The invariant is conditional, and that is the point: a posting that
        never reached the general ledger keeps working without one.
        """
        from apps.inventory.ledger import MovementInput, post_stock_entry

        entry = post_stock_entry(
            organization=organization,
            effects=[
                MovementInput(
                    warehouse=main_store,
                    item=rice,
                    movement_type=MovementType.OPENING,
                    quantity=Decimal("5"),
                    effect_key="bare",
                    unit_cost=Decimal("100"),
                )
            ],
            idempotency_key="bare-kernel",
        )
        assert entry.journal_entry_id is None
        assert entry.movements.get().control_account_id is None


# ---------------------------------------------------------------------------
# Permissions and scope
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_a_storekeeper_may_dispatch_within_their_branch(
        self,
        keeper: User,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        transfer = _transfer(keeper, organization, main_store, kitchen_store, rice, "10")
        dispatched = dispatch_transfer(actor=keeper, transfer=transfer)
        assert dispatched.status == StockTransferStatus.DISPATCHED

    def test_a_storekeeper_cannot_reach_another_branch_as_a_destination(
        self,
        keeper: User,
        organization: Organization,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        with pytest.raises(OutOfScope):
            create_transfer(
                actor=keeper,
                organization=organization,
                source_warehouse=main_store,
                destination_warehouse=far_store,
                effective_at=WHEN,
                evidence_reference="TN-1",
            )

    def test_an_accounting_manager_may_close_a_shortage_but_not_dispatch(
        self,
        accounting_manager: User,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        warehouse_center: CostCenter,
    ) -> None:
        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        with pytest.raises(PermissionDenied):
            dispatch_transfer(actor=accounting_manager, transfer=transfer)

        dispatch_transfer(actor=group_manager, transfer=transfer)
        shortage = _close_short(accounting_manager, transfer, warehouse_center)
        assert shortage.status == InventoryDocumentStatus.POSTED

    def test_the_role_map_grants_shortage_closure_to_exactly_three_roles(self) -> None:
        from apps.inventory.permissions import (
            CLOSE_TRANSFER_SHORTAGE,
            ROLE_PERMISSIONS,
        )

        holders = {
            role
            for role, permissions in ROLE_PERMISSIONS.items()
            if CLOSE_TRANSFER_SHORTAGE in permissions
        }
        assert holders == {"OWNER", "MANAGER", "ACCOUNTING_MANAGER"}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestReads:
    def test_both_ends_can_see_the_transfer(
        self,
        keeper: User,
        group_manager: User,
        organization: Organization,
        second_branch: Branch,
        main_store: Warehouse,
        far_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        far_keeper = User.objects.create_user(username="far-keeper", password="pw-not-real-1234")
        grant_branch_access(user=far_keeper, branch=second_branch, role=Role.STOREKEEPER)
        far_keeper = User.objects.get(pk=far_keeper.pk)

        transfer = _transfer(group_manager, organization, main_store, far_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)

        assert transfer in visible_transfers(keeper)
        assert transfer in visible_transfers(far_keeper)

    def test_reconciliation_is_clean_after_a_full_cycle(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        far_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        warehouse_center: CostCenter,
    ) -> None:
        """
        A cross-branch transfer partly received and closed short, a same-branch
        one completed, and one receipt reversed — then every comparison must
        still agree.
        """
        from apps.inventory.reconciliation import verify_inventory_accounting

        far = _transfer(group_manager, organization, main_store, far_store, rice, "30")
        dispatch_transfer(actor=group_manager, transfer=far)
        _receive(group_manager, far, "12", reference="GRN-F1")
        _close_short(group_manager, far, warehouse_center)

        near = _transfer(group_manager, organization, main_store, kitchen_store, rice, "20")
        dispatch_transfer(actor=group_manager, transfer=near)
        undone = _receive(group_manager, near, "20", reference="GRN-N1")
        reverse_transfer_receipt(actor=group_manager, receipt=undone, reason="خطأ")
        _receive(group_manager, near, "20", reference="GRN-N2")

        assert verify_inventory_accounting(organization) == []

    def test_a_planted_manual_journal_stays_visible_as_drift(
        self,
        group_manager: User,
        organization: Organization,
        branch: Branch,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
        transit_account: Account,
        clearing_account: Account,
    ) -> None:
        """
        No repair mode. A hand-written journal against the in-transit control
        account is reported and left standing — that drift is exactly what the
        report exists to surface.
        """
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine
        from apps.inventory.reconciliation import verify_inventory_accounting

        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        assert verify_inventory_accounting(organization) == []

        post_entry(
            organization=organization,
            accounting_date=datetime.date(TEST_YEAR, 3, 15),
            lines=[
                PostingLine(account=transit_account, branch=branch, debit=Decimal("5000")),
                PostingLine(account=clearing_account, branch=branch, credit=Decimal("5000")),
            ],
            idempotency_key="planted-by-hand",
        )
        problems = verify_inventory_accounting(organization)
        assert any("inventory_vs_gl" in problem for problem in problems), problems

    def test_a_tampered_remaining_balance_is_reported(
        self,
        group_manager: User,
        organization: Organization,
        main_store: Warehouse,
        kitchen_store: Warehouse,
        rice: InventoryItem,
        stocked: None,
    ) -> None:
        """The retained remaining balance is checked against its own children."""
        from apps.inventory.reconciliation import verify_inventory_accounting

        transfer = _transfer(group_manager, organization, main_store, kitchen_store, rice, "40")
        dispatch_transfer(actor=group_manager, transfer=transfer)
        _receive(group_manager, transfer, "10")

        line = transfer.lines.get()
        StockTransferLine.objects.filter(pk=line.pk).update(remaining_quantity=Decimal("20.000"))
        problems = verify_inventory_accounting(organization)
        assert any("dispatched_quantity" in problem for problem in problems), problems
        assert any("in_transit_quantity" in problem for problem in problems), problems
