"""
Task 2.12 — the entry that turns a match into money, and the boundary it holds.

Task 2.11 decided what covers what. This is the task that clears GRNI, credits
the supplier and parks the difference, and these tests are what say it does so
in the one shape Task 2.0 §9 and ADR-022 specify:

    Dr  GRNI                                what the deliveries posted
    Dr  purchase price variance clearing     the difference, when dearer
        Cr  supplier payable                 what the supplier charges

with the difference on the other side when the invoice is cheaper, and the line
**absent** when the two agree.

`TestTheStepFifteenBoundary` is the negative half: ordinary invoice goods lines
do not move stock or revalue it. Structured landed-cost charges are the narrow,
explicit zero-quantity value-only route tested at the end of this module.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONTROL,
    PURCHASE_PRICE_VARIANCE,
    SUPPLIER_PAYABLE,
    Account,
    AccountClass,
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
from apps.inventory.ledger import MovementInput, post_stock_entry
from apps.inventory.models import (
    InventoryItem,
    ItemType,
    MovementType,
    StockBalance,
    StockLocationMovement,
    StockMovement,
    Warehouse,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access, grant_organization_access
from apps.procurement.additional_costs import (
    create_charge,
    preview_charge_allocations,
    save_manual_shares,
)
from apps.procurement.invoices import (
    add_account_line,
    add_inventory_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    post_supplier_invoice,
    return_supplier_invoice_to_draft,
    reverse_supplier_invoice,
    supplier_outstanding,
)
from apps.procurement.matching import (
    add_allocation,
    cancel_purchase_match,
    create_purchase_match,
    mark_match_ready,
)
from apps.procurement.models import (
    GoodsReceipt,
    PurchaseMatch,
    PurchaseMatchStatus,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceChargeAllocationBasis,
    SupplierInvoiceChargeCategory,
    SupplierInvoiceChargeTreatment,
    SupplierInvoicePosting,
    SupplierInvoicePostingStatus,
    SupplierInvoiceStatus,
)
from apps.procurement.posting import post_goods_receipt, reverse_goods_receipt
from apps.procurement.reconciliation import verify_invoice_charges, verify_procurement
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

GRNI_CODE = "2-01-02-001"
PAYABLE_CODE = "2-01-01-001"
VARIANCE_CODE = "8-01-03-001"
EXPENSE_CODE = "5-01-02-003"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def units() -> None:
    call_command("seed_units", verbosity=0)


@pytest.fixture
def kilogram(units: None) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="KG")


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=TEST_YEAR)
    call_command("seed_chart_of_accounts", organization=organization.code, verbosity=0)


@pytest.fixture
def mapped(organization: Organization, accounting: None) -> None:
    """Everything a receipt, an invoice and a variance need to post."""
    for code, account_code in (
        (INVENTORY_CONTROL, "1-03-01-001"),
        (GOODS_RECEIVED_NOT_INVOICED, GRNI_CODE),
        (SUPPLIER_PAYABLE, PAYABLE_CODE),
        (PURCHASE_PRICE_VARIANCE, VARIANCE_CODE),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=Account.objects.get(organization=organization, code=account_code),
            effective_from=JAN_1,
        )


@pytest.fixture
def unmapped_variance(organization: Organization, accounting: None) -> None:
    """Everything except the variance role, which stays deliberately unmapped."""
    for code, account_code in (
        (INVENTORY_CONTROL, "1-03-01-001"),
        (GOODS_RECEIVED_NOT_INVOICED, GRNI_CODE),
        (SUPPLIER_PAYABLE, PAYABLE_CODE),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=Account.objects.get(organization=organization, code=account_code),
            effective_from=JAN_1,
        )


@pytest.fixture
def rice(organization: Organization, kilogram: UnitOfMeasure) -> InventoryItem:
    from apps.inventory.services import create_item, create_item_category

    return create_item(
        organization=organization,
        code="RICE",
        name_ar="رز",
        category=create_item_category(organization=organization, code="GRAINS", name_ar="حبوب"),
        item_type=ItemType.RAW_MATERIAL,
        base_unit=kilogram,
    )


@pytest.fixture
def store(branch: Branch) -> Warehouse:
    from apps.inventory.services import create_warehouse

    return create_warehouse(branch=branch, code="MAIN", name_ar="مخزن")


@pytest.fixture
def grocery(organization: Organization) -> Supplier:
    return create_supplier(organization=organization, code="GROC-01", name_ar="مورد")


@pytest.fixture
def keeper(branch: Branch, store: Warehouse) -> User:
    user = User.objects.create_user(username="keeper", password=PASSWORD)
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def clerk(organization: Organization) -> User:
    user = User.objects.create_user(username="clerk", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTANT)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def controller(organization: Organization) -> User:
    user = User.objects.create_user(username="controller", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


def _post_receipt(
    *,
    supplier: Supplier,
    branch: Branch,
    store: Warehouse,
    keeper: User,
    item: InventoryItem,
    quantity: str,
    price: str,
    reference: str,
) -> GoodsReceipt:
    receipt = create_goods_receipt(
        supplier=supplier,
        branch=branch,
        warehouse=store,
        created_by=keeper,
        received_at=RECEIVED,
        delivery_reference=reference,
        evidence_reference="إشعار",
    )
    line = add_receipt_line(
        receipt=receipt,
        item=item,
        delivered_quantity=Decimal(quantity),
        unit_price=Decimal(price),
    )
    inspect_receipt_line(line=line, accepted_base_quantity=Decimal(quantity), actor=keeper)
    return post_goods_receipt(receipt=receipt, actor=keeper)


def _approved_invoice(
    *,
    supplier: Supplier,
    branch: Branch,
    clerk: User,
    controller: User,
    item: InventoryItem,
    quantity: str,
    price: str,
    reference: str = "INV-001",
) -> SupplierInvoice:
    invoice = create_supplier_invoice(
        supplier=supplier,
        branch=branch,
        created_by=clerk,
        supplier_invoice_number=reference,
        invoice_date=INVOICED,
        business_date=INVOICED,
    )
    add_inventory_line(
        invoice=invoice,
        item=item,
        base_quantity=Decimal(quantity),
        unit_price=Decimal(price),
    )
    return approve_supplier_invoice(invoice=invoice, actor=controller)


def _matched(
    *,
    invoice: SupplierInvoice,
    receipt: GoodsReceipt,
    clerk: User,
    quantity: str = "50.000",
) -> PurchaseMatch:
    match = create_purchase_match(invoice=invoice, created_by=clerk)
    add_allocation(
        match=match,
        invoice_line=invoice.lines.get(),
        receipt_line=receipt.lines.get(),
        matched_base_quantity=Decimal(quantity),
        created_by=clerk,
    )
    return mark_match_ready(match=match, actor=clerk)


@pytest.fixture
def receipt(
    grocery: Supplier,
    branch: Branch,
    store: Warehouse,
    keeper: User,
    rice: InventoryItem,
    mapped: None,
) -> GoodsReceipt:
    """Fifty kilograms at 1,400 — 70,000 parked in GRNI."""
    return _post_receipt(
        supplier=grocery,
        branch=branch,
        store=store,
        keeper=keeper,
        item=rice,
        quantity="50.000",
        price="1400.000000",
        reference="DN-1",
    )


@pytest.fixture
def dearer(
    grocery: Supplier, branch: Branch, clerk: User, controller: User, rice: InventoryItem
) -> SupplierInvoice:
    """Fifty at 1,450 — 72,500 claimed against 70,000 delivered."""
    return _approved_invoice(
        supplier=grocery,
        branch=branch,
        clerk=clerk,
        controller=controller,
        item=rice,
        quantity="50.000",
        price="1450.000000",
    )


def _balance(organization: Organization, code: str) -> Decimal:
    account = Account.objects.get(organization=organization, code=code)
    row = JournalLine.objects.filter(account=account).aggregate(
        debit=Sum("debit"), credit=Sum("credit")
    )
    return (row["debit"] or Decimal("0.000")) - (row["credit"] or Decimal("0.000"))


def _lines(journal: JournalEntry) -> dict[str, Decimal]:
    """Account code -> signed movement, for reading an entry at a glance."""
    return {
        row.account.code: row.debit - row.credit
        for row in journal.lines.select_related("account").all()
    }


# ---------------------------------------------------------------------------
# The entry
# ---------------------------------------------------------------------------


class TestTheEntry:
    """
    Task 2.0 §9's journal, in all four shapes, with the arithmetic checked
    against the stored figures rather than against a number typed in a test.
    """

    def test_a_dearer_invoice_debits_the_difference(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        organization: Organization,
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        posted = post_supplier_invoice(invoice=dearer, actor=controller)

        assert posted.status == SupplierInvoiceStatus.POSTED
        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            GRNI_CODE: Decimal("70000.000"),
            VARIANCE_CODE: Decimal("2500.000"),
            PAYABLE_CODE: Decimal("-72500.000"),
        }

    def test_a_cheaper_invoice_credits_the_difference(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """
        The other side, and the reason the sign is a side rather than a minus:
        the kernel refuses a negative amount and says to use the other side.
        """
        receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="50.000",
            price="1400.000000",
            reference="DN-CHEAP",
        )
        invoice = _approved_invoice(
            supplier=grocery,
            branch=branch,
            clerk=clerk,
            controller=controller,
            item=rice,
            quantity="50.000",
            price="1360.000000",
            reference="INV-CHEAP",
        )
        _matched(invoice=invoice, receipt=receipt, clerk=clerk)
        posted = post_supplier_invoice(invoice=invoice, actor=controller)

        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            GRNI_CODE: Decimal("70000.000"),
            VARIANCE_CODE: Decimal("-2000.000"),
            PAYABLE_CODE: Decimal("-68000.000"),
        }

    def test_an_agreeing_invoice_posts_two_lines_and_no_variance(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """
        The common case, and it must be clean. A zero third line is refused by
        the kernel and would be noise even if it were not.
        """
        receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="50.000",
            price="1400.000000",
            reference="DN-AGREE",
        )
        invoice = _approved_invoice(
            supplier=grocery,
            branch=branch,
            clerk=clerk,
            controller=controller,
            item=rice,
            quantity="50.000",
            price="1400.000000",
            reference="INV-AGREE",
        )
        _matched(invoice=invoice, receipt=receipt, clerk=clerk)
        posted = post_supplier_invoice(invoice=invoice, actor=controller)

        assert posted.journal_entry is not None
        assert posted.journal_entry.lines.count() == 2
        assert _lines(posted.journal_entry) == {
            GRNI_CODE: Decimal("70000.000"),
            PAYABLE_CODE: Decimal("-70000.000"),
        }

    def test_an_agreeing_invoice_needs_no_variance_mapping(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        unmapped_variance: None,
    ) -> None:
        """
        A role nobody has mapped must not block an invoice that agrees with its
        delivery. The account is resolved only when there is a difference to
        put in it.
        """
        receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="10.000",
            price="1000.000000",
            reference="DN-NOMAP",
        )
        invoice = _approved_invoice(
            supplier=grocery,
            branch=branch,
            clerk=clerk,
            controller=controller,
            item=rice,
            quantity="10.000",
            price="1000.000000",
            reference="INV-NOMAP",
        )
        _matched(invoice=invoice, receipt=receipt, clerk=clerk, quantity="10.000")
        posted = post_supplier_invoice(invoice=invoice, actor=controller)
        assert posted.status == SupplierInvoiceStatus.POSTED

    def test_a_variance_without_a_mapping_refuses_the_whole_posting(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        unmapped_variance: None,
        organization: Organization,
    ) -> None:
        """And nothing partial is left behind (PRC-034, PRC-036)."""
        receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="10.000",
            price="1000.000000",
            reference="DN-NOMAP2",
        )
        invoice = _approved_invoice(
            supplier=grocery,
            branch=branch,
            clerk=clerk,
            controller=controller,
            item=rice,
            quantity="10.000",
            price="1100.000000",
            reference="INV-NOMAP2",
        )
        _matched(invoice=invoice, receipt=receipt, clerk=clerk, quantity="10.000")
        journals = JournalEntry.objects.count()
        with pytest.raises(ValidationError) as error:
            post_supplier_invoice(invoice=invoice, actor=controller)
        assert error.value.code == "account_role_unmapped"
        invoice.refresh_from_db()
        assert invoice.status == SupplierInvoiceStatus.APPROVED
        assert JournalEntry.objects.count() == journals
        assert not SupplierInvoicePosting.objects.exists()

    def test_a_mixed_invoice_posts_both_halves_in_one_entry(
        self,
        receipt: GoodsReceipt,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        organization: Organization,
        mapped: None,
    ) -> None:
        """
        A delivery charge and the goods it delivered, on one document. One
        payable credit, because the supplier is owed one amount.
        """
        raw = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number="INV-MIXED",
            invoice_date=INVOICED,
            business_date=INVOICED,
        )
        add_inventory_line(
            invoice=raw,
            item=rice,
            base_quantity=Decimal("50.000"),
            unit_price=Decimal("1450.000000"),
        )
        add_account_line(
            invoice=raw,
            account=Account.objects.get(organization=organization, code=EXPENSE_CODE),
            cost_center=CostCenter.objects.get(organization=organization, code="DELIVERY"),
            description="أجور نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("5000.000000"),
        )
        invoice = approve_supplier_invoice(invoice=raw, actor=controller)
        match = create_purchase_match(invoice=invoice, created_by=clerk)
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(sequence=1),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        posted = post_supplier_invoice(invoice=invoice, actor=controller)

        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            EXPENSE_CODE: Decimal("5000.000"),
            GRNI_CODE: Decimal("70000.000"),
            VARIANCE_CODE: Decimal("2500.000"),
            PAYABLE_CODE: Decimal("-77500.000"),
        }
        assert posted.posted_amount == Decimal("77500.000")

    def test_the_payable_is_the_whole_invoice_not_the_direct_charges(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        """
        Task 2.10 stored only the direct-charge total here, because that was
        all it could post. Leaving it would understate every matched invoice by
        the goods portion, and `verify_supplier_payables` reads it.
        """
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        posted = post_supplier_invoice(invoice=dearer, actor=controller)
        assert posted.posted_amount == Decimal("72500.000")
        assert supplier_outstanding(posted.supplier) == Decimal("72500.000")

    def test_grni_clears_to_exactly_zero(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        organization: Organization,
    ) -> None:
        """
        The claim ADR-023 §1 makes about GRNI: its balance is the value of
        accepted receipt lines no invoice has matched. Nothing left over.
        """
        assert _balance(organization, GRNI_CODE) == Decimal("-70000.000")
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        assert _balance(organization, GRNI_CODE) == Decimal("0.000")

    def test_the_posting_records_what_it_did(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        posted = post_supplier_invoice(invoice=dearer, actor=controller)

        posting = SupplierInvoicePosting.objects.get()
        assert posting.generation == 1
        assert posting.status == SupplierInvoicePostingStatus.LIVE
        assert posting.goods_cleared_value == Decimal("70000.000")
        assert posting.invoice_matched_value == Decimal("72500.000")
        assert posting.price_variance == Decimal("2500.000")
        assert posting.direct_charge_value == Decimal("0.000")
        assert posting.payable_value == Decimal("72500.000")
        assert posting.journal_entry == posted.journal_entry
        assert posting.purchase_match is not None

    def test_the_journal_names_the_generation_not_the_invoice(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        """
        ADR-017 source identity. Keying on the invoice would make a re-post
        look like a retry of the first posting, and the kernel would refuse it.
        """
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        posted = post_supplier_invoice(invoice=dearer, actor=controller)
        posting = SupplierInvoicePosting.objects.get()
        assert posted.journal_entry is not None
        assert posted.journal_entry.source_document_type == "PROCUREMENT_SUPPLIER_INVOICE"
        assert posted.journal_entry.source_document_id == str(posting.public_id)
        assert posted.journal_entry.source_document_id != str(posted.public_id)

    def test_a_direct_only_invoice_still_posts_and_needs_no_match(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        organization: Organization,
        mapped: None,
    ) -> None:
        """Task 2.10's shape, unchanged — with a generation record beneath it."""
        raw = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number="INV-EXPENSE",
            invoice_date=INVOICED,
            business_date=INVOICED,
        )
        add_account_line(
            invoice=raw,
            account=Account.objects.get(organization=organization, code=EXPENSE_CODE),
            cost_center=CostCenter.objects.get(organization=organization, code="DELIVERY"),
            description="أجور نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("9000.000000"),
        )
        invoice = approve_supplier_invoice(invoice=raw, actor=controller)
        posted = post_supplier_invoice(invoice=invoice, actor=controller)

        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            EXPENSE_CODE: Decimal("9000.000"),
            PAYABLE_CODE: Decimal("-9000.000"),
        }
        posting = SupplierInvoicePosting.objects.get()
        assert posting.purchase_match is None
        assert posting.goods_cleared_value == Decimal("0.000")


# ---------------------------------------------------------------------------
# Lifecycle, correction and generations
# ---------------------------------------------------------------------------


class TestTheCorrectionPath:
    """
    The sequence that has to work, and the three guards that would each,
    alone, make it impossible.
    """

    def test_a_posted_invoice_can_be_reversed(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        """
        The one that could not have worked before this task. The invoice's own
        dependency guard counts match allocations, and those allocations are
        the *precondition* for the posting — so a bare existence check made
        every matched invoice permanently irreversible.
        """
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        reversed_invoice = reverse_supplier_invoice(
            invoice=dearer, actor=controller, reason="خُصّصت الشحنة الخطأ"
        )
        assert reversed_invoice.status == SupplierInvoiceStatus.REVERSED

    def test_reversing_unwinds_the_ledger_exactly(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        organization: Organization,
    ) -> None:
        """Every account back where it started, GRNI included."""
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        reverse_supplier_invoice(invoice=dearer, actor=controller, reason="خطأ")

        assert _balance(organization, GRNI_CODE) == Decimal("-70000.000")
        assert _balance(organization, PAYABLE_CODE) == Decimal("0.000")
        assert _balance(organization, VARIANCE_CODE) == Decimal("0.000")

    def test_reversing_cancels_the_match_and_frees_the_delivery(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        """
        The last step of the reversal, and the reason it is one command: the
        two guards involved have no legal order as separate operator actions.
        """
        match = _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        reverse_supplier_invoice(invoice=dearer, actor=controller, reason="خطأ")

        match.refresh_from_db()
        assert match.status == PurchaseMatchStatus.CANCELLED
        assert "أُعكست الفاتورة" in match.cancellation_reason

        from apps.procurement.matching import receipt_availability

        assert receipt_availability(receipt.lines.get()).quantity == Decimal("50.000")

    def test_the_generation_is_reversed_not_deleted(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        reverse_supplier_invoice(invoice=dearer, actor=controller, reason="خطأ")

        posting = SupplierInvoicePosting.objects.get()
        assert posting.status == SupplierInvoicePostingStatus.REVERSED
        assert posting.reversal_journal_entry is not None
        assert posting.reversed_by is not None
        assert posting.reversal_reason == "خطأ"
        assert posting.purchase_match is not None

    def test_the_delivery_can_be_reversed_after_the_invoice_is(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        with pytest.raises(ValidationError) as error:
            reverse_goods_receipt(receipt=receipt, actor=controller, reason="محاولة")
        assert error.value.code == "receipt_has_dependents"

        reverse_supplier_invoice(invoice=dearer, actor=controller, reason="خطأ")
        reverse_goods_receipt(receipt=receipt, actor=controller, reason="أُعيدت الشحنة")
        receipt.refresh_from_db()
        assert receipt.status == "REVERSED"

    def test_a_corrected_match_posts_as_the_next_generation(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        mapped: None,
        organization: Organization,
    ) -> None:
        """
        The whole point of generations: the same invoice, matched against the
        delivery it was actually for, posted again — with its own source
        identity so the kernel does not read it as a retry.
        """
        wrong = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="50.000",
            price="1400.000000",
            reference="DN-WRONG",
        )
        right = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="50.000",
            price="1420.000000",
            reference="DN-RIGHT",
        )
        invoice = _approved_invoice(
            supplier=grocery,
            branch=branch,
            clerk=clerk,
            controller=controller,
            item=rice,
            quantity="50.000",
            price="1450.000000",
            reference="INV-REGEN",
        )
        _matched(invoice=invoice, receipt=wrong, clerk=clerk)
        post_supplier_invoice(invoice=invoice, actor=controller)
        reverse_supplier_invoice(invoice=invoice, actor=controller, reason="الشحنة الخطأ")

        invoice.refresh_from_db()
        _matched(invoice=invoice, receipt=right, clerk=clerk)
        again = post_supplier_invoice(invoice=invoice, actor=controller)

        assert again.status == SupplierInvoiceStatus.POSTED
        assert again.reversal_journal_entry is None
        assert again.reversed_at is None
        generations = list(
            SupplierInvoicePosting.objects.order_by("generation").values_list(
                "generation", "status"
            )
        )
        assert generations == [(1, "REVERSED"), (2, "LIVE")]
        # The second delivery is the one now cleared, at its own value.
        assert _balance(organization, GRNI_CODE) == Decimal("-70000.000")
        posting = SupplierInvoicePosting.objects.get(generation=2)
        assert posting.goods_cleared_value == Decimal("71000.000")
        assert posting.price_variance == Decimal("1500.000")

    def test_a_repost_keeps_the_document_number(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        """A second number would leave a gap an auditor reads as a lost invoice."""
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        first = post_supplier_invoice(invoice=dearer, actor=controller)
        number = first.number
        reverse_supplier_invoice(invoice=dearer, actor=controller, reason="خطأ")
        dearer.refresh_from_db()
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        again = post_supplier_invoice(invoice=dearer, actor=controller)
        assert again.number == number

    def test_a_reversed_invoice_needs_a_new_match_before_it_can_post(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        reverse_supplier_invoice(invoice=dearer, actor=controller, reason="خطأ")
        dearer.refresh_from_db()
        with pytest.raises(ValidationError) as error:
            post_supplier_invoice(invoice=dearer, actor=controller)
        assert error.value.code == "repost_needs_a_new_match"

    def test_a_match_backing_a_live_posting_cannot_be_cancelled(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        """
        Without this, cancelling would release the delivery while the ledger
        still carried the invoice — and both sides of every Task 2.11 equality
        would move together, so no verifier would notice.
        """
        match = _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        with pytest.raises(ValidationError) as error:
            cancel_purchase_match(match=match, actor=controller, reason="محاولة")
        assert error.value.code == "match_backs_a_live_posting"

    def test_the_database_refuses_it_too(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        """
        The belt to that brace. ADR-023 §3's reasoning about over-allocation
        applies here word for word: a guard living only inside one service
        function is one refactor away from not existing.
        """
        match = _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        with pytest.raises(IntegrityError, match="live posting"), transaction.atomic():
            PurchaseMatch.objects.filter(pk=match.pk).update(
                status=PurchaseMatchStatus.CANCELLED,
                cancelled_by=controller,
                cancelled_at=timezone.now(),
                cancellation_reason="تجاوز",
            )

    def test_a_posted_invoice_cannot_be_matched_again(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        """
        A second match on a posted invoice would consume receipt availability
        the ledger has already spent, re-block the delivery, and have no
        posting path of its own to resolve it.
        """
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        dearer.refresh_from_db()
        with pytest.raises(ValidationError) as error:
            create_purchase_match(invoice=dearer, created_by=clerk)
        assert error.value.code == "invoice_already_posted"

    def test_an_approved_invoice_with_a_match_cannot_be_returned_to_draft(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        """
        In draft the line freeze lifts and `_recalculate` rewrites every net
        amount — including lines nobody touched, because changing the freight
        re-runs the allocation across all of them. The frozen allocation would
        then cite a figure the line no longer states.
        """
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        with pytest.raises(ValidationError) as error:
            return_supplier_invoice_to_draft(invoice=dearer, actor=controller, reason="تصحيح")
        assert error.value.code == "invoice_has_an_active_match"

    def test_withdrawing_the_match_frees_the_invoice_for_correction(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        match = _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        cancel_purchase_match(match=match, actor=controller, reason="خطأ في التخصيص")
        returned = return_supplier_invoice_to_draft(
            invoice=dearer, actor=controller, reason="تصحيح السعر"
        )
        assert returned.status == SupplierInvoiceStatus.DRAFT

    def test_an_unmatched_goods_invoice_still_waits(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, controller: User
    ) -> None:
        """Task 2.10's refusal, unchanged where it is still right."""
        with pytest.raises(ValidationError) as error:
            post_supplier_invoice(invoice=dearer, actor=controller)
        assert error.value.code == "invoice_awaiting_matching"

    def test_a_draft_match_cannot_be_posted_from(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        match = create_purchase_match(invoice=dearer, created_by=clerk)
        add_allocation(
            match=match,
            invoice_line=dearer.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        with pytest.raises(ValidationError) as error:
            post_supplier_invoice(invoice=dearer, actor=controller)
        assert error.value.code == "match_not_ready"

    def test_a_partly_matched_invoice_waits_for_the_rest(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        """
        The whole document or none of it. Half a payable is a debt for part of
        what is owed, which is a worse answer than no payable at all.
        """
        match = create_purchase_match(invoice=dearer, created_by=clerk)
        add_allocation(
            match=match,
            invoice_line=dearer.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("30.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        with pytest.raises(ValidationError) as error:
            post_supplier_invoice(invoice=dearer, actor=controller)
        assert error.value.code == "invoice_partly_matched"


class TestImmutabilityAndIdempotency:
    def test_posting_twice_is_refused(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        dearer.refresh_from_db()
        with pytest.raises(ValidationError) as error:
            post_supplier_invoice(invoice=dearer, actor=controller)
        assert error.value.code == "already_posted"

    def test_a_live_posting_cannot_be_edited(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        posting = SupplierInvoicePosting.objects.get()
        with pytest.raises(IntegrityError, match="live"), transaction.atomic():
            SupplierInvoicePosting.objects.filter(pk=posting.pk).update(
                price_variance=Decimal("1.000")
            )

    def test_a_reversed_posting_is_frozen(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        reverse_supplier_invoice(invoice=dearer, actor=controller, reason="خطأ")
        posting = SupplierInvoicePosting.objects.get()
        with pytest.raises(IntegrityError, match="reversed"), transaction.atomic():
            SupplierInvoicePosting.objects.filter(pk=posting.pk).update(reversal_reason="شيء آخر")

    def test_a_posting_is_never_deleted(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        posting = SupplierInvoicePosting.objects.get()
        with pytest.raises(IntegrityError, match="cannot be deleted"), transaction.atomic():
            SupplierInvoicePosting.objects.filter(pk=posting.pk).delete()

    def test_two_live_generations_are_impossible(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        """
        The partial unique index, which is what two concurrent posts collide
        on if the row lock somehow did not serialise them.
        """
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        first = SupplierInvoicePosting.objects.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierInvoicePosting.objects.create(
                organization=first.organization,
                supplier_invoice=first.supplier_invoice,
                purchase_match=first.purchase_match,
                generation=99,
                journal_entry=first.journal_entry,
                allocation_fingerprint=first.allocation_fingerprint,
                goods_cleared_value=first.goods_cleared_value,
                invoice_matched_value=first.invoice_matched_value,
                price_variance=first.price_variance,
                direct_charge_value=first.direct_charge_value,
                payable_value=first.payable_value,
                posted_by=first.posted_by,
                posted_at=first.posted_at,
            )

    def test_the_variance_is_its_own_arithmetic_at_the_database(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        first = SupplierInvoicePosting.objects.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierInvoicePosting.objects.create(
                organization=first.organization,
                supplier_invoice=first.supplier_invoice,
                purchase_match=first.purchase_match,
                generation=98,
                status=SupplierInvoicePostingStatus.REVERSED,
                journal_entry=first.journal_entry,
                reversal_journal_entry=first.journal_entry,
                reversed_by=first.posted_by,
                reversed_at=first.posted_at,
                reversal_reason="x",
                allocation_fingerprint=first.allocation_fingerprint,
                goods_cleared_value=Decimal("10.000"),
                invoice_matched_value=Decimal("20.000"),
                price_variance=Decimal("999.000"),
                direct_charge_value=Decimal("0.000"),
                payable_value=Decimal("20.000"),
                posted_by=first.posted_by,
                posted_at=first.posted_at,
            )


# ---------------------------------------------------------------------------
# The Step 15 boundary: PRC-044 is deferred and not elected
# ---------------------------------------------------------------------------


class TestTheStepFifteenBoundary:
    """
    Task 2.12 posts money and touches no stock, and these are the assertions
    that keep it that way.

    PRC-044 — carrying the difference into inventory value where the goods are
    still on hand — is **deferred and formally not elected**. It needs a
    permission, a source-document identity, an inventory-versus-cost-of-sales
    allocation policy, journal shapes for both directions, and locking,
    idempotency, reversal and period-close rules. None of those exist in an
    approved document, and a partial implementation of a valuation rule is
    worse than none: it would move an inventory figure nobody could derive
    from a document they were shown.

    Every negative here is a positive twin some later task must deliberately
    write, the way this task rewrote Task 2.11's.
    """

    def test_posting_moves_no_stock_at_all(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        movements = StockMovement.objects.count()
        located = StockLocationMovement.objects.count()
        post_supplier_invoice(invoice=dearer, actor=controller)
        assert StockMovement.objects.count() == movements
        assert StockLocationMovement.objects.count() == located

    def test_a_reversal_moves_no_stock_either(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, controller: User
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        movements = StockMovement.objects.count()
        reverse_supplier_invoice(invoice=dearer, actor=controller, reason="خطأ")
        assert StockMovement.objects.count() == movements

    def test_the_moving_average_is_untouched_by_a_variance(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        store: Warehouse,
    ) -> None:
        """
        PRC-043, asserted on the figure that would actually move. The invoice
        says 1,450; the stock stays at the 1,400 the delivery posted, because
        repricing a receipt would restate every issue that followed it.
        """
        balance = StockBalance.objects.get(warehouse=store, item=rice, lot__isnull=True)
        before_quantity = balance.quantity
        before_value = balance.value
        before_average = balance.average_cost

        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)

        balance.refresh_from_db()
        assert balance.quantity == before_quantity
        assert balance.value == before_value
        assert balance.average_cost == before_average == Decimal("1400.000000")

    def test_the_variance_account_is_a_clearing_account(
        self, organization: Organization, mapped: None
    ) -> None:
        """
        Not cost of sales. ADR-022 rejects conflating a purchasing outcome with
        a consumption outcome, and class 5 would additionally demand a cost
        centre a supplier invoice has nowhere to get.
        """
        account = Account.objects.get(organization=organization, code=VARIANCE_CODE)
        assert account.account_class == AccountClass.CLEARING
        assert account.requires_cost_center is False

    def test_no_revaluation_service_exists(self) -> None:
        """A revaluation command appearing here would mean PRC-044 was elected."""
        from apps.procurement import invoices, matching

        for module in (invoices, matching):
            for name in (
                "revalue_inventory",
                "revalue_from_variance",
                "allocate_variance",
                "split_variance",
                "capitalise_variance",
            ):
                assert not hasattr(module, name), f"{name} belongs to the deferred PRC-044"

    def test_no_revaluation_route_exists(self) -> None:
        from django.urls import NoReverseMatch
        from django.urls import reverse as resolve

        for name in ("procurement:variance_revalue", "procurement:posting_revalue"):
            with pytest.raises(NoReverseMatch):
                resolve(name, args=[1])

    def test_no_revaluation_permission_exists(self) -> None:
        from apps.procurement import permissions as procurement_permissions

        codes = {
            name
            for name in dir(procurement_permissions)
            if name.isupper() and isinstance(getattr(procurement_permissions, name), str)
        }
        assert not {name for name in codes if "REVALU" in name}

    def test_the_variance_balance_is_expected_to_stand(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        organization: Organization,
    ) -> None:
        """
        Parked, not cleared. Nothing in Task 2.12 empties this account, and the
        verifier says so rather than reporting a non-zero balance as a fault.
        """
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        assert _balance(organization, VARIANCE_CODE) == Decimal("2500.000")
        assert verify_procurement(organization) == []


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_a_dearer_posting_reconciles(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        organization: Organization,
    ) -> None:
        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        assert verify_procurement(organization) == []

    def test_a_cheaper_posting_reconciles(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        mapped: None,
        organization: Organization,
    ) -> None:
        """
        The direction Task 2.10's verifier was wrong about. Its two blanket
        sums compared total debits and total credits against the line total,
        which on a favourable variance are 70,000 and 68,000 — so both fired on
        a correct posting, while the dearer case passed by algebraic accident.
        """
        receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="50.000",
            price="1400.000000",
            reference="DN-REC-CHEAP",
        )
        invoice = _approved_invoice(
            supplier=grocery,
            branch=branch,
            clerk=clerk,
            controller=controller,
            item=rice,
            quantity="50.000",
            price="1360.000000",
            reference="INV-REC-CHEAP",
        )
        _matched(invoice=invoice, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=invoice, actor=controller)
        assert verify_procurement(organization) == []

    def test_grni_equals_the_uncleared_delivery_value(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        mapped: None,
        organization: Organization,
    ) -> None:
        """
        Invariant 47, walked through its three states: nothing billed, one of
        two deliveries billed, and the bill reversed again.
        """
        from apps.procurement.reconciliation import verify_grni_clearing

        first = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="50.000",
            price="1400.000000",
            reference="DN-47-A",
        )
        _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="20.000",
            price="1400.000000",
            reference="DN-47-B",
        )
        assert _balance(organization, GRNI_CODE) == Decimal("-98000.000")
        assert verify_grni_clearing(organization) == []

        invoice = _approved_invoice(
            supplier=grocery,
            branch=branch,
            clerk=clerk,
            controller=controller,
            item=rice,
            quantity="50.000",
            price="1450.000000",
            reference="INV-47",
        )
        _matched(invoice=invoice, receipt=first, clerk=clerk)
        post_supplier_invoice(invoice=invoice, actor=controller)

        assert _balance(organization, GRNI_CODE) == Decimal("-28000.000")
        assert verify_grni_clearing(organization) == []

        reverse_supplier_invoice(invoice=invoice, actor=controller, reason="خطأ")
        assert _balance(organization, GRNI_CODE) == Decimal("-98000.000")
        assert verify_grni_clearing(organization) == []

    def test_a_ready_match_clears_nothing_from_grni(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        organization: Organization,
    ) -> None:
        """
        Cleared is not the same as matched, and this is the difference. A ready
        match consumes availability and clears no money: the evidence is agreed
        but nobody has been billed.
        """
        from apps.procurement.reconciliation import verify_grni_clearing

        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        assert _balance(organization, GRNI_CODE) == Decimal("-70000.000")
        assert verify_grni_clearing(organization) == []

    def test_the_parked_variance_traces_to_its_allocations(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        organization: Organization,
    ) -> None:
        from apps.procurement.reconciliation import verify_parked_variance

        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        assert verify_parked_variance(organization) == []
        assert _balance(organization, VARIANCE_CODE) == Decimal("2500.000")

    def test_the_verifier_notices_a_planted_journal(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        organization: Organization,
    ) -> None:
        """
        A manual journal against the variance account is exactly the drift this
        report exists to surface, so it must not pass.
        """
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine
        from apps.procurement.reconciliation import verify_parked_variance

        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        post_entry(
            organization=organization,
            accounting_date=INVOICED,
            lines=[
                PostingLine(
                    account=Account.objects.get(organization=organization, code=VARIANCE_CODE),
                    branch=dearer.branch,
                    debit=Decimal("500.000"),
                ),
                PostingLine(
                    account=Account.objects.get(organization=organization, code=PAYABLE_CODE),
                    branch=dearer.branch,
                    credit=Decimal("500.000"),
                ),
            ],
            idempotency_key="planted-by-a-test",
            narration="قيد يدوي",
        )
        problems = verify_parked_variance(organization)
        assert any(problem.field == "parked_price_variance" for problem in problems)


# ---------------------------------------------------------------------------
# Permissions, API and screens
# ---------------------------------------------------------------------------


class TestSurface:
    def test_an_accountant_cannot_post_an_invoice(
        self, receipt: GoodsReceipt, dearer: SupplierInvoice, clerk: User, client: Client
    ) -> None:
        """
        Separation of duties, unchanged from Task 2.10 and more consequential
        now: posting clears GRNI and creates a payable. An accountant matches;
        an accounting manager posts.
        """
        from apps.procurement.permissions import POST_SUPPLIER_INVOICE

        _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        assert not clerk.has_perm(POST_SUPPLIER_INVOICE)
        client.force_login(clerk)
        response = client.post(
            reverse("procurement:supplier_invoice_post", args=[dearer.pk]),
            {},
        )
        assert response.status_code == 403
        dearer.refresh_from_db()
        assert dearer.status == SupplierInvoiceStatus.APPROVED

    def test_the_api_reports_the_posting_on_the_match(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        client: Client,
    ) -> None:
        """
        The positive twin of Task 2.11's `is_financially_posted is False`.
        Derived from the posting table, so a reversal turns it back to false
        without anybody rewriting a stored flag.
        """
        match = _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        client.force_login(controller)

        before = client.get(f"/api/v1/procurement/matches/{match.pk}/").json()
        assert before["is_financially_posted"] is False
        assert before["posting_generation"] is None

        post_supplier_invoice(invoice=dearer, actor=controller)
        after = client.get(f"/api/v1/procurement/matches/{match.pk}/").json()
        assert after["is_financially_posted"] is True
        assert after["posting_generation"] == 1
        assert after["supplier_invoice_status"] == "POSTED"
        assert after["journal_entry"]
        assert after["goods_cleared_value"] == "70000.000"
        assert after["posted_price_variance"] == "2500.000"

        reverse_supplier_invoice(invoice=dearer, actor=controller, reason="خطأ")
        undone = client.get(f"/api/v1/procurement/matches/{match.pk}/").json()
        assert undone["is_financially_posted"] is False

    def test_the_posted_figures_are_gated_on_the_cost_permission(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        client: Client,
    ) -> None:
        """
        PRC-061: omitted, not blanked. A null would still say a number exists
        and you are not trusted with it, which is a different statement.

        Asserted on the serializer, because every role that can *see* a match
        in this system also holds cost visibility — so the endpoint alone
        cannot demonstrate the gate, and a test that used the endpoint would
        be proving the permission map rather than the serialization.
        """
        from apps.procurement.api import _serialize_match

        match = _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        match.refresh_from_db()

        with_cost = _serialize_match(match, include_cost=True)
        assert with_cost["goods_cleared_value"] == "70000.000"
        assert with_cost["posted_price_variance"] == "2500.000"

        without = _serialize_match(match, include_cost=False)
        assert without["is_financially_posted"] is True
        assert without["posting_generation"] == 1
        for money in (
            "goods_cleared_value",
            "invoice_matched_value",
            "posted_price_variance",
            "total_price_variance",
        ):
            assert money not in without

    def test_money_crosses_the_wire_as_strings(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        client: Client,
    ) -> None:
        match = _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        post_supplier_invoice(invoice=dearer, actor=controller)
        client.force_login(controller)
        payload = client.get(f"/api/v1/procurement/matches/{match.pk}/").json()
        for key in (
            "goods_cleared_value",
            "invoice_matched_value",
            "posted_price_variance",
            "total_price_variance",
        ):
            assert isinstance(payload[key], str)

    def test_the_detail_screen_says_what_was_posted(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        client: Client,
    ) -> None:
        """
        The Arabic sentence Task 2.11 put on this screen said nothing had been
        posted. It must stop saying that the moment something has.
        """
        match = _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        client.force_login(controller)
        url = reverse("procurement:purchase_match_detail", args=[match.pk])

        before = client.get(url).content.decode()
        assert "المطابقة أدلة، لا نقود" in before

        post_supplier_invoice(invoice=dearer, actor=controller)
        after = client.get(url).content.decode()
        assert "المطابقة أدلة، لا نقود" not in after
        assert "رُحِّلت هذه المطابقة مالياً" in after
        assert "لم تتحرّك أي كمية مخزنية" in after
        assert "70,000.000" in after

    def test_a_posted_match_offers_no_cancel_button(
        self,
        receipt: GoodsReceipt,
        dearer: SupplierInvoice,
        clerk: User,
        controller: User,
        client: Client,
    ) -> None:
        """The screen must not offer an action the service will refuse."""
        match = _matched(invoice=dearer, receipt=receipt, clerk=clerk)
        client.force_login(controller)
        url = reverse("procurement:purchase_match_detail", args=[match.pk])
        assert "إلغاء المطابقة" in client.get(url).content.decode()

        post_supplier_invoice(invoice=dearer, actor=controller)
        posted = client.get(url).content.decode()
        assert "إلغاء المطابقة" not in posted
        assert "التصحيح يكون بعكس الفاتورة" in posted

    def test_the_admin_exposes_no_writable_posting(self) -> None:
        """PRC-062: no writable admin for a posted procurement record."""
        from django.contrib import admin

        from apps.procurement.models import SupplierInvoicePosting as Model

        registered = admin.site._registry.get(Model)
        if registered is None:
            return
        assert registered.has_add_permission(None) is False  # type: ignore[arg-type]
        assert registered.has_change_permission(None) is False  # type: ignore[arg-type]
        assert registered.has_delete_permission(None) is False  # type: ignore[arg-type]


class TestStructuredAdditionalCosts:
    """Actual costs keep their own evidence and one exact accounting route."""

    def _draft_goods_invoice(
        self,
        *,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        rice: InventoryItem,
        reference: str,
    ) -> SupplierInvoice:
        invoice = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number=reference,
            invoice_date=INVOICED,
            business_date=INVOICED,
        )
        add_inventory_line(
            invoice=invoice,
            item=rice,
            base_quantity=Decimal("50.000"),
            unit_price=Decimal("1450.000000"),
        )
        return invoice

    def test_landed_cost_posts_value_without_quantity_and_reverses_exactly(
        self,
        receipt: GoodsReceipt,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        rice: InventoryItem,
        clerk: User,
        controller: User,
    ) -> None:
        invoice = self._draft_goods_invoice(
            grocery=grocery,
            branch=branch,
            clerk=clerk,
            rice=rice,
            reference="INV-LANDED-1",
        )
        charge = create_charge(
            invoice=invoice,
            actor=clerk,
            category=SupplierInvoiceChargeCategory.FREIGHT,
            treatment=SupplierInvoiceChargeTreatment.LANDED_COST,
            description="شحن محلي",
            amount=Decimal("1000.000"),
            allocation_basis=SupplierInvoiceChargeAllocationBasis.RECEIPT_VALUE,
            evidence_reference="FRT-1",
        )
        approve_supplier_invoice(invoice=invoice, actor=controller)
        _matched(invoice=invoice, receipt=receipt, clerk=clerk)

        before = StockBalance.objects.get(warehouse=store, item=rice, lot=None)
        before_quantity, before_value = before.quantity, before.value
        preview = preview_charge_allocations(charge)
        assert [row.allocated_amount for row in preview] == [Decimal("1000.000")]

        posted = post_supplier_invoice(invoice=invoice, actor=controller)
        posting = posted.postings.get(status=SupplierInvoicePostingStatus.LIVE)
        after = StockBalance.objects.get(warehouse=store, item=rice, lot=None)
        assert after.quantity == before_quantity
        assert after.value == before_value + Decimal("1000.000")
        assert posting.landed_cost_value == Decimal("1000.000")
        assert posting.stock_entry is not None
        movement = posting.stock_entry.movements.get()
        assert movement.base_quantity == Decimal("0.000")
        assert movement.inventory_value == Decimal("1000.000")
        allocation = posting.landed_cost_allocations.get()
        assert allocation.allocated_amount == charge.amount
        assert allocation.inventory_movement == movement
        assert _lines(posting.journal_entry)["1-03-01-001"] == Decimal("1000.000")
        assert posting.payable_value == posted.total_amount == Decimal("73500.000")
        assert verify_invoice_charges(invoice.organization) == []

        reverse_supplier_invoice(invoice=posted, actor=controller, reason="تصحيح")
        posting.refresh_from_db()
        restored = StockBalance.objects.get(warehouse=store, item=rice, lot=None)
        assert restored.quantity == before_quantity
        assert restored.value == before_value
        assert posting.reversal_stock_entry is not None
        reverse_movement = posting.reversal_stock_entry.movements.get()
        assert reverse_movement.base_quantity == Decimal("0.000")
        assert reverse_movement.inventory_value == Decimal("-1000.000")
        assert reverse_movement.reverses == movement

    def test_downstream_outbound_refuses_capitalisation(
        self,
        receipt: GoodsReceipt,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        rice: InventoryItem,
        clerk: User,
        controller: User,
        organization: Organization,
    ) -> None:
        invoice = self._draft_goods_invoice(
            grocery=grocery,
            branch=branch,
            clerk=clerk,
            rice=rice,
            reference="INV-LANDED-OUT",
        )
        charge = create_charge(
            invoice=invoice,
            actor=clerk,
            category=SupplierInvoiceChargeCategory.HANDLING,
            treatment=SupplierInvoiceChargeTreatment.LANDED_COST,
            description="مناولة",
            amount=Decimal("500.000"),
        )
        approve_supplier_invoice(invoice=invoice, actor=controller)
        _matched(invoice=invoice, receipt=receipt, clerk=clerk)
        post_stock_entry(
            organization=organization,
            effects=[
                MovementInput(
                    warehouse=store,
                    item=rice,
                    movement_type=MovementType.ISSUE,
                    quantity=Decimal("1.000"),
                    effect_key="downstream-issue",
                )
            ],
            idempotency_key="downstream-after-receipt",
            business_date=INVOICED,
            reason="صرف بعد الاستلام",
        )

        with pytest.raises(ValidationError) as refused:
            preview_charge_allocations(charge)
        assert refused.value.code == "landed_cost_has_downstream_outbound"
        with pytest.raises(ValidationError) as posting_refused:
            post_supplier_invoice(invoice=invoice, actor=controller)
        assert posting_refused.value.code == "landed_cost_has_downstream_outbound"
        invoice.refresh_from_db()
        assert invoice.status == SupplierInvoiceStatus.APPROVED
        assert invoice.journal_entry_id is None

    def test_manual_shares_must_equal_the_charge(
        self,
        receipt: GoodsReceipt,
        grocery: Supplier,
        branch: Branch,
        rice: InventoryItem,
        clerk: User,
        controller: User,
    ) -> None:
        invoice = self._draft_goods_invoice(
            grocery=grocery,
            branch=branch,
            clerk=clerk,
            rice=rice,
            reference="INV-LANDED-MANUAL",
        )
        charge = create_charge(
            invoice=invoice,
            actor=clerk,
            category=SupplierInvoiceChargeCategory.INSURANCE,
            treatment=SupplierInvoiceChargeTreatment.LANDED_COST,
            description="تأمين",
            amount=Decimal("750.000"),
            allocation_basis=SupplierInvoiceChargeAllocationBasis.MANUAL,
        )
        approve_supplier_invoice(invoice=invoice, actor=controller)
        match = _matched(invoice=invoice, receipt=receipt, clerk=clerk)
        target = match.allocations.get()

        with pytest.raises(ValidationError) as refused:
            save_manual_shares(
                charge=charge,
                actor=clerk,
                shares={target.pk: Decimal("749.999")},
            )
        assert refused.value.code == "manual_shares_do_not_balance"
        save_manual_shares(
            charge=charge,
            actor=clerk,
            shares={target.pk: Decimal("750.000")},
        )
        assert preview_charge_allocations(charge)[0].allocated_amount == Decimal("750.000")

    def test_direct_charge_posts_to_its_account_and_cost_center(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        organization: Organization,
        mapped: None,
    ) -> None:
        invoice = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number="INV-DIRECT-COST",
            invoice_date=INVOICED,
            business_date=INVOICED,
        )
        account = Account.objects.get(organization=organization, code=EXPENSE_CODE)
        center = CostCenter.objects.create(
            organization=organization,
            code="PROC",
            name_ar="المشتريات",
            name_en="Procurement",
        )
        add_account_line(
            invoice=invoice,
            account=account,
            cost_center=center,
            description="خدمة",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000.000000"),
        )
        create_charge(
            invoice=invoice,
            actor=clerk,
            category=SupplierInvoiceChargeCategory.DELIVERY,
            treatment=SupplierInvoiceChargeTreatment.DIRECT_EXPENSE,
            description="توصيل خارجي",
            amount=Decimal("250.000"),
            direct_account=account,
            cost_center=center,
        )
        approve_supplier_invoice(invoice=invoice, actor=controller)
        posted = post_supplier_invoice(invoice=invoice, actor=controller)
        posting = posted.postings.get()
        assert posting.stock_entry_id is None
        assert posting.direct_charge_value == Decimal("1250.000")
        expense_line = posting.journal_entry.lines.get(account=account)
        assert expense_line.debit == Decimal("1250.000")
        assert expense_line.cost_center == center

    def test_additional_cost_workspace_is_rtl_and_htmx_ready(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        organization: Organization,
        mapped: None,
        client: Client,
    ) -> None:
        invoice = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number="INV-COST-UI",
            invoice_date=INVOICED,
            business_date=INVOICED,
        )
        account = Account.objects.get(organization=organization, code=EXPENSE_CODE)
        center = CostCenter.objects.create(
            organization=organization,
            code="UI",
            name_ar="واجهة",
            name_en="UI",
        )
        add_account_line(
            invoice=invoice,
            account=account,
            cost_center=center,
            description="خدمة",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.000000"),
        )
        charge = create_charge(
            invoice=invoice,
            actor=clerk,
            category=SupplierInvoiceChargeCategory.OTHER,
            treatment=SupplierInvoiceChargeTreatment.DIRECT_EXPENSE,
            description="تكلفة اختبار",
            amount=Decimal("10.000"),
            direct_account=account,
            cost_center=center,
        )
        client.force_login(clerk)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
        list_response = client.get(reverse("procurement:supplier_invoice_charge_list"))
        html = list_response.content.decode()
        assert list_response.status_code == 200
        assert "التكاليف الإضافية" in html
        assert "تكلفة اختبار" in html
        detail = client.get(reverse("procurement:supplier_invoice_charge_detail", args=[charge.pk]))
        assert detail.status_code == 200
        form = client.get(
            reverse("procurement:supplier_invoice_charge_update", args=[charge.pk])
        ).content.decode()
        assert "hx-post" in form
        assert 'dir="rtl"' in html
