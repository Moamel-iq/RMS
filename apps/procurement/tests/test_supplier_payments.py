"""
Task 2.15 — money leaving for a supplier.

    Dr  SUPPLIER_PAYABLE    the allocated amount
    Dr  SUPPLIER_ADVANCE    the unallocated remainder, where any
        Cr  cash or bank    the full amount

The source account arrives through the method's effective-dated role
(PRC-056); the remainder is an asset and never a negative payable (PRC-055);
over-allocation is impossible on both sides (PRC-054).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.test import Client
from django.urls import reverse

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONTROL,
    SUPPLIER_ADVANCE,
    SUPPLIER_PAYABLE,
    SUPPLIER_PAYMENT_BANK,
    SUPPLIER_PAYMENT_CASH,
    Account,
    AccountRole,
    CostCenter,
    JournalEntry,
    JournalLine,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access, grant_organization_access
from apps.procurement.invoices import (
    add_account_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    outstanding_amount,
    post_supplier_invoice,
    supplier_outstanding,
)
from apps.procurement.models import (
    Supplier,
    SupplierInvoice,
    SupplierPayment,
    SupplierPaymentStatus,
)
from apps.procurement.payments import (
    add_payment_allocation,
    advance_remainder,
    create_supplier_payment,
    delete_supplier_payment,
    post_supplier_payment,
    reverse_supplier_payment,
)
from apps.procurement.reconciliation import verify_procurement, verify_supplier_payments
from apps.procurement.services import create_supplier
from apps.users.models import User

pytestmark = pytest.mark.django_db

TEST_YEAR = 2026
JAN_1 = datetime.date(TEST_YEAR, 1, 1)
BILLED = datetime.date(TEST_YEAR, 3, 1)
PAID = datetime.date(TEST_YEAR, 3, 15)
PASSWORD = "pw-not-real-1234"

PAYABLE_CODE = "2-01-01-001"
CASH_CODE = "1-01-01-001"
BANK_CODE = "1-01-02-001"
ADVANCE_CODE = "1-04-01-001"
EXPENSE_CODE = "5-01-02-003"


@pytest.fixture
def units() -> None:
    call_command("seed_units", verbosity=0)


@pytest.fixture
def accounting(organization: Organization, units: None) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization=organization.code, verbosity=0)


@pytest.fixture
def mapped(organization: Organization, accounting: None) -> None:
    """Everything an invoice and a payment need to post."""
    for code, account_code in (
        (INVENTORY_CONTROL, "1-03-01-001"),
        (GOODS_RECEIVED_NOT_INVOICED, "2-01-02-001"),
        (SUPPLIER_PAYABLE, PAYABLE_CODE),
        (SUPPLIER_PAYMENT_CASH, CASH_CODE),
        (SUPPLIER_PAYMENT_BANK, BANK_CODE),
        (SUPPLIER_ADVANCE, ADVANCE_CODE),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=Account.objects.get(organization=organization, code=account_code),
            effective_from=JAN_1,
        )


@pytest.fixture
def grocery(organization: Organization, mapped: None) -> Supplier:
    return create_supplier(organization=organization, code="GROC-01", name="مورد")


@pytest.fixture
def keeper(branch: Branch) -> User:
    user = User.objects.create_user(username="keeper", password=PASSWORD)
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def manager(organization: Organization) -> User:
    user = User.objects.create_user(username="manager", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


def _posted_invoice(
    *,
    organization: Organization,
    supplier: Supplier,
    branch: Branch,
    actor: User,
    amount: str,
    reference: str,
) -> SupplierInvoice:
    invoice = create_supplier_invoice(
        supplier=supplier,
        branch=branch,
        created_by=actor,
        supplier_invoice_number=reference,
        invoice_date=BILLED,
    )
    add_account_line(
        invoice=invoice,
        account=Account.objects.get(organization=organization, code=EXPENSE_CODE),
        cost_center=CostCenter.objects.filter(organization=organization).first(),
        description="أجور نقل",
        quantity=Decimal("1.000"),
        unit_price=Decimal(amount),
    )
    approve_supplier_invoice(invoice=invoice, actor=actor)
    return post_supplier_invoice(invoice=invoice, actor=actor)


def _draft_payment(
    *,
    supplier: Supplier,
    branch: Branch,
    actor: User,
    amount: str,
    method: str = "BANK",
    reference: str = "TRF-1",
) -> SupplierPayment:
    return create_supplier_payment(
        supplier=supplier,
        branch=branch,
        created_by=actor,
        paid_at=PAID,
        method=method,
        amount=Decimal(amount),
        reference=reference,
    )


def _lines(journal: JournalEntry) -> dict[str, Decimal]:
    return {
        row.account.code: row.debit - row.credit
        for row in journal.lines.select_related("account").all()
    }


def _balance(organization: Organization, code: str) -> Decimal:
    account = Account.objects.get(organization=organization, code=code)
    row = JournalLine.objects.filter(account=account).aggregate(
        debit=Sum("debit"), credit=Sum("credit")
    )
    return (row["debit"] or Decimal("0.000")) - (row["credit"] or Decimal("0.000"))


class TestTheEntry:
    def test_a_fully_allocated_payment_debits_only_the_payable(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
    ) -> None:
        invoice = _posted_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="20000.000000",
            reference="INV-1",
        )
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="20000.000")
        add_payment_allocation(
            payment=payment, invoice=invoice, allocated_amount=Decimal("20000.000")
        )
        posted = post_supplier_payment(payment=payment, actor=keeper)

        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            PAYABLE_CODE: Decimal("20000.000"),
            BANK_CODE: Decimal("-20000.000"),
        }
        assert outstanding_amount(invoice) == Decimal("0.000")
        assert verify_procurement(organization) == []

    def test_the_remainder_stands_as_an_advance_never_a_negative_payable(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
    ) -> None:
        """PRC-055 in one journal: 50,000 paid, 35,000 allocated, 15,000 asset."""
        invoice = _posted_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="35000.000000",
            reference="INV-2",
        )
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="50000.000")
        add_payment_allocation(
            payment=payment, invoice=invoice, allocated_amount=Decimal("35000.000")
        )
        posted = post_supplier_payment(payment=payment, actor=keeper)

        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            PAYABLE_CODE: Decimal("35000.000"),
            ADVANCE_CODE: Decimal("15000.000"),
            BANK_CODE: Decimal("-50000.000"),
        }
        posted.refresh_from_db()
        assert advance_remainder(posted) == Decimal("15000.000")
        assert _balance(organization, ADVANCE_CODE) == Decimal("15000.000")
        assert verify_procurement(organization) == []

    def test_an_unallocated_payment_is_wholly_an_advance(
        self, organization: Organization, grocery: Supplier, branch: Branch, keeper: User
    ) -> None:
        payment = _draft_payment(
            supplier=grocery, branch=branch, actor=keeper, amount="9000.000", method="CASH"
        )
        posted = post_supplier_payment(payment=payment, actor=keeper)
        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            ADVANCE_CODE: Decimal("9000.000"),
            CASH_CODE: Decimal("-9000.000"),
        }

    def test_the_method_chooses_the_source_through_its_role(
        self, organization: Organization, grocery: Supplier, branch: Branch, keeper: User
    ) -> None:
        """PRC-056: cash and bank are roles, and no account is named anywhere."""
        cash = post_supplier_payment(
            payment=_draft_payment(
                supplier=grocery,
                branch=branch,
                actor=keeper,
                amount="1000.000",
                method="CASH",
                reference="C-1",
            ),
            actor=keeper,
        )
        bank = post_supplier_payment(
            payment=_draft_payment(
                supplier=grocery,
                branch=branch,
                actor=keeper,
                amount="1000.000",
                method="BANK",
                reference="B-1",
            ),
            actor=keeper,
        )
        assert cash.journal_entry is not None and bank.journal_entry is not None
        assert CASH_CODE in _lines(cash.journal_entry)
        assert BANK_CODE in _lines(bank.journal_entry)

    def test_partial_payment_across_two_invoices_is_normal(
        self, organization: Organization, grocery: Supplier, branch: Branch, keeper: User
    ) -> None:
        """PRC-053."""
        first = _posted_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="10000.000000",
            reference="INV-3",
        )
        second = _posted_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="8000.000000",
            reference="INV-4",
        )
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="12000.000")
        add_payment_allocation(
            payment=payment, invoice=first, allocated_amount=Decimal("10000.000")
        )
        add_payment_allocation(
            payment=payment, invoice=second, allocated_amount=Decimal("2000.000")
        )
        post_supplier_payment(payment=payment, actor=keeper)
        assert outstanding_amount(first) == Decimal("0.000")
        assert outstanding_amount(second) == Decimal("6000.000")
        assert supplier_outstanding(grocery) == Decimal("6000.000")


class TestTheBounds:
    def test_over_allocating_the_invoice_is_refused(
        self, organization: Organization, grocery: Supplier, branch: Branch, keeper: User
    ) -> None:
        invoice = _posted_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="5000.000000",
            reference="INV-5",
        )
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="9000.000")
        with pytest.raises(ValidationError) as refusal:
            add_payment_allocation(
                payment=payment, invoice=invoice, allocated_amount=Decimal("5000.001")
            )
        assert refusal.value.code == "allocation_over_invoice"

    def test_allocations_may_not_exceed_the_payment(
        self, organization: Organization, grocery: Supplier, branch: Branch, keeper: User
    ) -> None:
        invoice = _posted_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="50000.000000",
            reference="INV-6",
        )
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="4000.000")
        with pytest.raises(ValidationError) as refusal:
            add_payment_allocation(
                payment=payment, invoice=invoice, allocated_amount=Decimal("4000.001")
            )
        assert refusal.value.code == "allocation_over_payment"

    def test_a_settled_invoice_cannot_be_paid_again(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
    ) -> None:
        """
        The one-expression rule: `outstanding_amount` is net of posted credit
        notes and posted payments both, so the bound a second payment reads
        already knows what the first one settled.
        """
        invoice = _posted_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="10000.000000",
            reference="INV-7",
        )
        assert outstanding_amount(invoice) == Decimal("10000.000")
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="10000.000")
        add_payment_allocation(
            payment=payment, invoice=invoice, allocated_amount=Decimal("10000.000")
        )
        post_supplier_payment(payment=payment, actor=keeper)
        assert outstanding_amount(invoice) == Decimal("0.000")
        second = _draft_payment(
            supplier=grocery, branch=branch, actor=keeper, amount="1.000", reference="TRF-2"
        )
        with pytest.raises(ValidationError) as refusal:
            add_payment_allocation(
                payment=second, invoice=invoice, allocated_amount=Decimal("0.001")
            )
        assert refusal.value.code == "allocation_over_invoice"


class TestLifecycle:
    def test_posting_twice_is_refused_and_the_reversal_mirrors(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        manager: User,
    ) -> None:
        invoice = _posted_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="15000.000000",
            reference="INV-8",
        )
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="20000.000")
        add_payment_allocation(
            payment=payment, invoice=invoice, allocated_amount=Decimal("15000.000")
        )
        post_supplier_payment(payment=payment, actor=keeper)
        with pytest.raises(ValidationError) as refusal:
            post_supplier_payment(payment=payment, actor=keeper)
        assert refusal.value.code == "already_posted"

        reversed_payment = reverse_supplier_payment(
            payment=payment, actor=manager, reason="حوالة خاطئة"
        )
        assert reversed_payment.reversal_journal_entry is not None
        assert _lines(reversed_payment.reversal_journal_entry) == {
            PAYABLE_CODE: Decimal("-15000.000"),
            ADVANCE_CODE: Decimal("-5000.000"),
            BANK_CODE: Decimal("20000.000"),
        }
        # The invoice owes again, and the advance account is empty.
        assert outstanding_amount(invoice) == Decimal("15000.000")
        assert _balance(organization, ADVANCE_CODE) == Decimal("0.000")
        assert verify_procurement(organization) == []

    def test_a_draft_can_be_discarded_and_a_posted_one_cannot(
        self, organization: Organization, grocery: Supplier, branch: Branch, keeper: User
    ) -> None:
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="100.000")
        allocationless = payment.pk
        delete_supplier_payment(payment=payment)
        assert not SupplierPayment.objects.filter(pk=allocationless).exists()

        second = _draft_payment(
            supplier=grocery, branch=branch, actor=keeper, amount="100.000", reference="TRF-3"
        )
        post_supplier_payment(payment=second, actor=keeper)
        with pytest.raises(ValidationError):
            delete_supplier_payment(payment=second)

    def test_a_posted_payments_allocation_blocks_the_invoice_reversal(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        manager: User,
    ) -> None:
        """The header walk reads `live_dependency` — the convention, unedited."""
        from apps.procurement.invoices import reverse_supplier_invoice

        invoice = _posted_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="6000.000000",
            reference="INV-9",
        )
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="6000.000")
        add_payment_allocation(
            payment=payment, invoice=invoice, allocated_amount=Decimal("6000.000")
        )
        post_supplier_payment(payment=payment, actor=keeper)
        with pytest.raises(ValidationError) as refusal:
            reverse_supplier_invoice(invoice=invoice, actor=manager, reason="محاولة")
        assert refusal.value.code == "invoice_has_dependents"

        reverse_supplier_payment(payment=payment, actor=manager, reason="أُعيدت")
        assert (
            reverse_supplier_invoice(invoice=invoice, actor=manager, reason="خطأ").status
            == "REVERSED"
        )

    def test_the_dedicated_verifier_stands_alone(
        self, organization: Organization, grocery: Supplier, branch: Branch, keeper: User
    ) -> None:
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="777.000")
        post_supplier_payment(payment=payment, actor=keeper)
        assert verify_supplier_payments(organization) == []


class TestTheSurface:
    def test_the_screens_render_and_commands_refuse_get(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        manager: User,
        accounting_manager: User,
        client: Client,
    ) -> None:
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="500.000")
        client.force_login(manager)
        listing = client.get(reverse("procurement:supplier_payment_list"))
        assert listing.status_code == 200
        fragment = client.get(reverse("procurement:supplier_payment_list"), HTTP_HX_REQUEST="true")
        assert len(fragment.content) < len(listing.content)
        detail = client.get(reverse("procurement:supplier_payment_detail", args=[payment.pk]))
        assert detail.status_code == 200
        # Maker-checker through the routes: the manager records, and only the
        # accounting manager lets the money go.
        assert (
            client.post(reverse("procurement:supplier_payment_post", args=[payment.pk])).status_code
            == 403
        )
        client.force_login(accounting_manager)
        assert (
            client.get(reverse("procurement:supplier_payment_post", args=[payment.pk])).status_code
            == 405
        )
        assert (
            client.post(reverse("procurement:supplier_payment_post", args=[payment.pk])).status_code
            == 302
        )
        payment.refresh_from_db()
        assert payment.status == SupplierPaymentStatus.POSTED

    def test_allocation_uses_an_htmx_panel_swap(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        manager: User,
        mapped: None,
        client: Client,
    ) -> None:
        invoice = _posted_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="500.000",
            reference="INV-HTMX",
        )
        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="500.000")
        client.force_login(manager)

        response = client.post(
            reverse("procurement:supplier_payment_detail", args=[payment.pk]),
            data={"invoice": invoice.pk, "allocated_amount": "500.000", "note": ""},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert b'id="supplier-payment-detail"' in response.content
        assert b"hx-post=" in response.content
        assert payment.allocations.count() == 1

    def test_the_api_drives_the_lifecycle_and_money_is_strings(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        manager: User,
        accounting_manager: User,
        client: Client,
    ) -> None:
        invoice = _posted_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            actor=keeper,
            amount="7000.000000",
            reference="INV-API",
        )
        client.force_login(manager)
        created = client.post(
            "/api/v1/procurement/supplier-payments/",
            data={
                "branch_id": branch.pk,
                "supplier_id": grocery.pk,
                "paid_at": "2026-03-15",
                "method": "BANK",
                "amount": "9000.000",
            },
            content_type="application/json",
        )
        assert created.status_code == 201
        payment_id = created.json()["id"]
        allocated = client.post(
            f"/api/v1/procurement/supplier-payments/{payment_id}/allocations/",
            data={"invoice_id": invoice.pk, "allocated_amount": "7000.000"},
            content_type="application/json",
        )
        assert allocated.status_code == 201

        client.force_login(accounting_manager)
        posted = client.post(f"/api/v1/procurement/supplier-payments/{payment_id}/post/")
        assert posted.status_code == 200
        payload = posted.json()
        assert payload["status"] == "POSTED"
        assert payload["advance"] == "2000.000"
        raw = client.get(f"/api/v1/procurement/supplier-payments/{payment_id}/").content.decode()
        assert '"amount": "9000.000"' in raw or '"amount":"9000.000"' in raw

        reversed_response = client.post(
            f"/api/v1/procurement/supplier-payments/{payment_id}/reverse/",
            data={"reason": "حوالة خاطئة"},
            content_type="application/json",
        )
        assert reversed_response.status_code == 200
        assert reversed_response.json()["status"] == "REVERSED"

    def test_out_of_scope_is_404(
        self,
        organization: Organization,
        grocery: Supplier,
        branch: Branch,
        keeper: User,
        client: Client,
    ) -> None:
        from apps.organizations.services import create_organization

        payment = _draft_payment(supplier=grocery, branch=branch, actor=keeper, amount="100.000")
        outsider = User.objects.create_user(username="spay-outsider", password=PASSWORD)
        rival = create_organization(code="RIV4", name="منافس")
        grant_organization_access(user=outsider, organization=rival, role=Role.MANAGER)
        outsider = User.objects.get(pk=outsider.pk)
        client.force_login(outsider)
        assert (
            client.get(
                reverse("procurement:supplier_payment_detail", args=[payment.pk])
            ).status_code
            == 404
        )
        assert client.get(f"/api/v1/procurement/supplier-payments/{payment.pk}/").status_code == 404

    def test_the_navigation_names_the_payments_screen(self) -> None:
        from apps.core.navigation import MODULES

        procurement = next(m for m in MODULES if m.key == "procurement")
        section = next(s for s in procurement.sections if str(s.label) == "دفعات الموردين")
        assert section.available is True
        assert section.url_name == "procurement:supplier_payment_list"


class TestDemoPayments:
    def test_the_seed_skips_what_is_missing(
        self, organization: Organization, keeper: User, manager: User
    ) -> None:
        from apps.procurement.demo import seed_demo_payments

        assert seed_demo_payments(organization=organization, recorder=keeper, poster=manager) == []
