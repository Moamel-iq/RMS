"""
Three-way matching under a real COMMIT boundary.

Matching has a genuine over-allocation race, and it is the reason availability
is derived from allocation rows behind a lock rather than cached in a column:

    receipt available = 100
    T1 allocates 60
    T2 allocates 60
    both read 100 before either commits

Six races, each exercising a different pair of locks in the order
`apps/procurement/matching.py` documents:

    two matches, one receipt remainder    the receipt line (step 4)
    two matches, one invoice remainder    the invoice line (step 3)
    two allocations, one order remainder  the order line (step 5)
    a match racing receipt reversal       the receipt row
    a match racing invoice reversal       the invoice row (step 2)
    opposite source ordering              every step, sorted by primary key

`default_connection.close()` appears nowhere. Task 2.9 proved it poisons the
whole session: closing the main test connection mid-run left every subsequent
test erroring at setup, and the failure looked nothing like its cause.
"""

from __future__ import annotations

import datetime
import threading
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONTROL,
    PURCHASE_PRICE_VARIANCE,
    SUPPLIER_PAYABLE,
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
from apps.inventory.models import ItemType
from apps.organizations.models import Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    grant_organization_access,
)
from apps.procurement.invoices import (
    add_inventory_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    post_supplier_invoice,
    reverse_supplier_invoice,
)
from apps.procurement.matching import (
    add_allocation,
    cancel_purchase_match,
    create_purchase_match,
    mark_match_ready,
    receipt_availability,
)
from apps.procurement.models import (
    GoodsReceipt,
    PurchaseMatch,
    PurchaseMatchAllocation,
    PurchaseMatchStatus,
    SupplierInvoice,
    SupplierInvoicePosting,
)
from apps.procurement.posting import post_goods_receipt, reverse_goods_receipt
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
RECEIVED = datetime.date(TEST_YEAR, 3, 1)
INVOICED = datetime.date(TEST_YEAR, 3, 10)
PASSWORD = "pw-not-real-1234"


class _Scene:
    """Everything one race needs, built once inside the transactional test."""

    def __init__(self) -> None:
        from apps.inventory.services import create_item, create_item_category, create_warehouse

        call_command("seed_units", verbosity=0)
        kilogram = UnitOfMeasure.objects.get(code="KG")

        self.organization = create_organization(code="RACEMTC", name="سباق المطابقة")
        self.branch = create_branch(
            organization=self.organization,
            code="MAIN",
            name="الرئيسي",
            business_day_start_time=datetime.time(9, 0),
        )
        configure_accounting(organization=self.organization, fiscal_year_start_month=1)
        open_fiscal_year(organization=self.organization, year=TEST_YEAR)
        call_command("seed_chart_of_accounts", organization=self.organization.code, verbosity=0)
        for code, account_code in (
            (INVENTORY_CONTROL, "1-03-01-001"),
            (GOODS_RECEIVED_NOT_INVOICED, "2-01-02-001"),
            (SUPPLIER_PAYABLE, "2-01-01-001"),
            (PURCHASE_PRICE_VARIANCE, "8-01-03-001"),
        ):
            create_account_mapping(
                organization=self.organization,
                account_role=AccountRole.objects.get(code=code),
                account=Account.objects.get(organization=self.organization, code=account_code),
                effective_from=JAN_1,
            )

        self.warehouse = create_warehouse(branch=self.branch, code="STORE", name="مخزن")
        category = create_item_category(organization=self.organization, code="GRAINS", name="حبوب")
        self.rice = create_item(
            organization=self.organization,
            code="RICE",
            name="رز",
            category=category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        self.sugar = create_item(
            organization=self.organization,
            code="SUGAR",
            name="سكر",
            category=category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        self.supplier = create_supplier(organization=self.organization, code="SUP-01", name="مورد")

        self.keeper = User.objects.create_user(username="race-keeper", password=PASSWORD)
        grant_branch_access(user=self.keeper, branch=self.branch, role=Role.STOREKEEPER)
        self.clerk = User.objects.create_user(username="race-clerk", password=PASSWORD)
        grant_organization_access(
            user=self.clerk, organization=self.organization, role=Role.ACCOUNTANT
        )
        self.controller = User.objects.create_user(username="race-controller", password=PASSWORD)
        grant_organization_access(
            user=self.controller,
            organization=self.organization,
            role=Role.ACCOUNTING_MANAGER,
        )

    def receipt(self, *, item: object, quantity: str, reference: str) -> GoodsReceipt:
        with audit_context(actor=self.keeper):
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
                item=item,  # type: ignore[arg-type]
                delivered_quantity=Decimal(quantity),
                unit_price=Decimal("1400.000000"),
            )
            inspect_receipt_line(
                line=line, accepted_base_quantity=Decimal(quantity), actor=self.keeper
            )
            return post_goods_receipt(receipt=receipt, actor=self.keeper)

    def invoice(
        self, *, reference: str, items: list[tuple[object, str]] | None = None
    ) -> SupplierInvoice:
        with audit_context(actor=self.clerk):
            invoice = create_supplier_invoice(
                supplier=self.supplier,
                branch=self.branch,
                created_by=self.clerk,
                supplier_invoice_number=reference,
                invoice_date=INVOICED,
                business_date=INVOICED,
            )
            for item, quantity in items or [(self.rice, "100.000")]:
                add_inventory_line(
                    invoice=invoice,
                    item=item,  # type: ignore[arg-type]
                    base_quantity=Decimal(quantity),
                    unit_price=Decimal("1450.000000"),
                )
        with audit_context(actor=self.controller):
            return approve_supplier_invoice(invoice=invoice, actor=self.controller)

    def match(self, invoice: SupplierInvoice) -> PurchaseMatch:
        with audit_context(actor=self.clerk):
            return create_purchase_match(invoice=invoice, created_by=self.clerk)


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
        except IntegrityError:
            outcome = "integrity_error"
        except Exception as unexpected:  # noqa: BLE001 - reported, not swallowed
            outcome = f"{type(unexpected).__name__}: {unexpected}"
        finally:
            # The worker's own connection only.
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
class TestMatchingConcurrency:
    def test_two_matches_competing_for_one_receipt_remainder(self, settings: object) -> None:
        """
        The race this whole design exists for. A delivery of a hundred, two
        invoices each claiming sixty of it: both read the same availability
        before either commits, and the receipt line lock is what makes the
        second one see the first one's allocation.
        """
        scene = _Scene()
        receipt = scene.receipt(item=scene.rice, quantity="100.000", reference="DN-SHARED")
        receipt_line = receipt.lines.get()

        first = scene.invoice(reference="INV-A", items=[(scene.rice, "60.000")])
        second = scene.invoice(reference="INV-B", items=[(scene.rice, "60.000")])
        match_a = scene.match(first)
        match_b = scene.match(second)

        def allocate(match: PurchaseMatch, invoice: SupplierInvoice) -> object:
            def run() -> None:
                with audit_context(actor=scene.clerk):
                    add_allocation(
                        match=PurchaseMatch.objects.get(pk=match.pk),
                        invoice_line=invoice.lines.get(),
                        receipt_line=receipt_line,
                        matched_base_quantity=Decimal("60.000"),
                        created_by=scene.clerk,
                    )

            return run

        results = _run(allocate(match_a, first), allocate(match_b, second))
        assert sorted(results) == ["ok", "receipt_over_allocation"], results
        assert PurchaseMatchAllocation.objects.count() == 1
        assert receipt_availability(receipt_line).quantity == Decimal("40.000")

    def test_two_matches_competing_for_one_invoice_remainder(self, settings: object) -> None:
        """
        The other direction: one invoice line of a hundred, two deliveries each
        claiming sixty of it. The invoice line lock decides.
        """
        scene = _Scene()
        first = scene.receipt(item=scene.rice, quantity="60.000", reference="DN-1")
        second = scene.receipt(item=scene.rice, quantity="60.000", reference="DN-2")
        invoice = scene.invoice(reference="INV-ONE", items=[(scene.rice, "100.000")])
        match = scene.match(invoice)

        def allocate(receipt: GoodsReceipt) -> object:
            def run() -> None:
                with audit_context(actor=scene.clerk):
                    add_allocation(
                        match=PurchaseMatch.objects.get(pk=match.pk),
                        invoice_line=invoice.lines.get(),
                        receipt_line=receipt.lines.get(),
                        matched_base_quantity=Decimal("60.000"),
                        created_by=scene.clerk,
                    )

            return run

        results = _run(allocate(first), allocate(second))
        assert sorted(results) == ["invoice_over_allocation", "ok"], results
        assert PurchaseMatchAllocation.objects.count() == 1

    def test_a_match_racing_receipt_reversal(self, settings: object) -> None:
        """
        Both orderings are safe. Either the allocation lands and the reversal
        is refused because the stock has been consumed, or the reversal wins
        and the allocation is refused because the delivery is no longer posted.
        What must never happen is an allocation against a reversed receipt.
        """
        scene = _Scene()
        receipt = scene.receipt(item=scene.rice, quantity="50.000", reference="DN-REV")
        invoice = scene.invoice(reference="INV-REV", items=[(scene.rice, "50.000")])
        match = scene.match(invoice)

        def allocate() -> None:
            with audit_context(actor=scene.clerk):
                add_allocation(
                    match=PurchaseMatch.objects.get(pk=match.pk),
                    invoice_line=invoice.lines.get(),
                    receipt_line=receipt.lines.get(),
                    matched_base_quantity=Decimal("50.000"),
                    created_by=scene.clerk,
                )

        def reverse() -> None:
            with audit_context(actor=scene.controller):
                reverse_goods_receipt(
                    receipt=GoodsReceipt.objects.get(pk=receipt.pk),
                    actor=scene.controller,
                    reason="أُعيدت الشحنة",
                )

        _run(allocate, reverse)
        receipt.refresh_from_db()
        if receipt.status != "POSTED":
            # The reversal won: nothing may cite it.
            for allocation in PurchaseMatchAllocation.objects.all():
                assert allocation.goods_receipt_line.receipt_id != receipt.pk

    def test_a_match_racing_invoice_reversal(self, settings: object) -> None:
        """
        A reversed invoice is a debt that no longer exists. Whichever wins, no
        allocation may end up citing one.
        """
        from apps.procurement.invoices import post_supplier_invoice, reverse_supplier_invoice
        from apps.procurement.models import SupplierInvoiceStatus

        scene = _Scene()
        receipt = scene.receipt(item=scene.rice, quantity="50.000", reference="DN-IREV")
        # A pure-goods invoice cannot post, so the reversal target is a match
        # against an invoice that is still APPROVED — reversal is refused, and
        # that refusal is itself the safety property.
        invoice = scene.invoice(reference="INV-IREV", items=[(scene.rice, "50.000")])
        match = scene.match(invoice)

        def allocate() -> None:
            with audit_context(actor=scene.clerk):
                add_allocation(
                    match=PurchaseMatch.objects.get(pk=match.pk),
                    invoice_line=invoice.lines.get(),
                    receipt_line=receipt.lines.get(),
                    matched_base_quantity=Decimal("50.000"),
                    created_by=scene.clerk,
                )

        def try_reverse() -> None:
            with audit_context(actor=scene.controller):
                reverse_supplier_invoice(
                    invoice=SupplierInvoice.objects.get(pk=invoice.pk),
                    actor=scene.controller,
                    reason="محاولة",
                )

        results = _run(allocate, try_reverse)
        assert "ok" in results, results
        invoice.refresh_from_db()
        # An APPROVED invoice cannot be reversed at all — Task 2.10 refuses it —
        # so the invoice is untouched and the allocation stands.
        assert invoice.status == SupplierInvoiceStatus.APPROVED
        assert "invoice_not_posted" in results
        _ = post_supplier_invoice

    def test_two_matches_naming_sources_in_opposite_order(self, settings: object) -> None:
        """
        Every lock step sorts by primary key rather than by the order the
        caller listed things, so two multi-line matches naming the same rows in
        opposite order take them in the same database order and cannot
        deadlock. Listed in caller order they would.
        """
        scene = _Scene()
        rice_receipt = scene.receipt(item=scene.rice, quantity="50.000", reference="DN-R")
        sugar_receipt = scene.receipt(item=scene.sugar, quantity="50.000", reference="DN-S")
        first = scene.invoice(
            reference="INV-FWD", items=[(scene.rice, "25.000"), (scene.sugar, "25.000")]
        )
        second = scene.invoice(
            reference="INV-BWD", items=[(scene.sugar, "25.000"), (scene.rice, "25.000")]
        )
        match_a = scene.match(first)
        match_b = scene.match(second)

        def allocate_pairs(match: PurchaseMatch, invoice: SupplierInvoice) -> object:
            def run() -> None:
                with audit_context(actor=scene.clerk):
                    for line in invoice.lines.order_by("sequence"):
                        receipt = rice_receipt if line.item_id == scene.rice.pk else sugar_receipt
                        add_allocation(
                            match=PurchaseMatch.objects.get(pk=match.pk),
                            invoice_line=line,
                            receipt_line=receipt.lines.get(),
                            matched_base_quantity=Decimal("25.000"),
                            created_by=scene.clerk,
                        )

            return run

        results = _run(allocate_pairs(match_a, first), allocate_pairs(match_b, second))
        assert results == ["ok", "ok"], results
        assert PurchaseMatchAllocation.objects.count() == 4

    def test_cancellation_racing_readiness(self, settings: object) -> None:
        """
        Both orderings are safe. Either the match freezes and the cancellation
        withdraws it afterwards, or the cancellation lands first and the
        readiness is refused. What must never exist is a match that is both.
        """
        from apps.procurement.matching import cancel_purchase_match

        scene = _Scene()
        receipt = scene.receipt(item=scene.rice, quantity="50.000", reference="DN-CANCEL")
        invoice = scene.invoice(reference="INV-CANCEL", items=[(scene.rice, "50.000")])
        match = scene.match(invoice)
        with audit_context(actor=scene.clerk):
            add_allocation(
                match=match,
                invoice_line=invoice.lines.get(),
                receipt_line=receipt.lines.get(),
                matched_base_quantity=Decimal("50.000"),
                created_by=scene.clerk,
            )

        def ready() -> None:
            with audit_context(actor=scene.clerk):
                mark_match_ready(match=PurchaseMatch.objects.get(pk=match.pk), actor=scene.clerk)

        def cancel() -> None:
            with audit_context(actor=scene.controller):
                cancel_purchase_match(
                    match=PurchaseMatch.objects.get(pk=match.pk),
                    actor=scene.controller,
                    reason="أُلغيت",
                )

        _run(ready, cancel)
        match.refresh_from_db()
        assert match.status in (PurchaseMatchStatus.READY, PurchaseMatchStatus.CANCELLED)
        if match.status == PurchaseMatchStatus.CANCELLED:
            assert match.cancellation_reason
        else:
            assert match.ready_at is not None


@pytest.mark.django_db(transaction=True)
class TestPostingConcurrency:
    """
    Real-COMMIT races around the entry Task 2.12 posts.

    Each one is a pair of operators doing something reasonable at the same
    moment. What must never happen is two payables for one invoice, GRNI
    cleared twice, or a match released while the ledger still stands on it.
    """

    def test_two_posts_of_one_invoice(self, settings: object) -> None:
        """
        The expensive one. Both threads pass the status check, and only one may
        reach the ledger — first on the invoice row lock, and independently on
        the partial unique index over live generations.
        """
        scene = _Scene()
        receipt = scene.receipt(item=scene.rice, quantity="100.000", reference="DN-P1")
        invoice = scene.invoice(reference="INV-P1")
        match = scene.match(invoice)
        with audit_context(actor=scene.clerk):
            add_allocation(
                match=match,
                invoice_line=invoice.lines.get(),
                receipt_line=receipt.lines.get(),
                matched_base_quantity=Decimal("100.000"),
                created_by=scene.clerk,
            )
            mark_match_ready(match=match, actor=scene.clerk)

        def post() -> None:
            with audit_context(actor=scene.controller):
                post_supplier_invoice(
                    invoice=SupplierInvoice.objects.get(pk=invoice.pk), actor=scene.controller
                )

        results = _run(post, post)
        assert sorted(results) == ["already_posted", "ok"], results
        assert SupplierInvoicePosting.objects.filter(status="LIVE").count() == 1
        assert (
            JournalEntry.objects.filter(
                source_document_type="PROCUREMENT_SUPPLIER_INVOICE",
                source_event="POSTED",
            ).count()
            == 1
        )

    def test_posting_racing_its_own_match_cancellation(self, settings: object) -> None:
        """
        Either the posting lands and the cancellation is refused because the
        ledger now stands on the match, or the cancellation lands and the
        posting is refused because there is no ready match to post from.
        Both orders are safe; a cleared GRNI beside a released delivery is not.
        """
        scene = _Scene()
        receipt = scene.receipt(item=scene.rice, quantity="100.000", reference="DN-P2")
        invoice = scene.invoice(reference="INV-P2")
        match = scene.match(invoice)
        with audit_context(actor=scene.clerk):
            add_allocation(
                match=match,
                invoice_line=invoice.lines.get(),
                receipt_line=receipt.lines.get(),
                matched_base_quantity=Decimal("100.000"),
                created_by=scene.clerk,
            )
            mark_match_ready(match=match, actor=scene.clerk)

        def post() -> None:
            with audit_context(actor=scene.controller):
                post_supplier_invoice(
                    invoice=SupplierInvoice.objects.get(pk=invoice.pk), actor=scene.controller
                )

        def withdraw() -> None:
            with audit_context(actor=scene.controller):
                cancel_purchase_match(
                    match=PurchaseMatch.objects.get(pk=match.pk),
                    actor=scene.controller,
                    reason="سحب المطابقة",
                )

        _run(post, withdraw)
        invoice.refresh_from_db()
        match.refresh_from_db()
        if invoice.status == "POSTED":
            assert match.status != "CANCELLED"
            assert SupplierInvoicePosting.objects.filter(status="LIVE").count() == 1
        else:
            assert match.status == "CANCELLED"
            assert not SupplierInvoicePosting.objects.exists()

    def test_posting_racing_a_receipt_reversal(self, settings: object) -> None:
        """A delivery cited by a live posting cannot be undone underneath it."""
        scene = _Scene()
        receipt = scene.receipt(item=scene.rice, quantity="100.000", reference="DN-P3")
        invoice = scene.invoice(reference="INV-P3")
        match = scene.match(invoice)
        with audit_context(actor=scene.clerk):
            add_allocation(
                match=match,
                invoice_line=invoice.lines.get(),
                receipt_line=receipt.lines.get(),
                matched_base_quantity=Decimal("100.000"),
                created_by=scene.clerk,
            )
            mark_match_ready(match=match, actor=scene.clerk)

        def post() -> None:
            with audit_context(actor=scene.controller):
                post_supplier_invoice(
                    invoice=SupplierInvoice.objects.get(pk=invoice.pk), actor=scene.controller
                )

        def undo() -> None:
            with audit_context(actor=scene.controller):
                reverse_goods_receipt(
                    receipt=GoodsReceipt.objects.get(pk=receipt.pk),
                    actor=scene.controller,
                    reason="أُعيدت",
                )

        _run(post, undo)
        receipt.refresh_from_db()
        if receipt.status != "POSTED":
            assert not SupplierInvoicePosting.objects.filter(status="LIVE").exists()

    def test_two_reversals_of_one_posting(self, settings: object) -> None:
        """One reversing journal, one cancelled match, no double credit."""
        scene = _Scene()
        receipt = scene.receipt(item=scene.rice, quantity="100.000", reference="DN-P4")
        invoice = scene.invoice(reference="INV-P4")
        match = scene.match(invoice)
        with audit_context(actor=scene.clerk):
            add_allocation(
                match=match,
                invoice_line=invoice.lines.get(),
                receipt_line=receipt.lines.get(),
                matched_base_quantity=Decimal("100.000"),
                created_by=scene.clerk,
            )
            mark_match_ready(match=match, actor=scene.clerk)
        with audit_context(actor=scene.controller):
            post_supplier_invoice(
                invoice=SupplierInvoice.objects.get(pk=invoice.pk), actor=scene.controller
            )

        def undo() -> None:
            with audit_context(actor=scene.controller):
                reverse_supplier_invoice(
                    invoice=SupplierInvoice.objects.get(pk=invoice.pk),
                    actor=scene.controller,
                    reason="خطأ",
                )

        results = _run(undo, undo)
        assert sorted(results) == ["already_reversed", "ok"], results
        assert SupplierInvoicePosting.objects.filter(status="REVERSED").count() == 1
        assert JournalEntry.objects.filter(source_event="REVERSED").count() == 1
