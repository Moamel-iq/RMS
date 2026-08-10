"""
Transfer events racing each other, at real COMMIT boundaries (Task 1.5 §R).

Every test here runs its work in real threads against a real database, so the
locks are the actual PostgreSQL locks and a deadlock is a real deadlock rather
than a story about one. `transaction=True` is what makes that possible: the
usual test transaction would keep both threads inside one connection and prove
nothing at all.

Eight races, and each protects a different figure:

  1. two receipts against one transfer      total received never exceeds dispatch
  2. a receipt against a shortage closure   the same, across two document kinds
  3. two shortage closures                  at most one active closure survives
  4. a receipt reversal against an issue    no negative stock, either way round
  5. two cross-branch receipts, opposite    no deadlock: keys are canonical
  6. a mapping mutation against a receipt   no mixed mapping and no deadlock
  7. an idempotent receipt retry            one posting only
  8. a cross-branch receipt, two journals   all-or-nothing across both branches
"""

from __future__ import annotations

import datetime
import threading
import zoneinfo
from concurrent.futures import ThreadPoolExecutor
from datetime import time as clock
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connections, transaction
from django.utils import timezone

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INTER_BRANCH_CLEARING,
    INVENTORY_CONSUMPTION,
    INVENTORY_CONTROL,
    INVENTORY_IN_TRANSIT,
    INVENTORY_SHORTAGE_LOSS,
    Account,
    AccountRole,
    CostCenter,
    JournalEntry,
)
from apps.accounting.services import (
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
    reverse_transfer_receipt,
)
from apps.inventory.models import (
    InventoryDocumentStatus,
    InventoryDocumentType,
    StockBalance,
    StockTransfer,
    StockTransferLine,
    StockTransferReceipt,
    StockTransferShortage,
    Warehouse,
    WarehouseType,
)
from apps.inventory.operations import DocumentLineInput
from apps.inventory.transfers import ReceiptLineInput, TransferLineInput
from apps.organizations.models import Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_organization_access,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db(transaction=True)

BAGHDAD = zoneinfo.ZoneInfo("Asia/Baghdad")
ZERO = Decimal("0")

#: How long a racing thread holds its transaction open after doing its work,
#: so the other thread is certainly waiting on a lock rather than merely slow.
LINGER = 0.6


@pytest.fixture
def world(django_db_setup: Any, django_db_blocker: Any) -> dict[str, Any]:
    """Two branches, mapped accounts, stocked shelves, and an actor."""
    from apps.inventory.services import create_item, create_item_category, create_warehouse

    call_command("seed_units", verbosity=0)
    organization = create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")
    first = create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=clock(9, 0),
    )
    second = create_branch(
        organization=organization,
        code="KARRADA",
        name_ar="الكرادة",
        name_en="Karrada",
        business_day_start_time=clock(9, 0),
    )
    year = timezone.localdate().year
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=year)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)

    effective = datetime.date(year, 1, 1)
    accounts = {
        INVENTORY_CONTROL: "1-03-01-001",
        INVENTORY_IN_TRANSIT: "1-03-02-001",
        INTER_BRANCH_CLEARING: "8-01-01-001",
        INVENTORY_SHORTAGE_LOSS: "6-02-01-001",
        GOODS_RECEIVED_NOT_INVOICED: "2-01-02-001",
        INVENTORY_CONSUMPTION: "5-01-02-001",
    }
    for code, account_code in accounts.items():
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=Account.objects.get(organization=organization, code=account_code),
            effective_from=effective,
        )

    root = create_item_category(organization=organization, code="FOOD", name_ar="أغذية")
    leaf = create_item_category(organization=organization, code="MEAT", name_ar="لحوم", parent=root)
    kilogram = UnitOfMeasure.objects.get(code="KG")
    rice = create_item(
        organization=organization,
        code="RICE-272",
        name_ar="رز",
        category=leaf,
        item_type="RAW_MATERIAL",
        base_unit=kilogram,
    )
    main = create_warehouse(branch=first, code="MAIN", name_ar="الرئيسي")
    kitchen = create_warehouse(branch=first, code="KITCHEN", name_ar="المطبخ")
    far = create_warehouse(branch=second, code="MAIN", name_ar="الكرادة")

    actor = User.objects.create_user(username="mover", password="pw-not-real-1234")
    grant_organization_access(user=actor, organization=organization, role=Role.OWNER)
    actor = User.objects.get(pk=actor.pk)

    world: dict[str, Any] = {
        "organization": organization,
        "first": first,
        "second": second,
        "main": main,
        "kitchen": kitchen,
        "far": far,
        "rice": rice,
        "actor": actor,
        "center": CostCenter.objects.get(organization=organization, code="WAREHOUSE"),
    }
    _receive(world, main, "1000", "1000")
    return world


def _receive(world: dict[str, Any], warehouse: Warehouse, quantity: str, cost: str) -> None:
    actor = world["actor"]
    document = create_document(
        actor=actor,
        organization=world["organization"],
        branch=warehouse.branch,
        warehouse=warehouse,
        document_type=InventoryDocumentType.RECEIPT,
        effective_at=timezone.now(),
        evidence_reference="DN-SEED",
    )
    add_document_line(
        actor=actor,
        document=document,
        line=DocumentLineInput(
            item=world["rice"], base_quantity=Decimal(quantity), unit_cost=Decimal(cost)
        ),
    )
    post_document(actor=actor, document=document)


def _dispatch(
    world: dict[str, Any], destination: Warehouse, quantity: str = "100"
) -> StockTransfer:
    actor = world["actor"]
    transfer = create_transfer(
        actor=actor,
        organization=world["organization"],
        source_warehouse=world["main"],
        destination_warehouse=destination,
        effective_at=timezone.now(),
        evidence_reference="TN",
    )
    add_transfer_line(
        actor=actor,
        transfer=transfer,
        line=TransferLineInput(item=world["rice"], base_quantity=Decimal(quantity)),
    )
    return dispatch_transfer(actor=actor, transfer=transfer)


def _draft_receipt(
    world: dict[str, Any], transfer: StockTransfer, quantity: str, reference: str
) -> StockTransferReceipt:
    actor = world["actor"]
    receipt = create_transfer_receipt(
        actor=actor,
        transfer=transfer,
        effective_at=timezone.now(),
        evidence_reference=reference,
    )
    replace_transfer_receipt_lines(
        actor=actor,
        receipt=receipt,
        lines=[
            ReceiptLineInput(transfer_line=transfer.lines.get(), base_quantity=Decimal(quantity))
        ],
    )
    return receipt


def _draft_shortage(world: dict[str, Any], transfer: StockTransfer) -> StockTransferShortage:
    return create_transfer_shortage(
        actor=world["actor"],
        transfer=transfer,
        effective_at=timezone.now(),
        reason="لم تصل",
        evidence_reference="CLAIM",
        cost_center=world["center"],
    )


def _in_thread(work: Any) -> Any:
    def runner() -> Any:
        try:
            return work()
        finally:
            connections.close_all()

    return runner


def _race(*jobs: Any) -> list[Any]:
    """Start every job at once and collect what each returned or raised."""
    barrier = threading.Barrier(len(jobs))

    def wrapped(job: Any) -> Any:
        def run() -> Any:
            barrier.wait(timeout=10)
            try:
                return job()
            except Exception as error:  # noqa: BLE001 - the outcome IS the result
                return error

        return _in_thread(run)

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(wrapped(job)) for job in jobs]
        return [future.result(timeout=120) for future in futures]


def _post_in_transaction(work: Any, *, linger: float = LINGER) -> Any:
    """Do the work, then hold the transaction open so the race is real."""
    with transaction.atomic():
        result = work()
        import time

        time.sleep(linger)
    return result


def _transit(branch: Any) -> Warehouse | None:
    return Warehouse.objects.filter(branch=branch, warehouse_type=WarehouseType.IN_TRANSIT).first()


def _maybe_balance(warehouse: Warehouse | None, item: Any) -> StockBalance | None:
    if warehouse is None:
        return None
    return StockBalance.objects.filter(warehouse=warehouse, item=item, lot=None).first()


def _balance(warehouse: Warehouse | None, item: Any) -> StockBalance:
    """The position, insisting it exists — these assertions are about figures."""
    balance = _maybe_balance(warehouse, item)
    assert balance is not None, f"no position in {warehouse}"
    return balance


def _resolved(transfer: StockTransfer) -> Decimal:
    """Quantity actually taken out of transit by posted children."""
    line = StockTransferLine.objects.get(transfer=transfer)
    return line.base_quantity - line.remaining_quantity


class TestReceiptRaces:
    def test_two_receipts_never_exceed_the_dispatch(self, world: dict[str, Any]) -> None:
        """
        Both claim 70 of a 100 dispatch. One wins outright or both split
        validly; what must never happen is 140 arriving.
        """
        transfer = _dispatch(world, world["kitchen"])
        first = _draft_receipt(world, transfer, "70", "GRN-1")
        second = _draft_receipt(world, transfer, "70", "GRN-2")

        results = _race(
            lambda: _post_in_transaction(
                lambda: post_transfer_receipt(actor=world["actor"], receipt=first)
            ),
            lambda: _post_in_transaction(
                lambda: post_transfer_receipt(actor=world["actor"], receipt=second)
            ),
        )
        succeeded = [result for result in results if not isinstance(result, Exception)]
        assert len(succeeded) == 1, results
        assert _resolved(transfer) == Decimal("70.000")
        assert _balance(world["kitchen"], world["rice"]).quantity == Decimal("70.000")

    def test_a_receipt_and_a_closure_never_double_resolve(self, world: dict[str, Any]) -> None:
        """
        A closure takes the *whole* remainder, so it and a receipt for part of
        it are competing for the same goods. Their sum must never exceed what
        was dispatched, however the scheduler orders them.
        """
        transfer = _dispatch(world, world["kitchen"])
        receipt = _draft_receipt(world, transfer, "60", "GRN-1")
        shortage = _draft_shortage(world, transfer)

        _race(
            lambda: _post_in_transaction(
                lambda: post_transfer_receipt(actor=world["actor"], receipt=receipt)
            ),
            lambda: _post_in_transaction(
                lambda: post_transfer_shortage(actor=world["actor"], shortage=shortage)
            ),
        )
        assert _resolved(transfer) <= Decimal("100.000")
        transit = _maybe_balance(_transit(world["first"]), world["rice"])
        assert transit is None or transit.quantity >= ZERO

    def test_two_closures_leave_at_most_one_active(self, world: dict[str, Any]) -> None:
        transfer = _dispatch(world, world["kitchen"])
        first = _draft_shortage(world, transfer)
        second = _draft_shortage(world, transfer)

        _race(
            lambda: _post_in_transaction(
                lambda: post_transfer_shortage(actor=world["actor"], shortage=first)
            ),
            lambda: _post_in_transaction(
                lambda: post_transfer_shortage(actor=world["actor"], shortage=second)
            ),
        )
        active = StockTransferShortage.objects.filter(
            transfer=transfer, status=InventoryDocumentStatus.POSTED
        ).count()
        assert active == 1
        assert _resolved(transfer) == Decimal("100.000")

    def test_a_receipt_reversal_racing_an_issue_never_goes_negative(
        self, world: dict[str, Any]
    ) -> None:
        """
        Whichever gets the stock first wins; the other is refused. What must
        never happen is both succeeding and the destination going negative.
        """
        transfer = _dispatch(world, world["kitchen"])
        receipt = _draft_receipt(world, transfer, "100", "GRN-1")
        post_transfer_receipt(actor=world["actor"], receipt=receipt)

        def issue() -> Any:
            actor = world["actor"]
            with transaction.atomic():
                document = create_document(
                    actor=actor,
                    organization=world["organization"],
                    branch=world["first"],
                    warehouse=world["kitchen"],
                    document_type=InventoryDocumentType.ISSUE,
                    effective_at=timezone.now(),
                    evidence_reference="REQ",
                    cost_center=world["center"],
                )
                add_document_line(
                    actor=actor,
                    document=document,
                    line=DocumentLineInput(item=world["rice"], base_quantity=Decimal("100")),
                )
                posted = post_document(actor=actor, document=document)
                import time

                time.sleep(LINGER)
            return posted

        results = _race(
            issue,
            lambda: _post_in_transaction(
                lambda: reverse_transfer_receipt(
                    actor=world["actor"], receipt=receipt, reason="خطأ"
                )
            ),
        )
        succeeded = [result for result in results if not isinstance(result, Exception)]
        assert len(succeeded) == 1, results
        balance = _maybe_balance(world["kitchen"], world["rice"])
        assert balance is None or balance.quantity >= ZERO

    def test_two_cross_branch_receipts_do_not_deadlock(self, world: dict[str, Any]) -> None:
        """
        Two transfers whose stock keys overlap, received at the same instant.

        Each receipt touches an in-transit key and a destination key through
        two separate kernel calls. Sorting inside each call is not enough —
        the keys have to be taken canonically **across the whole event**, or
        one receipt takes them in one order and the other in the opposite.
        """
        first = _dispatch(world, world["far"], "40")
        second = _dispatch(world, world["kitchen"], "40")
        first_receipt = _draft_receipt(world, first, "40", "GRN-A")
        second_receipt = _draft_receipt(world, second, "40", "GRN-B")

        results = _race(
            lambda: _post_in_transaction(
                lambda: post_transfer_receipt(actor=world["actor"], receipt=first_receipt),
                linger=0.3,
            ),
            lambda: _post_in_transaction(
                lambda: post_transfer_receipt(actor=world["actor"], receipt=second_receipt),
                linger=0.3,
            ),
        )
        for result in results:
            assert not isinstance(result, Exception), result
        assert _balance(world["far"], world["rice"]).quantity == Decimal("40.000")
        assert _balance(world["kitchen"], world["rice"]).quantity == Decimal("40.000")

    def test_a_mapping_mutation_racing_a_receipt_neither_mixes_nor_deadlocks(
        self, world: dict[str, Any]
    ) -> None:
        """
        The mapping lock, seen from the transfer side. A receipt resolves the
        destination's control account under the shared lock; a mutation waits
        for the exclusive form. Neither may deadlock, and the arriving stock
        must sit in exactly one account.
        """
        from apps.core.context import audit_context
        from apps.inventory.accounts import create_inventory_mapping

        transfer = _dispatch(world, world["kitchen"], "40")
        receipt = _draft_receipt(world, transfer, "40", "GRN-1")
        other = Account.objects.get(organization=world["organization"], code="1-03-01-001")

        def mutate() -> Any:
            with audit_context(actor=world["actor"]), transaction.atomic():
                mapping = create_inventory_mapping(
                    organization=world["organization"],
                    role=AccountRole.objects.get(code=INVENTORY_CONTROL),
                    account=other,
                    item=world["rice"],
                    effective_from=datetime.date(timezone.localdate().year, 1, 1),
                )
                import time

                time.sleep(0.3)
            return mapping

        results = _race(
            lambda: _post_in_transaction(
                lambda: post_transfer_receipt(actor=world["actor"], receipt=receipt), linger=0.3
            ),
            mutate,
        )
        for result in results:
            if isinstance(result, Exception):
                # A refusal is fine; a deadlock is not.
                assert "deadlock" not in str(result).lower(), result

        arrived = _maybe_balance(world["kitchen"], world["rice"])
        if arrived is not None and arrived.quantity > ZERO:
            accounts = {
                movement.control_account_id
                for movement in arrived.item.stock_movements.filter(
                    warehouse=world["kitchen"]
                ).exclude(inventory_value=ZERO)
            }
            assert len(accounts) == 1, accounts

    def test_an_idempotent_retry_posts_one_receipt_only(self, world: dict[str, Any]) -> None:
        """
        The same receipt submitted twice at once. One posting, one number, one
        set of movements — the second is refused as already posted rather than
        quietly duplicating the arrival.
        """
        transfer = _dispatch(world, world["kitchen"], "40")
        receipt = _draft_receipt(world, transfer, "40", "GRN-1")

        results = _race(
            lambda: _post_in_transaction(
                lambda: post_transfer_receipt(actor=world["actor"], receipt=receipt)
            ),
            lambda: _post_in_transaction(
                lambda: post_transfer_receipt(actor=world["actor"], receipt=receipt)
            ),
        )
        succeeded = [result for result in results if not isinstance(result, Exception)]
        assert len(succeeded) == 1, results
        receipt.refresh_from_db()
        assert receipt.status == InventoryDocumentStatus.POSTED
        assert _balance(world["kitchen"], world["rice"]).quantity == Decimal("40.000")
        assert (
            StockTransferReceipt.objects.filter(
                transfer=transfer, status=InventoryDocumentStatus.POSTED
            ).count()
            == 1
        )

    def test_a_cross_branch_receipt_commits_both_journals_or_neither(
        self, world: dict[str, Any]
    ) -> None:
        """
        Two branch-local journals in one transaction. Closing the source
        branch's mapping between dispatch and receipt makes the source half
        impossible; neither journal, and no stock effect, may survive.
        """
        from apps.accounting.services import archive_account_mapping

        transfer = _dispatch(world, world["far"], "40")
        before = JournalEntry.objects.count()
        archive_account_mapping(
            mapping=world["organization"].account_mappings.get(
                account_role__code=INTER_BRANCH_CLEARING
            ),
            reason="recorded in error",
        )
        receipt = _draft_receipt(world, transfer, "40", "GRN-1")

        with pytest.raises(ValidationError) as caught:
            post_transfer_receipt(actor=world["actor"], receipt=receipt)
        assert caught.value.code == "account_role_unmapped"

        assert JournalEntry.objects.count() == before
        assert _maybe_balance(world["far"], world["rice"]) is None
        assert _resolved(transfer) == ZERO
        receipt.refresh_from_db()
        assert receipt.status == InventoryDocumentStatus.DRAFT
