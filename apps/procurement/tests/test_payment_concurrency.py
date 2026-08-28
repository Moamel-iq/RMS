"""
Supplier payments under a real COMMIT boundary.

Three races, each exercising a different lock in the order
`apps/procurement/payments.py` documents:

    two posts of one payment              the payment row
    two payments racing one invoice       the invoice row at posting
    a payment racing the invoice reversal the invoice row + the header guard
"""

from __future__ import annotations

import datetime
import threading
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from apps.accounting.models import (
    SUPPLIER_ADVANCE,
    SUPPLIER_PAYABLE,
    SUPPLIER_PAYMENT_BANK,
    SUPPLIER_PAYMENT_CASH,
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
from apps.organizations.models import Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
)
from apps.procurement.invoices import (
    add_account_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    outstanding_amount,
    post_supplier_invoice,
    reverse_supplier_invoice,
)
from apps.procurement.models import (
    SupplierInvoice,
    SupplierPayment,
    SupplierPaymentStatus,
)
from apps.procurement.payments import (
    add_payment_allocation,
    create_supplier_payment,
    post_supplier_payment,
)
from apps.procurement.services import create_supplier
from apps.users.models import User

pytestmark = pytest.mark.django_db

TEST_YEAR = 2026
JAN_1 = datetime.date(TEST_YEAR, 1, 1)
BILLED = datetime.date(TEST_YEAR, 2, 10)
PAID = datetime.date(TEST_YEAR, 2, 20)
PASSWORD = "pw-not-real-1234"


class _Scene:
    """Everything one race needs, built once inside the transactional test."""

    def __init__(self) -> None:
        call_command("seed_units", verbosity=0)
        self.organization = create_organization(code="RACE", name="سباق")
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
            (SUPPLIER_PAYABLE, "2-01-01-001"),
            (SUPPLIER_PAYMENT_CASH, "1-01-01-001"),
            (SUPPLIER_PAYMENT_BANK, "1-01-02-001"),
            (SUPPLIER_ADVANCE, "1-04-01-001"),
        ):
            create_account_mapping(
                organization=self.organization,
                account_role=AccountRole.objects.get(code=code),
                account=Account.objects.get(organization=self.organization, code=account_code),
                effective_from=JAN_1,
            )
        self.supplier = create_supplier(organization=self.organization, code="SUP-01", name="مورد")
        self.keeper = User.objects.create_user(username="race-keeper", password=PASSWORD)
        grant_branch_access(user=self.keeper, branch=self.branch, role=Role.ACCOUNTING_MANAGER)

    def posted_invoice(self, *, amount: str, reference: str) -> SupplierInvoice:
        with audit_context(actor=self.keeper):
            invoice = create_supplier_invoice(
                supplier=self.supplier,
                branch=self.branch,
                created_by=self.keeper,
                supplier_invoice_number=reference,
                invoice_date=BILLED,
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

    def draft_payment(
        self, *, amount: str, reference: str, invoice: SupplierInvoice | None = None
    ) -> SupplierPayment:
        with audit_context(actor=self.keeper):
            payment = create_supplier_payment(
                supplier=self.supplier,
                branch=self.branch,
                created_by=self.keeper,
                paid_at=PAID,
                method="BANK",
                amount=Decimal(amount),
                reference=reference,
            )
            if invoice is not None:
                add_payment_allocation(
                    payment=payment, invoice=invoice, allocated_amount=Decimal(amount)
                )
        return payment


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
class TestPaymentConcurrency:
    def test_two_simultaneous_posts_of_one_payment(self, settings: object) -> None:
        """The payment row is locked first; exactly one journal exists after."""
        scene = _Scene()
        payment = scene.draft_payment(amount="5000.000", reference="TRF-RACE")

        def post() -> None:
            with audit_context(actor=scene.keeper):
                post_supplier_payment(
                    payment=SupplierPayment.objects.get(pk=payment.pk), actor=scene.keeper
                )

        results = _run(post, post)
        assert sorted(results) == ["already_posted", "ok"], results
        assert (
            JournalEntry.objects.filter(source_document_type="PROCUREMENT_SUPPLIER_PAYMENT").count()
            == 1
        )
        payment.refresh_from_db()
        assert payment.status == SupplierPaymentStatus.POSTED

    def test_two_payments_racing_one_invoice_outstanding(self, settings: object) -> None:
        """
        A 6,000 invoice, two 6,000 payments both allocated in full. The
        posting re-check runs under the invoice row lock, so the loser counts
        the winner's committed settlement and is refused rather than paying
        the debt twice.
        """
        scene = _Scene()
        invoice = scene.posted_invoice(amount="6000.000000", reference="INV-RACE")
        first = scene.draft_payment(amount="6000.000", reference="TRF-A", invoice=invoice)
        second = scene.draft_payment(amount="6000.000", reference="TRF-B", invoice=invoice)

        def post(pk: int) -> object:
            def run() -> None:
                with audit_context(actor=scene.keeper):
                    post_supplier_payment(
                        payment=SupplierPayment.objects.get(pk=pk), actor=scene.keeper
                    )

            return run

        results = _run(post(first.pk), post(second.pk))
        assert sorted(results) == ["allocation_over_invoice", "ok"], results
        assert outstanding_amount(invoice) == Decimal("0.000")

    def test_a_payment_racing_the_invoice_reversal(self, settings: object) -> None:
        """
        Either the payment posts and the reversal is refused because a
        standing allocation depends on the invoice, or the reversal wins and
        the payment is refused because the invoice is no longer posted.
        Never both.
        """
        scene = _Scene()
        invoice = scene.posted_invoice(amount="7000.000000", reference="INV-REV")
        payment = scene.draft_payment(amount="7000.000", reference="TRF-C", invoice=invoice)

        def post() -> None:
            with audit_context(actor=scene.keeper):
                post_supplier_payment(
                    payment=SupplierPayment.objects.get(pk=payment.pk), actor=scene.keeper
                )

        def reverse() -> None:
            with audit_context(actor=scene.keeper):
                reverse_supplier_invoice(
                    invoice=SupplierInvoice.objects.get(pk=invoice.pk),
                    actor=scene.keeper,
                    reason="أُلغيت الفاتورة",
                )

        results = _run(post, reverse)
        assert results.count("ok") == 1, results
        payment.refresh_from_db()
        invoice.refresh_from_db()
        if payment.status == SupplierPaymentStatus.POSTED:
            assert invoice.status == "POSTED"
        else:
            assert invoice.status == "REVERSED"
            assert payment.status == SupplierPaymentStatus.DRAFT
