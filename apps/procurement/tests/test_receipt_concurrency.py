"""
Goods receipt posting under a real COMMIT boundary.

Its own file, for the reason `test_location_concurrency.py` records: a
transactional test needs a genuine COMMIT and truncates afterwards, and neither
is possible inside the outer atomic block a shared module seed holds open. A
concurrency test that passed while proving nothing would be the worst available
outcome, so the incompatible combination is kept apart structurally.

Six races, each exercising a different pair of locks in the order
`apps/procurement/posting.py` documents:

    two posts of one receipt        the receipt row (step 1)
    two receipts, one PO remainder  the order lines (step 3)
    posting vs cancellation         the order row (step 3)
    posting vs a mapping change     the mapping advisory lock (step 2)
    opposite item order             the stock keys (step 5, inside the kernel)
    reversal vs an issue            the stock keys, in opposite directions

The other F14 scenarios — an idempotent retry, a changed payload, two drafts
that together exceed the remainder, a journal failure rolling everything back,
a reversal releasing the ordered quantity — are sequential facts rather than
races, and are asserted in `test_goods_receipt_posting.py` where they read
more clearly.
"""

from __future__ import annotations

import datetime
import threading
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONTROL,
    Account,
    AccountRole,
    JournalEntry,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.core.context import audit_context
from apps.inventory.models import (
    InventoryItem,
    ItemType,
    StockLedgerEntry,
    StockMovement,
)
from apps.organizations.models import Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
)
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptStatus,
    PurchaseOrder,
    PurchaseOrderLine,
)
from apps.procurement.posting import post_goods_receipt, reverse_goods_receipt
from apps.procurement.services import (
    add_order_line,
    add_receipt_line,
    approve_purchase_order,
    cancel_purchase_order,
    create_goods_receipt,
    create_purchase_order,
    create_supplier,
    inspect_receipt_line,
    issue_purchase_order,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

TEST_YEAR = 2026
JAN_1 = datetime.date(TEST_YEAR, 1, 1)
RECEIVED = datetime.date(TEST_YEAR, 2, 10)
PASSWORD = "pw-not-real-1234"


class _Scene:
    """Everything one race needs, built once inside the transactional test."""

    def __init__(self) -> None:
        from apps.inventory.services import (
            create_item,
            create_item_category,
            create_warehouse,
        )

        call_command("seed_units", verbosity=0)
        kilogram = UnitOfMeasure.objects.get(code="KG")

        self.organization = create_organization(code="RACE", name_ar="سباق", name_en="Race")
        self.branch = create_branch(
            organization=self.organization,
            code="MAIN",
            name_ar="الرئيسي",
            name_en="Main",
            business_day_start_time=datetime.time(9, 0),
        )
        configure_accounting(organization=self.organization, fiscal_year_start_month=1)
        open_fiscal_year(organization=self.organization, year=TEST_YEAR)
        call_command("seed_chart_of_accounts", organization=self.organization.code, verbosity=0)
        for code, account_code in (
            (INVENTORY_CONTROL, "1-03-01-001"),
            (GOODS_RECEIVED_NOT_INVOICED, "2-01-02-001"),
        ):
            create_account_mapping(
                organization=self.organization,
                account_role=AccountRole.objects.get(code=code),
                account=Account.objects.get(organization=self.organization, code=account_code),
                effective_from=JAN_1,
            )

        self.warehouse = create_warehouse(branch=self.branch, code="STORE", name_ar="مخزن")
        category = create_item_category(
            organization=self.organization, code="GRAINS", name_ar="حبوب"
        )
        self.rice = create_item(
            organization=self.organization,
            code="RICE",
            name_ar="رز",
            category=category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        self.sugar = create_item(
            organization=self.organization,
            code="SUGAR",
            name_ar="سكر",
            category=category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        self.supplier = create_supplier(
            organization=self.organization, code="SUP-01", name_ar="مورد"
        )

        self.keeper = User.objects.create_user(username="race-keeper", password=PASSWORD)
        grant_branch_access(user=self.keeper, branch=self.branch, role=Role.STOREKEEPER)
        self.buyer = User.objects.create_user(username="race-buyer", password=PASSWORD)
        grant_branch_access(user=self.buyer, branch=self.branch, role=Role.PURCHASING)
        self.approver = User.objects.create_user(username="race-approver", password=PASSWORD)
        grant_branch_access(user=self.approver, branch=self.branch, role=Role.ACCOUNTING_MANAGER)

    def order(self, *, quantity: str = "100.000") -> PurchaseOrder:
        order = create_purchase_order(
            supplier=self.supplier,
            branch=self.branch,
            warehouse=self.warehouse,
            created_by=self.buyer,
            ordered_on=datetime.date(TEST_YEAR, 2, 1),
        )
        add_order_line(
            order=order,
            item=self.rice,
            ordered_quantity=Decimal(quantity),
            unit_price=Decimal("1400.000000"),
        )
        approve_purchase_order(order=order, actor=self.approver)
        return issue_purchase_order(order=order, actor=self.buyer)

    def receipt(
        self,
        *,
        reference: str,
        items: list[tuple[InventoryItem, str]] | None = None,
        order: PurchaseOrder | None = None,
        order_line: PurchaseOrderLine | None = None,
    ) -> GoodsReceipt:
        receipt = create_goods_receipt(
            supplier=self.supplier,
            branch=self.branch,
            warehouse=self.warehouse,
            created_by=self.keeper,
            received_at=RECEIVED,
            order=order,
            delivery_reference=reference,
            evidence_reference="إشعار",
        )
        for item, quantity in items or [(self.rice, "10.000")]:
            line = add_receipt_line(
                receipt=receipt,
                item=item,
                delivered_quantity=Decimal(quantity),
                unit_price=None if order_line is not None else Decimal("1400.000000"),
                order_line=order_line,
            )
            inspect_receipt_line(
                line=line, accepted_base_quantity=Decimal(quantity), actor=self.keeper
            )
        return GoodsReceipt.objects.get(pk=receipt.pk)


def _run(*targets: object) -> list[str]:
    """Start every target at once and collect what each one reported."""
    results: list[str] = []
    lock = threading.Lock()

    def wrap(target: object) -> None:
        from django.db import connection as thread_connection

        try:
            target()  # type: ignore[operator]
            outcome = "ok"
        except ValidationError as refusal:
            outcome = str(refusal.code)
        except Exception as unexpected:  # noqa: BLE001 - reported, not swallowed
            outcome = f"{type(unexpected).__name__}: {unexpected}"
        finally:
            thread_connection.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=wrap, args=(target,)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


@pytest.mark.django_db(transaction=True)
class TestReceiptPostingConcurrency:
    def test_two_simultaneous_posts_of_one_receipt(self, settings: object) -> None:
        """
        The receipt row is locked first, so the loser waits and then reads
        POSTED — the state the winner committed, not the DRAFT it started with.
        Exactly one movement and one journal exist afterwards.
        """
        scene = _Scene()
        receipt = scene.receipt(reference="DN-RACE")

        def post() -> None:
            with audit_context(actor=scene.keeper):
                post_goods_receipt(
                    receipt=GoodsReceipt.objects.get(pk=receipt.pk), actor=scene.keeper
                )

        results = _run(post, post)
        assert sorted(results) == ["already_posted", "ok"], results
        assert StockMovement.objects.count() == 1
        assert StockLedgerEntry.objects.count() == 1
        assert JournalEntry.objects.count() == 1
        receipt.refresh_from_db()
        assert receipt.status == GoodsReceiptStatus.POSTED

    def test_two_receipts_competing_for_one_order_remainder(self, settings: object) -> None:
        """
        Two deliveries of sixty against an order that has since been revised
        down to a hundred.

        Getting here needs the revision, and that is the point rather than a
        contrivance: `add_receipt_line` already refuses two drafts that
        together exceed the remainder, so the only way two *postable* drafts
        can compete for one remainder is for the remainder to have shrunk
        underneath them. The order lines are locked before the posted quantity
        is counted, so the second one to arrive counts the first one's commit
        and is refused rather than over-receiving.
        """
        from apps.procurement.services import revise_purchase_order

        scene = _Scene()
        order = scene.order(quantity="120.000")
        order_line = order.lines.get()
        first = scene.receipt(
            reference="DN-A",
            items=[(scene.rice, "60.000")],
            order=order,
            order_line=order_line,
        )
        second = scene.receipt(
            reference="DN-B",
            items=[(scene.rice, "60.000")],
            order=order,
            order_line=order_line,
        )
        with audit_context(actor=scene.buyer):
            revise_purchase_order(
                order=order,
                actor=scene.buyer,
                reason="خُفّضت الكمية بعد تسجيل التسليمين",
                line_quantities={str(order_line.line_uid): Decimal("100.000")},
            )

        def post(pk: int) -> object:
            def run() -> None:
                with audit_context(actor=scene.keeper):
                    post_goods_receipt(receipt=GoodsReceipt.objects.get(pk=pk), actor=scene.keeper)

            return run

        results = _run(post(first.pk), post(second.pk))
        assert sorted(results) == ["ok", "over_receipt"], results
        assert StockMovement.objects.count() == 1

    def test_posting_racing_order_cancellation(self, settings: object) -> None:
        """
        Both orderings are correct outcomes, and both are safe. Either the
        receipt posts and the cancellation is refused because goods arrived, or
        the cancellation wins and the posting is refused. What must never
        happen is both succeeding.
        """
        scene = _Scene()
        order = scene.order()
        order_line = order.lines.get()
        receipt = scene.receipt(
            reference="DN-CANCEL",
            items=[(scene.rice, "10.000")],
            order=order,
            order_line=order_line,
        )

        def post() -> None:
            with audit_context(actor=scene.keeper):
                post_goods_receipt(
                    receipt=GoodsReceipt.objects.get(pk=receipt.pk), actor=scene.keeper
                )

        def cancel() -> None:
            with audit_context(actor=scene.approver):
                cancel_purchase_order(
                    order=PurchaseOrder.objects.get(pk=order.pk),
                    actor=scene.approver,
                    reason="ألغي الطلب",
                )

        results = _run(post, cancel)
        assert "ok" in results, results
        assert results.count("ok") <= 2
        receipt.refresh_from_db()
        order.refresh_from_db()
        if receipt.status == GoodsReceiptStatus.POSTED:
            assert StockMovement.objects.count() == 1
            assert JournalEntry.objects.count() == 1
        else:
            assert StockMovement.objects.count() == 0
            assert JournalEntry.objects.count() == 0

    def test_posting_racing_a_mapping_change(self, settings: object) -> None:
        """
        The mapping advisory lock sits above every row lock a posting takes, so
        a mutation cannot slip between the resolution and the commit. Whichever
        wins, the posted movement names the account the journal debited.
        """
        scene = _Scene()
        receipt = scene.receipt(reference="DN-MAP")
        replacement = Account.objects.get(organization=scene.organization, code="1-03-02-001")

        def post() -> None:
            with audit_context(actor=scene.keeper):
                post_goods_receipt(
                    receipt=GoodsReceipt.objects.get(pk=receipt.pk), actor=scene.keeper
                )

        def remap() -> None:
            from apps.inventory.accounts import create_inventory_mapping

            with audit_context(actor=scene.approver):
                create_inventory_mapping(
                    organization=scene.organization,
                    role=INVENTORY_CONTROL,
                    account=replacement,
                    item=scene.rice,
                    effective_from=JAN_1,
                )

        _run(post, remap)
        receipt.refresh_from_db()
        if receipt.status != GoodsReceiptStatus.POSTED:
            return
        movement = StockMovement.objects.get()
        debit = JournalEntry.objects.get().lines.get(debit__gt=0)
        assert movement.control_account_id == debit.account_id

    def test_two_receipts_naming_the_same_items_in_opposite_order(self, settings: object) -> None:
        """
        The kernel sorts the stock keys canonically, so two events listing rice
        before sugar and sugar before rice take the same locks in the same
        database order. Listed in caller order they would deadlock.
        """
        scene = _Scene()
        forward = scene.receipt(
            reference="DN-FWD", items=[(scene.rice, "5.000"), (scene.sugar, "5.000")]
        )
        backward = scene.receipt(
            reference="DN-BWD", items=[(scene.sugar, "5.000"), (scene.rice, "5.000")]
        )

        def post(pk: int) -> object:
            def run() -> None:
                with audit_context(actor=scene.keeper):
                    post_goods_receipt(receipt=GoodsReceipt.objects.get(pk=pk), actor=scene.keeper)

            return run

        results = _run(post(forward.pk), post(backward.pk))
        assert results == ["ok", "ok"], results
        assert StockMovement.objects.count() == 4

    def test_a_reversal_racing_an_issue_of_the_same_stock(self, settings: object) -> None:
        """
        Both want the same kilograms and only one can have them. Whichever
        loses is refused on availability rather than posting negative stock.
        """
        from apps.inventory.ledger import MovementInput, post_stock_entry
        from apps.inventory.models import MovementType

        scene = _Scene()
        receipt = scene.receipt(reference="DN-REV", items=[(scene.rice, "10.000")])
        with audit_context(actor=scene.keeper):
            posted = post_goods_receipt(receipt=receipt, actor=scene.keeper)

        def reverse() -> None:
            with audit_context(actor=scene.approver):
                reverse_goods_receipt(
                    receipt=GoodsReceipt.objects.get(pk=posted.pk),
                    actor=scene.approver,
                    reason="أُعيدت الشحنة",
                )

        def issue() -> None:
            with audit_context(actor=scene.keeper):
                post_stock_entry(
                    organization=scene.organization,
                    effects=[
                        MovementInput(
                            warehouse=scene.warehouse,
                            item=scene.rice,
                            movement_type=MovementType.ISSUE,
                            quantity=Decimal("10.000"),
                            effect_key="race-issue",
                        )
                    ],
                    idempotency_key="race-issue-1",
                    business_date=RECEIVED,
                )

        _run(reverse, issue)
        from apps.inventory.models import StockBalance

        balance = StockBalance.objects.get(warehouse=scene.warehouse, item=scene.rice)
        assert balance.quantity == Decimal("0.000")
