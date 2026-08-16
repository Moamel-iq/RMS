"""
Supplier credit notes under a real COMMIT boundary.

Three races, each exercising a different lock in the order
`apps/procurement/credit_notes.py` documents:

    two posts of one note              the note row (step 1)
    two notes racing one return        the standing-note index + the return row
    two notes' allocations, one invoice remainder   the invoice row (step 4)
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
    PURCHASE_RETURN_VARIANCE,
    SUPPLIER_PAYABLE,
    SUPPLIER_RETURN_CLEARING,
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
from apps.core.context import audit_context
from apps.inventory.models import ItemType
from apps.organizations.models import Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
)
from apps.procurement.credit_notes import (
    add_credit_allocation,
    add_return_allocation,
    create_supplier_credit_note,
    credit_allocated_to,
    post_supplier_credit_note,
)
from apps.procurement.invoices import (
    add_account_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    post_supplier_invoice,
)
from apps.procurement.models import (
    SupplierCreditNote,
    SupplierCreditNoteStatus,
    SupplierInvoice,
    SupplierReturn,
)
from apps.procurement.posting import post_goods_receipt
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
CREDITED = datetime.date(TEST_YEAR, 2, 20)
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
            (SUPPLIER_PAYABLE, "2-01-01-001"),
            (SUPPLIER_RETURN_CLEARING, "8-01-04-001"),
            (PURCHASE_RETURN_VARIANCE, "7-09-04-001"),
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

    def posted_return(self, *, reference: str = "DN-RACE") -> SupplierReturn:
        """Fifty in at 1,400; ten back: 14,000 in the clearing account."""
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
            delivered_quantity=Decimal("50.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("50.000"), actor=self.keeper)
        with audit_context(actor=self.keeper):
            post_goods_receipt(receipt=receipt, actor=self.keeper)
            supplier_return = create_supplier_return(
                receipt=receipt,
                created_by=self.keeper,
                returned_at=RETURNED,
                evidence_reference="وصل",
            )
            add_return_line(
                supplier_return=supplier_return,
                receipt_line=receipt.lines.get(),
                returned_base_quantity=Decimal("10.000"),
            )
            return post_supplier_return(supplier_return=supplier_return, actor=self.keeper)

    def posted_invoice(self, *, amount: str, reference: str) -> SupplierInvoice:
        with audit_context(actor=self.keeper):
            invoice = create_supplier_invoice(
                supplier=self.supplier,
                branch=self.branch,
                created_by=self.keeper,
                supplier_invoice_number=reference,
                invoice_date=RECEIVED,
            )
            add_account_line(
                invoice=invoice,
                account=Account.objects.get(organization=self.organization, code="5-01-02-003"),
                cost_center=CostCenter.objects.filter(organization=self.organization).first(),
                description="أجور نقل",
                quantity=Decimal("1.000"),
                unit_price=Decimal(amount),
            )
            approve_supplier_invoice(invoice=invoice, actor=self.keeper)
            return post_supplier_invoice(invoice=invoice, actor=self.keeper)


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
class TestCreditNoteConcurrency:
    def test_two_simultaneous_posts_of_one_note(self, settings: object) -> None:
        """The note row is locked first; exactly one journal exists afterwards."""
        scene = _Scene()
        supplier_return = scene.posted_return()
        with audit_context(actor=scene.keeper):
            note = create_supplier_credit_note(
                supplier_return=supplier_return,
                created_by=scene.keeper,
                supplier_document_number="SCN-1",
                credit_date=CREDITED,
                amount=Decimal("14000.000"),
            )
            add_return_allocation(
                credit_note=note,
                return_line=supplier_return.lines.get(),
                credited_base_quantity=Decimal("10.000"),
                allocated_credit_amount=Decimal("14000.000"),
            )

        def post() -> None:
            with audit_context(actor=scene.keeper):
                post_supplier_credit_note(
                    credit_note=SupplierCreditNote.objects.get(pk=note.pk), actor=scene.keeper
                )

        results = _run(post, post)
        assert sorted(results) == ["already_posted", "ok"], results
        assert (
            JournalEntry.objects.filter(
                source_document_type="PROCUREMENT_SUPPLIER_CREDIT_NOTE"
            ).count()
            == 1
        )
        note.refresh_from_db()
        assert note.status == SupplierCreditNoteStatus.POSTED

    def test_two_notes_racing_for_one_return_remainder(self, settings: object) -> None:
        """
        Both want the whole ten kilograms. The return line is locked before
        the remainder is read, so the loser waits, counts the winner's
        committed reservation, and is refused — the claim cannot be settled
        one and a half times whatever the interleaving.
        """
        scene = _Scene()
        supplier_return = scene.posted_return()
        return_line = supplier_return.lines.get()

        def open_and_take(reference: str) -> object:
            def run() -> None:
                with audit_context(actor=scene.keeper):
                    note = create_supplier_credit_note(
                        supplier_return=SupplierReturn.objects.get(pk=supplier_return.pk),
                        created_by=scene.keeper,
                        supplier_document_number=reference,
                        credit_date=CREDITED,
                        amount=Decimal("14000.000"),
                    )
                    add_return_allocation(
                        credit_note=note,
                        return_line=return_line,
                        credited_base_quantity=Decimal("10.000"),
                        allocated_credit_amount=Decimal("14000.000"),
                    )
                    post_supplier_credit_note(credit_note=note, actor=scene.keeper)

            return run

        results = _run(open_and_take("SCN-A"), open_and_take("SCN-B"))
        assert sorted(results) == ["credit_over_quantity", "ok"], results
        assert (
            SupplierCreditNote.objects.filter(
                supplier_return=supplier_return, status=SupplierCreditNoteStatus.POSTED
            ).count()
            == 1
        )

    def test_two_notes_allocations_racing_one_invoice_remainder(self, settings: object) -> None:
        """
        Two returns, two notes, both allocated in full against one 20,000
        invoice — 28,000 wanted, 20,000 available. The posting re-check runs
        under the invoice row lock, so the loser counts the winner's committed
        allocation and is refused rather than crediting the invoice below
        zero.
        """
        scene = _Scene()
        invoice = scene.posted_invoice(amount="20000.000000", reference="INV-RACE")

        notes = []
        for index in range(2):
            supplier_return = scene.posted_return(reference=f"DN-ALLOC-{index}")
            with audit_context(actor=scene.keeper):
                note = create_supplier_credit_note(
                    supplier_return=supplier_return,
                    created_by=scene.keeper,
                    supplier_document_number=f"SCN-ALLOC-{index}",
                    credit_date=CREDITED,
                    amount=Decimal("14000.000"),
                )
                add_return_allocation(
                    credit_note=note,
                    return_line=supplier_return.lines.get(),
                    credited_base_quantity=Decimal("10.000"),
                    allocated_credit_amount=Decimal("14000.000"),
                )
                add_credit_allocation(
                    credit_note=note,
                    invoice=invoice,
                    allocated_amount=Decimal("14000.000"),
                )
            notes.append(note)

        def post(pk: int) -> object:
            def run() -> None:
                with audit_context(actor=scene.keeper):
                    post_supplier_credit_note(
                        credit_note=SupplierCreditNote.objects.get(pk=pk), actor=scene.keeper
                    )

            return run

        results = _run(post(notes[0].pk), post(notes[1].pk))
        assert sorted(results) == ["allocation_over_invoice", "ok"], results
        assert credit_allocated_to(invoice) == Decimal("14000.000")
