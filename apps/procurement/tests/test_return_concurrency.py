"""
Supplier returns under a real COMMIT boundary.

Its own file, for the reason `test_location_concurrency.py` records: a
transactional test needs a genuine COMMIT and truncates afterwards, and neither
is possible inside the outer atomic block a shared module seed holds open.

Four races, each exercising a different lock in the order
`apps/procurement/returns.py` documents:

    two posts of one return           the return row (step 1)
    two drafts, one line remainder    the receipt line (add_return_line)
    a new return vs receipt reversal  the receipt row (step 2)
    a return vs an issue of the same  the stock keys (step 5, in the kernel)

The quantity bound has a property worth stating: a **draft** return already
consumes availability (`returned_quantity_for` excludes only REVERSED), and
`add_return_line` reads the bound under a lock on the receipt line. The sum of
standing returns therefore cannot exceed the accepted quantity through any
interleaving of adds and posts — which is why the interesting quantity race is
two *adds*, not two posts. The posting-time re-check
(`_require_still_within_the_accepted_quantity`) stays anyway, as depth against
a route nobody has thought of yet.
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
    SUPPLIER_RETURN_CLEARING,
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
    ItemType,
    MovementType,
    StockBalance,
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
    SupplierReturn,
    SupplierReturnStatus,
)
from apps.procurement.posting import post_goods_receipt, reverse_goods_receipt
from apps.procurement.returns import (
    add_return_line,
    create_supplier_return,
    post_supplier_return,
)
from apps.procurement.services import (
    add_receipt_line,
    create_goods_receipt,
    create_supplier,
    inspect_receipt_line,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

TEST_YEAR = 2026
JAN_1 = datetime.date(TEST_YEAR, 1, 1)
RECEIVED = datetime.date(TEST_YEAR, 2, 10)
RETURNED = datetime.date(TEST_YEAR, 2, 14)
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
            (SUPPLIER_RETURN_CLEARING, "8-01-04-001"),
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
        self.supplier = create_supplier(
            organization=self.organization, code="SUP-01", name_ar="مورد"
        )

        self.keeper = User.objects.create_user(username="race-keeper", password=PASSWORD)
        grant_branch_access(user=self.keeper, branch=self.branch, role=Role.STOREKEEPER)
        self.approver = User.objects.create_user(username="race-approver", password=PASSWORD)
        grant_branch_access(user=self.approver, branch=self.branch, role=Role.ACCOUNTING_MANAGER)

    def receipt(self, *, reference: str, quantity: str = "50.000") -> GoodsReceipt:
        receipt = create_goods_receipt(
            supplier=self.supplier,
            branch=self.branch,
            warehouse=self.warehouse,
            created_by=self.keeper,
            received_at=RECEIVED,
            delivery_reference=reference,
            evidence_reference="إشعار",
        )
        line = add_receipt_line(
            receipt=receipt,
            item=self.rice,
            delivered_quantity=Decimal(quantity),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal(quantity), actor=self.keeper)
        with audit_context(actor=self.keeper):
            post_goods_receipt(receipt=receipt, actor=self.keeper)
        return GoodsReceipt.objects.get(pk=receipt.pk)

    def draft_return(self, *, receipt: GoodsReceipt, quantity: str) -> SupplierReturn:
        with audit_context(actor=self.keeper):
            supplier_return = create_supplier_return(
                receipt=receipt,
                created_by=self.keeper,
                returned_at=RETURNED,
                reason="بضاعة تالفة",
                evidence_reference="وصل السائق",
            )
            add_return_line(
                supplier_return=supplier_return,
                receipt_line=receipt.lines.get(),
                returned_base_quantity=Decimal(quantity),
            )
        return supplier_return


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
class TestReturnConcurrency:
    def test_two_simultaneous_posts_of_one_return(self, settings: object) -> None:
        """
        The return row is locked first, so the loser waits and then reads
        POSTED — the state the winner committed. Exactly one RETURN_OUT
        movement and one return journal exist afterwards.
        """
        scene = _Scene()
        receipt = scene.receipt(reference="DN-RACE")
        supplier_return = scene.draft_return(receipt=receipt, quantity="10.000")

        def post() -> None:
            with audit_context(actor=scene.keeper):
                post_supplier_return(
                    supplier_return=SupplierReturn.objects.get(pk=supplier_return.pk),
                    actor=scene.keeper,
                )

        results = _run(post, post)
        assert sorted(results) == ["already_posted", "ok"], results
        assert StockMovement.objects.filter(movement_type=MovementType.RETURN_OUT).count() == 1
        assert (
            JournalEntry.objects.filter(source_document_type="PROCUREMENT_SUPPLIER_RETURN").count()
            == 1
        )
        supplier_return.refresh_from_db()
        assert supplier_return.status == SupplierReturnStatus.POSTED

    def test_two_drafts_racing_for_one_line_remainder(self, settings: object) -> None:
        """
        Fifty kilograms accepted; two returns each want thirty. The receipt
        line is locked before availability is read, so the loser waits, counts
        the winner's committed thirty, and is refused — rather than both
        reading fifty available and together sending back sixty.
        """
        scene = _Scene()
        receipt = scene.receipt(reference="DN-BOUND", quantity="50.000")
        receipt_line = receipt.lines.get()

        def open_and_take() -> None:
            with audit_context(actor=scene.keeper):
                supplier_return = create_supplier_return(
                    receipt=GoodsReceipt.objects.get(pk=receipt.pk),
                    created_by=scene.keeper,
                    returned_at=RETURNED,
                    reason="بضاعة تالفة",
                    evidence_reference="وصل",
                )
                add_return_line(
                    supplier_return=supplier_return,
                    receipt_line=receipt_line,
                    returned_base_quantity=Decimal("30.000"),
                )

        results = _run(open_and_take, open_and_take)
        assert sorted(results) == ["ok", "return_over_quantity"], results
        from apps.procurement.models import SupplierReturnLine

        total = sum(
            (
                line.returned_base_quantity
                for line in SupplierReturnLine.objects.filter(goods_receipt_line=receipt_line)
            ),
            start=Decimal("0.000"),
        )
        assert total == Decimal("30.000")

    def test_a_new_return_racing_the_receipt_reversal(self, settings: object) -> None:
        """
        Both lock the receipt row, so the outcomes serialize. Either the
        return opens first and the reversal is refused because a standing
        return depends on the delivery, or the reversal wins and the return is
        refused because the delivery is no longer posted. Never both.
        """
        scene = _Scene()
        receipt = scene.receipt(reference="DN-REV")

        def open_return() -> None:
            with audit_context(actor=scene.keeper):
                supplier_return = create_supplier_return(
                    receipt=GoodsReceipt.objects.get(pk=receipt.pk),
                    created_by=scene.keeper,
                    returned_at=RETURNED,
                    reason="تلف",
                    evidence_reference="وصل",
                )
                add_return_line(
                    supplier_return=supplier_return,
                    receipt_line=receipt.lines.get(),
                    returned_base_quantity=Decimal("10.000"),
                )

        def reverse_delivery() -> None:
            with audit_context(actor=scene.approver):
                reverse_goods_receipt(
                    receipt=GoodsReceipt.objects.get(pk=receipt.pk),
                    actor=scene.approver,
                    reason="أُعيدت الشحنة",
                )

        results = _run(open_return, reverse_delivery)
        assert results.count("ok") == 1, results

        receipt.refresh_from_db()
        if receipt.status == GoodsReceiptStatus.POSTED:
            # The return won; the delivery still stands and carries a draft.
            assert SupplierReturn.objects.filter(receipt=receipt).exists()
        else:
            # The reversal won; no return exists against an unmade delivery.
            assert receipt.status == GoodsReceiptStatus.REVERSED
            assert not SupplierReturn.objects.filter(receipt=receipt).exists()

    def test_a_return_racing_an_issue_of_the_same_stock(self, settings: object) -> None:
        """
        Fifty on hand; a return of thirty and an issue of thirty both want
        them. The kernel's stock-key locks serialize the two, and whichever
        loses is refused on availability rather than posting negative stock.
        """
        from apps.inventory.ledger import MovementInput, post_stock_entry

        scene = _Scene()
        receipt = scene.receipt(reference="DN-STOCK", quantity="50.000")
        supplier_return = scene.draft_return(receipt=receipt, quantity="30.000")

        def post_return() -> None:
            with audit_context(actor=scene.keeper):
                post_supplier_return(
                    supplier_return=SupplierReturn.objects.get(pk=supplier_return.pk),
                    actor=scene.keeper,
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
                            quantity=Decimal("30.000"),
                            effect_key="race-issue",
                        )
                    ],
                    idempotency_key="race-issue-1",
                    business_date=RETURNED,
                )

        results = _run(post_return, issue)
        assert results.count("ok") == 1, results
        balance = StockBalance.objects.get(warehouse=scene.warehouse, item=scene.rice)
        assert balance.quantity == Decimal("20.000")
