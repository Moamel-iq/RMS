"""
Supplier invoice posting under a real COMMIT boundary.

Its own file, for the reason `test_receipt_concurrency.py` records: a
transactional test needs a genuine COMMIT and truncates afterwards, and neither
is possible inside the outer atomic block a shared module seed holds open.

Four races, each exercising a different lock in the order
`apps/procurement/invoices.py` documents:

    two posts of one invoice        the invoice row (step 1)
    duplicate supplier reference    the partial unique index (PRC-037)
    posting vs a mapping change     the mapping advisory lock (step 2)
    approve vs post                 the invoice row again, in the other order

`default_connection.close()` appears nowhere here. Task 2.9 proved it poisons
the whole session: closing the main test connection mid-run left every
subsequent test erroring at setup, and the failure looked nothing like its
cause.
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
    SUPPLIER_PAYABLE,
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
    grant_organization_access,
)
from apps.procurement.invoices import (
    add_account_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    post_supplier_invoice,
)
from apps.procurement.models import SupplierInvoice, SupplierInvoiceStatus
from apps.procurement.services import create_supplier
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

TEST_YEAR = 2026
JAN_1 = datetime.date(TEST_YEAR, 1, 1)
INVOICED = datetime.date(TEST_YEAR, 3, 10)
PASSWORD = "pw-not-real-1234"


class _Scene:
    """Everything one race needs, built once inside the transactional test."""

    def __init__(self) -> None:
        call_command("seed_units", verbosity=0)
        UnitOfMeasure.objects.get(code="KG")

        self.organization = create_organization(code="RACEINV", name="سباق الفواتير")
        self.branch = create_branch(
            organization=self.organization,
            code="MAIN",
            name="الرئيسي",
            business_day_start_time=datetime.time(9, 0),
        )
        configure_accounting(organization=self.organization, fiscal_year_start_month=1)
        open_fiscal_year(organization=self.organization, year=TEST_YEAR)
        call_command("seed_chart_of_accounts", organization=self.organization.code, verbosity=0)

        self.payable = Account.objects.get(organization=self.organization, code="2-01-01-001")
        self.expense = Account.objects.get(organization=self.organization, code="5-01-02-003")
        # Every expense account in this chart demands a cost centre.
        self.cost_center = CostCenter.objects.get(organization=self.organization, code="DELIVERY")
        self.other_payable = Account.objects.get(organization=self.organization, code="2-01-02-001")
        create_account_mapping(
            organization=self.organization,
            account_role=AccountRole.objects.get(code=SUPPLIER_PAYABLE),
            account=self.payable,
            effective_from=JAN_1,
        )

        self.supplier = create_supplier(organization=self.organization, code="SUP-01", name="مورد")
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

    def invoice(self, *, reference: str, approve: bool = True) -> SupplierInvoice:
        with audit_context(actor=self.clerk):
            invoice = create_supplier_invoice(
                supplier=self.supplier,
                branch=self.branch,
                created_by=self.clerk,
                supplier_invoice_number=reference,
                invoice_date=INVOICED,
                business_date=INVOICED,
            )
            add_account_line(
                invoice=invoice,
                account=self.expense,
                cost_center=self.cost_center,
                description="أجور نقل",
                quantity=Decimal("1.000"),
                unit_price=Decimal("50000.000000"),
            )
        if approve:
            with audit_context(actor=self.controller):
                approve_supplier_invoice(invoice=invoice, actor=self.controller)
        return SupplierInvoice.objects.get(pk=invoice.pk)


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
            # The worker's own connection only. Closing the main test
            # connection here is what poisoned the session in Task 2.9.
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
class TestInvoicePostingConcurrency:
    def test_two_simultaneous_posts_of_one_invoice(self, settings: object) -> None:
        """
        The invoice row is locked first, so the loser waits and then reads
        POSTED — the state the winner committed, not the APPROVED it started
        with. Exactly one journal exists afterwards.
        """
        scene = _Scene()
        invoice = scene.invoice(reference="RACE-1")

        def post() -> None:
            with audit_context(actor=scene.controller):
                post_supplier_invoice(
                    invoice=SupplierInvoice.objects.get(pk=invoice.pk), actor=scene.controller
                )

        results = _run(post, post)
        assert sorted(results) == ["already_posted", "ok"], results
        assert JournalEntry.objects.count() == 1
        invoice.refresh_from_db()
        assert invoice.status == SupplierInvoiceStatus.POSTED
        assert invoice.posted_amount == Decimal("50000.000")

    def test_two_invoices_racing_for_one_supplier_reference(self, settings: object) -> None:
        """
        PRC-037 at the database. Two clerks entering the same supplier invoice
        number at the same instant: the partial unique index decides, not a
        service check that read before the other committed.
        """
        scene = _Scene()

        def enter() -> None:
            with audit_context(actor=scene.clerk):
                create_supplier_invoice(
                    supplier=scene.supplier,
                    branch=scene.branch,
                    created_by=scene.clerk,
                    supplier_invoice_number="DUPLICATE-1",
                    invoice_date=INVOICED,
                    business_date=INVOICED,
                )

        results = _run(enter, enter)
        assert results.count("ok") == 1, results
        assert (
            SupplierInvoice.objects.filter(supplier_invoice_number_key="DUPLICATE-1").count() == 1
        )

    def test_posting_racing_a_payable_mapping_change(self, settings: object) -> None:
        """
        The mapping advisory lock sits above every row lock a posting takes, so
        a mutation cannot slip between the resolution and the commit. Whichever
        wins, the credit lands in exactly one account and the entry balances.
        """
        scene = _Scene()
        invoice = scene.invoice(reference="RACE-MAP")

        def post() -> None:
            with audit_context(actor=scene.controller):
                post_supplier_invoice(
                    invoice=SupplierInvoice.objects.get(pk=invoice.pk), actor=scene.controller
                )

        def remap() -> None:
            with audit_context(actor=scene.controller):
                create_account_mapping(
                    organization=scene.organization,
                    account_role=AccountRole.objects.get(code=SUPPLIER_PAYABLE),
                    account=scene.other_payable,
                    effective_from=JAN_1,
                )

        _run(post, remap)
        invoice.refresh_from_db()
        if invoice.status != SupplierInvoiceStatus.POSTED:
            return
        journal = JournalEntry.objects.get()
        credits = list(journal.lines.filter(credit__gt=0))
        assert len(credits) == 1
        assert credits[0].credit == invoice.posted_amount
        assert sum(row.debit for row in journal.lines.all()) == credits[0].credit

    def test_approval_racing_posting(self, settings: object) -> None:
        """
        Both orderings are safe. Either the approval lands first and the post
        succeeds, or the post arrives first and is refused because the invoice
        is still a draft. What must never happen is a posted draft.
        """
        scene = _Scene()
        invoice = scene.invoice(reference="RACE-APPROVE", approve=False)

        def approve() -> None:
            with audit_context(actor=scene.controller):
                approve_supplier_invoice(
                    invoice=SupplierInvoice.objects.get(pk=invoice.pk), actor=scene.controller
                )

        def post() -> None:
            with audit_context(actor=scene.controller):
                post_supplier_invoice(
                    invoice=SupplierInvoice.objects.get(pk=invoice.pk), actor=scene.controller
                )

        results = _run(approve, post)
        assert "ok" in results, results
        invoice.refresh_from_db()
        assert invoice.status in (
            SupplierInvoiceStatus.APPROVED,
            SupplierInvoiceStatus.POSTED,
        )
        if invoice.status == SupplierInvoiceStatus.POSTED:
            assert JournalEntry.objects.count() == 1
            assert invoice.approved_at is not None
        else:
            assert JournalEntry.objects.count() == 0
