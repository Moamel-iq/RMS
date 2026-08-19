"""
Task 2.10 — supplier invoices, the payable, and the boundary at matching.

Six classes carry the weight.

`TestInvoiceIdentity` covers PRC-037. Paying the same invoice twice is the most
expensive ordinary mistake in accounts payable, so the tests here are about the
ways a duplicate could sneak past: whitespace, case, and the leading zeros that
must NOT be folded.

`TestTheLineTypeInvariant` covers the one structural rule of the line model. A
line bills for goods or for an account, never both and never neither, and the
database is the thing that says so.

`TestTotalsAndAllocation` covers PRC-039. Freight and discount go through
`apps.core.allocation`, so the parts sum exactly to the whole and the answer
does not depend on the order a queryset returned.

`TestPostingAndThePayable` is the accounting: `Dr` the charge, `Cr` supplier
payable, and — asserted with a count either side — no stock effect whatsoever.

`TestTheMatchingBoundary` is the one this task exists to hold. An invoice line
that bills for goods has no determinate accounting until Task 2.11 matches it,
so posting is refused rather than guessed. These are the positive twins of the
absence assertions Task 2.11 will delete.

`TestReversal` mirrors exactly and refuses to do it twice.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONTROL,
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
from apps.core.models import AuditEvent
from apps.inventory.models import (
    InventoryItem,
    ItemType,
    StockLocationMovement,
    StockMovement,
    Warehouse,
)
from apps.organizations.authorization import OutOfScope
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access, grant_organization_access
from apps.procurement.invoices import (
    SOURCE_DOCUMENT_TYPE,
    add_account_line,
    add_inventory_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    delete_supplier_invoice,
    due_date_for,
    normalize_invoice_number,
    outstanding_amount,
    post_supplier_invoice,
    remove_invoice_line,
    return_supplier_invoice_to_draft,
    reverse_supplier_invoice,
    supplier_outstanding,
    update_supplier_invoice,
)
from apps.procurement.models import (
    GoodsReceipt,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceLineType,
    SupplierInvoiceStatus,
)
from apps.procurement.permissions import (
    APPROVE_SUPPLIER_INVOICE,
    CREATE_SUPPLIER_INVOICE,
    POST_SUPPLIER_INVOICE,
    VIEW_SUPPLIER_INVOICE,
    permissions_for_role,
)
from apps.procurement.reconciliation import (
    verify_invoice_charges,
    verify_procurement,
    verify_supplier_invoice,
    verify_supplier_payables,
)
from apps.procurement.selectors import (
    open_payables,
    resolve_supplier_invoice,
    visible_supplier_invoices,
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
INVOICED = datetime.date(TEST_YEAR, 3, 10)
PASSWORD = "pw-not-real-1234"
HX = {"hx-request": "true"}


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
def payable_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="2-01-01-001")


@pytest.fixture
def expense_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="5-01-02-003")


@pytest.fixture
def second_expense_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="5-01-02-002")


@pytest.fixture
def delivery(organization: Organization, accounting: None) -> CostCenter:
    """Every expense account in this chart demands a cost centre; this is it."""
    return CostCenter.objects.get(organization=organization, code="DELIVERY")


@pytest.fixture
def kitchen(organization: Organization, accounting: None) -> CostCenter:
    return CostCenter.objects.get(organization=organization, code="KITCHEN")


@pytest.fixture
def mapped(organization: Organization, payable_account: Account) -> None:
    """The one role a Task 2.10 posting resolves."""
    create_account_mapping(
        organization=organization,
        account_role=AccountRole.objects.get(code=SUPPLIER_PAYABLE),
        account=payable_account,
        effective_from=JAN_1,
    )


@pytest.fixture
def inventory_mapped(organization: Organization, accounting: None) -> None:
    """What a goods receipt needs, for the tests that post one first."""
    for code, account_code in (
        (INVENTORY_CONTROL, "1-03-01-001"),
        (GOODS_RECEIVED_NOT_INVOICED, "2-01-02-001"),
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
    return create_supplier(
        organization=organization, code="GROC-01", name_ar="مورد", payment_terms_days=30
    )


@pytest.fixture
def other_grocery(organization: Organization) -> Supplier:
    return create_supplier(organization=organization, code="GROC-02", name_ar="مورد آخر")


@pytest.fixture
def clerk(organization: Organization) -> User:
    """An accountant: enters invoices, approves none."""
    user = User.objects.create_user(username="clerk", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTANT)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def controller(organization: Organization) -> User:
    """An accounting manager: approves, posts, reverses — and enters nothing."""
    user = User.objects.create_user(username="controller", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def buyer(organization: Organization) -> User:
    """Purchasing reaches invoice evidence; tests can remove only its cost grant."""
    user = User.objects.create_user(username="buyer", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.PURCHASING)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def keeper(branch: Branch, store: Warehouse) -> User:
    """A storekeeper: branch-scoped, and no business with a payable."""
    user = User.objects.create_user(username="keeper", password=PASSWORD)
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


def _invoice(
    *,
    grocery: Supplier,
    branch: Branch,
    clerk: User,
    reference: str = "INV-001",
    **extra: object,
) -> SupplierInvoice:
    return create_supplier_invoice(
        supplier=grocery,
        branch=branch,
        created_by=clerk,
        supplier_invoice_number=reference,
        invoice_date=INVOICED,
        business_date=INVOICED,
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def expense_invoice(
    grocery: Supplier,
    branch: Branch,
    clerk: User,
    expense_account: Account,
    delivery: CostCenter,
    mapped: None,
) -> SupplierInvoice:
    """One delivery charge of 75,000. Every line has a complete route."""
    invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
    add_account_line(
        invoice=invoice,
        account=expense_account,
        cost_center=delivery,
        description="أجور نقل",
        quantity=Decimal("1.000"),
        unit_price=Decimal("75000.000000"),
    )
    return SupplierInvoice.objects.get(pk=invoice.pk)


@pytest.fixture
def posted_receipt(
    grocery: Supplier,
    branch: Branch,
    store: Warehouse,
    keeper: User,
    rice: InventoryItem,
    inventory_mapped: None,
) -> GoodsReceipt:
    from apps.procurement.posting import post_goods_receipt

    receipt = create_goods_receipt(
        supplier=grocery,
        branch=branch,
        warehouse=store,
        created_by=keeper,
        received_at=datetime.date(TEST_YEAR, 3, 1),
        delivery_reference="DN-1",
        evidence_reference="إشعار",
    )
    line = add_receipt_line(
        receipt=receipt,
        item=rice,
        delivered_quantity=Decimal("50.000"),
        unit_price=Decimal("1400.000000"),
    )
    inspect_receipt_line(line=line, accepted_base_quantity=Decimal("50.000"), actor=keeper)
    return post_goods_receipt(receipt=receipt, actor=keeper)


# ---------------------------------------------------------------------------
# Identity (PRC-037)
# ---------------------------------------------------------------------------


class TestInvoiceIdentity:
    def test_the_normalizer_folds_whitespace_and_case(self) -> None:
        assert normalize_invoice_number(" INV-001 ") == "INV-001"
        assert normalize_invoice_number("inv-001") == "INV-001"
        assert normalize_invoice_number("INV-001\n") == "INV-001"

    def test_the_normalizer_preserves_leading_zeros(self) -> None:
        """
        `INV-001` and `INV-0001` are different documents, and a normalizer that
        parsed either as a number would merge them.
        """
        assert normalize_invoice_number("INV-001") != normalize_invoice_number("INV-0001")
        assert normalize_invoice_number("0042") == "0042"

    def test_the_normalizer_keeps_internal_spacing(self) -> None:
        """Two suppliers' numbering habits are not ours to reconcile."""
        assert normalize_invoice_number("INV 001") != normalize_invoice_number("INV001")

    def test_the_supplier_reference_is_stored_as_written(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="  inv-001  ")
        assert invoice.supplier_invoice_number == "inv-001"
        assert invoice.supplier_invoice_number_key == "INV-001"

    def test_the_same_number_twice_for_one_supplier_is_refused(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
    ) -> None:
        _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-001")
        with pytest.raises(IntegrityError), transaction.atomic():
            _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-001")

    def test_whitespace_does_not_create_a_second_invoice(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
    ) -> None:
        """The expensive one: a retyped reference with a stray space."""
        _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-001")
        with pytest.raises(IntegrityError), transaction.atomic():
            _invoice(grocery=grocery, branch=branch, clerk=clerk, reference=" INV-001 ")

    def test_case_does_not_create_a_second_invoice(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
    ) -> None:
        _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-001")
        with pytest.raises(IntegrityError), transaction.atomic():
            _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="inv-001")

    def test_leading_zeros_do_make_a_different_invoice(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
    ) -> None:
        _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-001")
        second = _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-0001")
        assert second.pk is not None

    def test_another_supplier_may_use_the_same_number(
        self,
        grocery: Supplier,
        other_grocery: Supplier,
        branch: Branch,
        clerk: User,
    ) -> None:
        """Two suppliers both numbering an invoice `1` is not a conflict."""
        first = _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="1")
        second = _invoice(grocery=other_grocery, branch=branch, clerk=clerk, reference="1")
        assert first.supplier_invoice_number_key == second.supplier_invoice_number_key
        assert first.supplier_id != second.supplier_id
        assert SupplierInvoice.objects.count() == 2

    def test_an_empty_reference_is_refused(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
    ) -> None:
        with pytest.raises(ValidationError) as error:
            _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="   ")
        assert error.value.code == "supplier_invoice_number_required"

    def test_the_due_date_uses_the_terms_snapshot(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
    ) -> None:
        """
        Thirty days from the invoice date, from the terms copied at creation.
        Renegotiating in March must not restate a January due date.
        """
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        assert invoice.payment_terms_days == 30
        assert invoice.due_date == INVOICED + datetime.timedelta(days=30)

        grocery.payment_terms_days = 60
        grocery.save(update_fields=["payment_terms_days"])
        invoice.refresh_from_db()
        assert invoice.due_date == INVOICED + datetime.timedelta(days=30)

    def test_the_due_date_helper_is_pure(self) -> None:
        assert due_date_for(invoice_date=INVOICED, payment_terms_days=0) == INVOICED
        assert due_date_for(
            invoice_date=INVOICED, payment_terms_days=14
        ) == INVOICED + datetime.timedelta(days=14)


# ---------------------------------------------------------------------------
# The line-type invariant
# ---------------------------------------------------------------------------


class TestTheLineTypeInvariant:
    def test_an_inventory_line_names_an_item_and_no_account(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        rice: InventoryItem,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        line = add_inventory_line(
            invoice=invoice,
            item=rice,
            base_quantity=Decimal("10.000"),
            unit_price=Decimal("1400.000000"),
        )
        assert line.line_type == SupplierInvoiceLineType.INVENTORY
        assert line.account is None
        assert line.base_quantity == Decimal("10.000")

    def test_an_account_line_names_an_account_and_no_item(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        expense_account: Account,
        delivery: CostCenter,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        line = add_account_line(
            invoice=invoice,
            account=expense_account,
            cost_center=delivery,
            description="نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000.000000"),
        )
        assert line.line_type == SupplierInvoiceLineType.ACCOUNT
        assert line.item is None
        assert line.base_quantity is None

    def test_a_line_that_is_both_is_refused_by_the_database(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        rice: InventoryItem,
        expense_account: Account,
        delivery: CostCenter,
    ) -> None:
        """
        The invariant the services could forget. A line naming an item *and* an
        expense account would have two contradictory debits, and no principled
        way to choose — so the shape does not exist in the table.
        """
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        line = add_inventory_line(
            invoice=invoice,
            item=rice,
            base_quantity=Decimal("10.000"),
            unit_price=Decimal("1400.000000"),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierInvoiceLine.objects.filter(pk=line.pk).update(account=expense_account)

    def test_a_line_that_is_neither_is_refused_by_the_database(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        rice: InventoryItem,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        line = add_inventory_line(
            invoice=invoice,
            item=rice,
            base_quantity=Decimal("10.000"),
            unit_price=Decimal("1400.000000"),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierInvoiceLine.objects.filter(pk=line.pk).update(item=None)

    def test_an_account_line_cannot_carry_a_receipt_reference(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        expense_account: Account,
        delivery: CostCenter,
        posted_receipt: GoodsReceipt,
    ) -> None:
        """A delivery charge is not evidence about a delivery of goods."""
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-ACC")
        line = add_account_line(
            invoice=invoice,
            account=expense_account,
            cost_center=delivery,
            description="نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000.000000"),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierInvoiceLine.objects.filter(pk=line.pk).update(
                receipt_line=posted_receipt.lines.first()
            )

    def test_a_foreign_account_is_refused(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        other_organization: Organization,
        delivery: CostCenter,
        accounting: None,
    ) -> None:
        configure_accounting(organization=other_organization, fiscal_year_start_month=1)
        call_command("seed_chart_of_accounts", organization=other_organization.code, verbosity=0)
        foreign = Account.objects.get(organization=other_organization, code="5-01-02-003")
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        with pytest.raises(ValidationError) as error:
            add_account_line(
                invoice=invoice,
                account=foreign,
                cost_center=delivery,
                description="نقل",
                quantity=Decimal("1.000"),
                unit_price=Decimal("1000.000000"),
            )
        assert error.value.code == "organization_mismatch"

    def test_a_heading_account_is_refused(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        organization: Organization,
        delivery: CostCenter,
        accounting: None,
    ) -> None:
        heading = Account.objects.filter(organization=organization, is_postable=False).first()
        assert heading is not None
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        with pytest.raises(ValidationError) as error:
            add_account_line(
                invoice=invoice,
                account=heading,
                cost_center=delivery,
                description="نقل",
                quantity=Decimal("1.000"),
                unit_price=Decimal("1000.000000"),
            )
        assert error.value.code == "account_not_postable"

    def test_a_role_owned_account_is_refused(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        delivery: CostCenter,
        organization: Organization,
        inventory_mapped: None,
        mapped: None,
    ) -> None:
        """
        The dangerous one. `INVENTORY_CONTROL` is an ordinary postable asset
        account, so nothing about its class stops an operator selecting it —
        and a direct line billed into it would inflate stock value with no
        stock behind it and break `verify_inventory_against_gl` in a way no
        procurement screen would explain. An account a posting rule owns is
        not one a person entering an invoice gets to choose (ADR-019).
        """
        control = Account.objects.get(organization=organization, code="1-03-01-001")
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        with pytest.raises(ValidationError) as error:
            add_account_line(
                invoice=invoice,
                account=control,
                cost_center=delivery,
                description="محاولة",
                quantity=Decimal("1.000"),
                unit_price=Decimal("1000.000000"),
            )
        assert error.value.code == "account_is_role_owned"

    def test_grni_cannot_be_cleared_by_hand(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        delivery: CostCenter,
        organization: Organization,
        inventory_mapped: None,
        mapped: None,
    ) -> None:
        """
        Task 2.10 refuses to clear GRNI without a match. Selecting the GRNI
        account on a direct line would do it by typing instead.
        """
        grni = Account.objects.get(organization=organization, code="2-01-02-001")
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        with pytest.raises(ValidationError) as error:
            add_account_line(
                invoice=invoice,
                account=grni,
                cost_center=delivery,
                description="محاولة",
                quantity=Decimal("1.000"),
                unit_price=Decimal("1000.000000"),
            )
        assert error.value.code in {"account_class_not_billable", "account_is_role_owned"}

    def test_the_payable_itself_is_refused(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        delivery: CostCenter,
        payable_account: Account,
        mapped: None,
    ) -> None:
        """`Dr` supplier payable `Cr` supplier payable balances and means nothing."""
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        with pytest.raises(ValidationError) as error:
            add_account_line(
                invoice=invoice,
                account=payable_account,
                cost_center=delivery,
                description="محاولة",
                quantity=Decimal("1.000"),
                unit_price=Decimal("1000.000000"),
            )
        assert error.value.code in {"account_class_not_billable", "account_is_role_owned"}

    def test_a_revenue_account_is_refused(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        delivery: CostCenter,
        organization: Organization,
        accounting: None,
    ) -> None:
        """A supplier bills for an expense or an asset, not for our revenue."""
        revenue = Account.objects.filter(
            organization=organization, account_class="4", is_postable=True
        ).first()
        assert revenue is not None
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        with pytest.raises(ValidationError) as error:
            add_account_line(
                invoice=invoice,
                account=revenue,
                cost_center=delivery,
                description="محاولة",
                quantity=Decimal("1.000"),
                unit_price=Decimal("1000.000000"),
            )
        assert error.value.code == "account_class_not_billable"

    def test_an_unowned_expense_account_is_still_allowed(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        delivery: CostCenter,
        expense_account: Account,
        mapped: None,
    ) -> None:
        """
        The guard narrows the choice; it does not remove it. An ordinary
        expense account no posting rule owns is exactly what a delivery charge
        belongs to.
        """
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        line = add_account_line(
            invoice=invoice,
            account=expense_account,
            cost_center=delivery,
            description="أجور نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000.000000"),
        )
        assert line.account == expense_account

    def test_a_foreign_receipt_is_refused(
        self,
        grocery: Supplier,
        other_grocery: Supplier,
        branch: Branch,
        clerk: User,
        rice: InventoryItem,
        posted_receipt: GoodsReceipt,
    ) -> None:
        """A delivery from one supplier cannot be billed by another."""
        invoice = create_supplier_invoice(
            supplier=other_grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number="INV-X",
            invoice_date=INVOICED,
            business_date=INVOICED,
        )
        with pytest.raises(ValidationError) as error:
            add_inventory_line(
                invoice=invoice,
                item=rice,
                base_quantity=Decimal("10.000"),
                unit_price=Decimal("1400.000000"),
                receipt_line=posted_receipt.lines.first(),
            )
        assert error.value.code == "receipt_supplier_mismatch"

    def test_an_unposted_receipt_cannot_be_cited(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        clerk: User,
        keeper: User,
        rice: InventoryItem,
    ) -> None:
        draft = create_goods_receipt(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=INVOICED,
            delivery_reference="DN-DRAFT",
            evidence_reference="إشعار",
        )
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("5.000"),
            unit_price=Decimal("1400.000000"),
        )
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        with pytest.raises(ValidationError) as error:
            add_inventory_line(
                invoice=invoice,
                item=rice,
                base_quantity=Decimal("5.000"),
                unit_price=Decimal("1400.000000"),
                receipt_line=line,
            )
        assert error.value.code == "receipt_not_posted"


# ---------------------------------------------------------------------------
# Totals and allocation (PRC-039)
# ---------------------------------------------------------------------------


class TestTotalsAndAllocation:
    def test_the_document_total_is_the_sum_of_its_lines(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        expense_account: Account,
        delivery: CostCenter,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        for price in ("40000.000000", "60000.000000"):
            add_account_line(
                invoice=invoice,
                account=expense_account,
                cost_center=delivery,
                description="خدمة",
                quantity=Decimal("1.000"),
                unit_price=Decimal(price),
            )
        invoice.refresh_from_db()
        assert invoice.lines_total == Decimal("100000.000")
        assert invoice.total_amount == Decimal("100000.000")

    def test_freight_and_discount_allocate_across_the_lines(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        expense_account: Account,
        delivery: CostCenter,
    ) -> None:
        """
        Weighted by line value, so a delivery charge belongs proportionally to
        what was delivered. 10,000 over lines of 40,000 and 60,000 is 4,000 and
        6,000; the 3,000 discount is 1,200 and 1,800.
        """
        invoice = _invoice(
            grocery=grocery,
            branch=branch,
            clerk=clerk,
            freight_amount=Decimal("10000.000"),
            discount_amount=Decimal("3000.000"),
        )
        for price in ("40000.000000", "60000.000000"):
            add_account_line(
                invoice=invoice,
                account=expense_account,
                cost_center=delivery,
                description="خدمة",
                quantity=Decimal("1.000"),
                unit_price=Decimal(price),
            )
        invoice.refresh_from_db()
        lines = list(invoice.lines.order_by("sequence"))
        assert [line.allocated_freight for line in lines] == [
            Decimal("4000.000"),
            Decimal("6000.000"),
        ]
        assert [line.allocated_discount for line in lines] == [
            Decimal("1200.000"),
            Decimal("1800.000"),
        ]
        assert [line.net_amount for line in lines] == [
            Decimal("42800.000"),
            Decimal("64200.000"),
        ]
        assert invoice.total_amount == Decimal("107000.000")

    def test_an_indivisible_charge_leaves_no_residual(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        expense_account: Account,
        delivery: CostCenter,
    ) -> None:
        """
        1.000 across three equal lines. Naive thirds give 0.333 each and lose
        0.001; largest remainder hands the odd quantum to a deterministic line
        and the parts sum exactly to the whole.
        """
        invoice = _invoice(
            grocery=grocery, branch=branch, clerk=clerk, freight_amount=Decimal("1.000")
        )
        for _index in range(3):
            add_account_line(
                invoice=invoice,
                account=expense_account,
                cost_center=delivery,
                description="خدمة",
                quantity=Decimal("1.000"),
                unit_price=Decimal("100.000000"),
            )
        invoice.refresh_from_db()
        shares = [line.allocated_freight for line in invoice.lines.order_by("sequence")]
        assert sum(shares) == Decimal("1.000")
        assert invoice.total_amount == Decimal("301.000")
        assert invoice.total_amount == sum(line.net_amount for line in invoice.lines.all())

    def test_the_allocation_is_deterministic_across_runs(
        self,
        grocery: Supplier,
        other_grocery: Supplier,
        branch: Branch,
        clerk: User,
        expense_account: Account,
        delivery: CostCenter,
    ) -> None:
        """Same economic input, same answer — twice, on two documents."""
        shares = []
        for supplier, reference in ((grocery, "A"), (other_grocery, "B")):
            invoice = create_supplier_invoice(
                supplier=supplier,
                branch=branch,
                created_by=clerk,
                supplier_invoice_number=reference,
                invoice_date=INVOICED,
                business_date=INVOICED,
                freight_amount=Decimal("1.000"),
            )
            for _index in range(3):
                add_account_line(
                    invoice=invoice,
                    account=expense_account,
                    cost_center=delivery,
                    description="خدمة",
                    quantity=Decimal("1.000"),
                    unit_price=Decimal("100.000000"),
                )
            invoice.refresh_from_db()
            shares.append([line.allocated_freight for line in invoice.lines.order_by("sequence")])
        assert shares[0] == shares[1]

    def test_removing_a_line_re_allocates(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        expense_account: Account,
        delivery: CostCenter,
    ) -> None:
        invoice = _invoice(
            grocery=grocery, branch=branch, clerk=clerk, freight_amount=Decimal("10000.000")
        )
        first = add_account_line(
            invoice=invoice,
            account=expense_account,
            cost_center=delivery,
            description="خدمة",
            quantity=Decimal("1.000"),
            unit_price=Decimal("40000.000000"),
        )
        add_account_line(
            invoice=invoice,
            account=expense_account,
            cost_center=delivery,
            description="خدمة",
            quantity=Decimal("1.000"),
            unit_price=Decimal("60000.000000"),
        )
        remove_invoice_line(line=first)
        invoice.refresh_from_db()
        remaining = invoice.lines.get()
        assert remaining.allocated_freight == Decimal("10000.000")
        assert invoice.total_amount == Decimal("70000.000")

    def test_a_net_amount_that_is_not_its_own_arithmetic_is_refused(
        self, expense_invoice: SupplierInvoice
    ) -> None:
        """The trigger states the equation, so no path can write past it."""
        line = expense_invoice.lines.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierInvoiceLine.objects.filter(pk=line.pk).update(net_amount=Decimal("1.000"))


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_a_draft_can_be_edited_and_deleted(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        updated = update_supplier_invoice(invoice=invoice, supplier_reference="مرجع")
        assert updated.supplier_reference == "مرجع"
        delete_supplier_invoice(invoice=updated)
        assert not SupplierInvoice.objects.filter(pk=invoice.pk).exists()

    def test_an_empty_invoice_cannot_be_approved(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        with pytest.raises(ValidationError) as error:
            approve_supplier_invoice(invoice=invoice, actor=controller)
        assert error.value.code == "no_lines"

    def test_approval_freezes_the_terms(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        approved = approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        assert approved.status == SupplierInvoiceStatus.APPROVED
        with pytest.raises(ValidationError) as error:
            update_supplier_invoice(invoice=approved, supplier_reference="مرجع")
        assert error.value.code == "invoice_not_editable"

    def test_an_approved_invoice_takes_no_further_lines(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        expense_account: Account,
        delivery: CostCenter,
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        with pytest.raises(ValidationError) as error:
            add_account_line(
                invoice=expense_invoice,
                account=expense_account,
                cost_center=delivery,
                description="متأخر",
                quantity=Decimal("1.000"),
                unit_price=Decimal("1.000000"),
            )
        assert error.value.code == "invoice_not_editable"

    def test_a_stale_draft_instance_cannot_edit_an_approved_invoice(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        """
        The stale-instance rule. The caller's copy still says DRAFT; the row
        does not, and the row decides.
        """
        stale = SupplierInvoice.objects.get(pk=expense_invoice.pk)
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        assert stale.status == SupplierInvoiceStatus.DRAFT
        with pytest.raises(ValidationError) as error:
            update_supplier_invoice(invoice=stale, notes="محاولة")
        assert error.value.code == "invoice_not_editable"

    def test_an_approved_invoice_returns_to_draft_with_a_reason(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        approved = approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        returned = return_supplier_invoice_to_draft(
            invoice=approved, actor=controller, reason="المبلغ خاطئ"
        )
        assert returned.status == SupplierInvoiceStatus.DRAFT
        assert returned.approved_by is None
        assert returned.approved_at is None

    def test_returning_to_draft_needs_a_reason(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        approved = approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        with pytest.raises(ValidationError) as error:
            return_supplier_invoice_to_draft(invoice=approved, actor=controller, reason="  ")
        assert error.value.code == "reason_required"

    def test_a_draft_cannot_be_posted(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        with pytest.raises(ValidationError) as error:
            post_supplier_invoice(invoice=expense_invoice, actor=controller)
        assert error.value.code == "invoice_not_approved"

    def test_a_posted_invoice_cannot_be_edited_or_deleted(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierInvoice.objects.filter(pk=posted.pk).update(supplier_reference="X")
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierInvoice.objects.filter(pk=posted.pk).delete()

    def test_a_posted_line_is_frozen(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        post_supplier_invoice(invoice=expense_invoice, actor=controller)
        line = expense_invoice.lines.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierInvoiceLine.objects.filter(pk=line.pk).update(description="X")

    def test_a_draft_carries_no_ledger_metadata(self, expense_invoice: SupplierInvoice) -> None:
        from django.utils import timezone

        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierInvoice.objects.filter(pk=expense_invoice.pk).update(posted_at=timezone.now())


# ---------------------------------------------------------------------------
# Posting and the payable
# ---------------------------------------------------------------------------


class TestPostingAndThePayable:
    def test_posting_debits_the_charge_and_credits_the_payable(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        expense_account: Account,
        payable_account: Account,
        delivery: CostCenter,
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)

        assert posted.status == SupplierInvoiceStatus.POSTED
        assert posted.number.startswith("SINV-2026-")
        assert posted.posted_amount == Decimal("75000.000")

        journal = JournalEntry.objects.get()
        debit = journal.lines.get(debit__gt=0)
        credit = journal.lines.get(credit__gt=0)
        assert debit.account == expense_account
        assert debit.debit == Decimal("75000.000")
        assert credit.account == payable_account
        assert credit.credit == Decimal("75000.000")

    def test_posting_moves_no_stock_at_all(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        """
        PRC-038, counted rather than assumed. This is the reason the receipt and
        the invoice are two models.
        """
        movements_before = StockMovement.objects.count()
        located_before = StockLocationMovement.objects.count()

        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        post_supplier_invoice(invoice=expense_invoice, actor=controller)

        assert StockMovement.objects.count() == movements_before
        assert StockLocationMovement.objects.count() == located_before

    def test_two_lines_on_one_account_produce_one_debit(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        expense_account: Account,
        delivery: CostCenter,
        mapped: None,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        for price in ("40000.000000", "60000.000000"):
            add_account_line(
                invoice=invoice,
                account=expense_account,
                cost_center=delivery,
                description="خدمة",
                quantity=Decimal("1.000"),
                unit_price=Decimal(price),
            )
        approve_supplier_invoice(invoice=invoice, actor=controller)
        post_supplier_invoice(invoice=invoice, actor=controller)

        journal = JournalEntry.objects.get()
        assert journal.lines.filter(debit__gt=0).count() == 1
        assert journal.lines.get(debit__gt=0).debit == Decimal("100000.000")

    def test_two_accounts_produce_two_debits_and_one_credit(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        expense_account: Account,
        second_expense_account: Account,
        delivery: CostCenter,
        mapped: None,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        for account, price in (
            (expense_account, "40000.000000"),
            (second_expense_account, "60000.000000"),
        ):
            add_account_line(
                invoice=invoice,
                account=account,
                cost_center=delivery,
                description="خدمة",
                quantity=Decimal("1.000"),
                unit_price=Decimal(price),
            )
        approve_supplier_invoice(invoice=invoice, actor=controller)
        post_supplier_invoice(invoice=invoice, actor=controller)

        journal = JournalEntry.objects.get()
        assert journal.lines.filter(debit__gt=0).count() == 2
        assert journal.lines.filter(credit__gt=0).count() == 1
        assert journal.lines.get(credit__gt=0).credit == Decimal("100000.000")

    def test_a_missing_payable_mapping_rolls_everything_back(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        expense_account: Account,
        delivery: CostCenter,
    ) -> None:
        """The `mapped` fixture is deliberately absent here."""
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk)
        add_account_line(
            invoice=invoice,
            account=expense_account,
            cost_center=delivery,
            description="نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000.000000"),
        )
        approve_supplier_invoice(invoice=invoice, actor=controller)
        with pytest.raises(ValidationError):
            post_supplier_invoice(invoice=invoice, actor=controller)

        invoice.refresh_from_db()
        assert invoice.status == SupplierInvoiceStatus.APPROVED
        assert invoice.number == ""
        assert invoice.journal_entry is None
        assert JournalEntry.objects.count() == 0

    def test_a_journal_failure_leaves_the_invoice_approved(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apps.procurement import invoices as module

        def explode(**_kwargs: object) -> JournalEntry:
            raise RuntimeError("the ledger went away")

        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        monkeypatch.setattr(module, "post_entry", explode)
        with pytest.raises(RuntimeError):
            post_supplier_invoice(invoice=expense_invoice, actor=controller)

        expense_invoice.refresh_from_db()
        assert expense_invoice.status == SupplierInvoiceStatus.APPROVED
        assert expense_invoice.number == ""
        assert expense_invoice.posted_amount is None
        assert JournalEntry.objects.count() == 0

    def test_a_closed_period_refuses_the_posting(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        organization: Organization,
    ) -> None:
        from apps.accounting.models import AccountingPeriod, PeriodState

        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        period = AccountingPeriod.objects.get(
            fiscal_year__organization=organization, start_date__month=3
        )
        AccountingPeriod.objects.filter(pk=period.pk).update(state=PeriodState.CLOSED)
        with pytest.raises(ValidationError):
            post_supplier_invoice(invoice=expense_invoice, actor=controller)
        assert JournalEntry.objects.count() == 0

    def test_the_business_date_drives_the_period_not_the_invoice_date(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        expense_account: Account,
        delivery: CostCenter,
        organization: Organization,
        mapped: None,
    ) -> None:
        """
        The supplier's date is theirs; ours is the one the ledger answers to.
        Closing February must not block an invoice dated in February but booked
        in March.
        """
        from apps.accounting.models import AccountingPeriod, PeriodState

        invoice = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number="INV-FEB",
            invoice_date=datetime.date(TEST_YEAR, 2, 20),
            business_date=INVOICED,
        )
        add_account_line(
            invoice=invoice,
            account=expense_account,
            cost_center=delivery,
            description="نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000.000000"),
        )
        february = AccountingPeriod.objects.get(
            fiscal_year__organization=organization, start_date__month=2
        )
        AccountingPeriod.objects.filter(pk=february.pk).update(state=PeriodState.CLOSED)

        approve_supplier_invoice(invoice=invoice, actor=controller)
        posted = post_supplier_invoice(invoice=invoice, actor=controller)
        assert posted.journal_entry is not None
        assert posted.journal_entry.accounting_date == INVOICED

    def test_a_second_post_is_refused_and_duplicates_nothing(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        with pytest.raises(ValidationError) as error:
            post_supplier_invoice(invoice=posted, actor=controller)
        assert error.value.code == "already_posted"
        assert JournalEntry.objects.count() == 1

    def test_the_source_identity_is_complete_and_names_the_generation(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        """
        Complete, and pointing at the **posting generation** rather than the
        invoice as of Task 2.12.

        An invoice may legitimately reach the ledger twice — posted, reversed
        because its match was wrong, posted again from a corrected one — and
        ADR-017's source identity has to tell those entries apart. Keyed on the
        invoice, the second would look like a retry of the first and the kernel
        would refuse it.
        """
        from apps.procurement.models import SupplierInvoicePosting

        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        journal = JournalEntry.objects.get()
        posting = SupplierInvoicePosting.objects.get()
        assert journal.source_document_type == SOURCE_DOCUMENT_TYPE
        assert journal.source_document_id == str(posting.public_id)
        assert journal.source_document_id != str(posted.public_id)
        assert journal.source_event == "POSTED"
        assert posting.generation == 1

    def test_the_payable_is_derived_not_stored(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        grocery: Supplier,
    ) -> None:
        assert supplier_outstanding(grocery) == Decimal("0.000")
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        assert supplier_outstanding(grocery) == Decimal("75000.000")
        assert outstanding_amount(posted) == Decimal("75000.000")
        # There is no field to have got wrong.
        assert not hasattr(grocery, "balance")

    def test_open_payables_lists_the_posted_invoice(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        post_supplier_invoice(invoice=expense_invoice, actor=controller)
        rows = open_payables(controller)
        assert len(rows) == 1
        assert rows[0]["outstanding"] == Decimal("75000.000")

    def test_the_audit_event_records_the_posting(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        event = AuditEvent.objects.filter(
            action="POSTED", source_document_type=SOURCE_DOCUMENT_TYPE
        ).latest("id")
        assert event.metadata["number"] == posted.number
        assert event.metadata["posted_amount"] == "75000.000"


# ---------------------------------------------------------------------------
# The matching boundary
# ---------------------------------------------------------------------------


class TestTheMatchingBoundary:
    """
    What Task 2.10 refuses to do, and why.

    Task 2.0 §9 posts the **matched receipt value** to GRNI and the difference
    to purchase price variance. Both come from a Task 2.11 match allocation, so
    a goods line has no determinate accounting here. Posting the invoiced
    amount to GRNI instead would balance and be wrong — it would clear a
    variance nobody computed and leave Task 2.12 nothing to recognise.

    These assertions are the boundary marker. Task 2.11/2.12 replace them with
    their positive twins; deleting them is the deliberate act of crossing.
    """

    def test_a_goods_invoice_approves_and_then_refuses_to_post(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        posted_receipt: GoodsReceipt,
        mapped: None,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-GOODS")
        add_inventory_line(
            invoice=invoice,
            item=rice,
            base_quantity=Decimal("50.000"),
            unit_price=Decimal("1450.000000"),
            receipt_line=posted_receipt.lines.first(),
        )
        approved = approve_supplier_invoice(invoice=invoice, actor=controller)
        assert approved.status == SupplierInvoiceStatus.APPROVED
        assert approved.is_ready_to_post is False
        assert [line.sequence for line in approved.blocking_lines] == [1]

        with pytest.raises(ValidationError) as error:
            post_supplier_invoice(invoice=approved, actor=controller)
        assert error.value.code == "invoice_awaiting_matching"

        approved.refresh_from_db()
        assert approved.status == SupplierInvoiceStatus.APPROVED
        assert approved.journal_entry is None

    def test_one_goods_line_blocks_the_whole_invoice(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        expense_account: Account,
        delivery: CostCenter,
        mapped: None,
    ) -> None:
        """
        Half-posting would create a payable for part of what is owed, which is
        a worse answer than creating none.
        """
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-MIXED")
        add_account_line(
            invoice=invoice,
            account=expense_account,
            cost_center=delivery,
            description="نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("5000.000000"),
        )
        add_inventory_line(
            invoice=invoice,
            item=rice,
            base_quantity=Decimal("10.000"),
            unit_price=Decimal("1400.000000"),
        )
        approve_supplier_invoice(invoice=invoice, actor=controller)
        with pytest.raises(ValidationError) as error:
            post_supplier_invoice(invoice=invoice, actor=controller)
        assert error.value.code == "invoice_awaiting_matching"
        assert JournalEntry.objects.count() == 0

    def test_no_match_allocation_is_created_by_referencing_a_receipt(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        rice: InventoryItem,
        posted_receipt: GoodsReceipt,
    ) -> None:
        """
        A reference is evidence, not an allocation. Naming a delivery must not
        consume its matchable remainder or mark anything matched (PRC-040 –
        PRC-042).
        """
        receipt_line = posted_receipt.lines.get()
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-REF")
        add_inventory_line(
            invoice=invoice,
            item=rice,
            base_quantity=Decimal("50.000"),
            unit_price=Decimal("1450.000000"),
            receipt_line=receipt_line,
        )
        receipt_line.refresh_from_db()
        # Nothing about the receipt line changed, and nothing claims it did.
        assert receipt_line.accepted_base_quantity == Decimal("50.000")
        assert receipt_line.posted_value == Decimal("70000.000")

    def test_matching_lives_outside_invoice_posting(self) -> None:
        """
        The positive twin of this task's boundary marker.

        Until Task 2.11 this asserted that no matching model existed at all.
        Task 2.11 delivered the models and the claim narrowed to "matching is a
        separate module that invoice posting cannot reach". Task 2.12 posts
        *from* a match, so posting now imports one function from it — and the
        claim narrows again to the part that was always the real one:

        **the dependency runs one way.** Posting reads matching's agreed
        answer; matching never resolves an account, never opens a journal, and
        never defines a posting service. A cycle in that direction is what a
        matching workspace that quietly posts money looks like.
        """
        from pathlib import Path

        from django.apps import apps as django_apps

        from apps.procurement import invoices as invoice_module
        from apps.procurement import matching as matching_module

        names = {model.__name__ for model in django_apps.get_app_config("procurement").get_models()}
        assert "PurchaseMatch" in names
        assert "PurchaseMatchAllocation" in names

        assert invoice_module.__file__ is not None
        source = Path(invoice_module.__file__).read_text(encoding="utf-8")
        assert "from apps.procurement.matching import" in source
        assert "def add_allocation" not in source
        assert "def mark_match_ready" not in source
        assert "def create_purchase_match" not in source

        assert matching_module.__file__ is not None
        matching_source = Path(matching_module.__file__).read_text(encoding="utf-8")
        assert "post_entry" not in matching_source
        assert "def post_supplier_invoice" not in matching_source

    def test_the_variance_role_is_seeded_and_is_a_clearing_role(self) -> None:
        """
        Task 2.12 seeded it, because Task 2.12 is what posts to it.

        Task 2.0 §15 proposed the cost-of-sales code `5-02-01-001` and that is
        superseded (ADR-022, amended here). Class 5 would have demanded a cost
        centre a supplier invoice has nowhere to get, and ADR-022 separately
        rejects booking a purchasing outcome as cost of sales. The difference
        is parked in a clearing account until a later, separately specified
        period-end process splits it between stock still on hand and what has
        been consumed.
        """
        role = AccountRole.objects.get(code="PURCHASE_PRICE_VARIANCE")
        assert role.domain == "PURCHASING"
        assert role.mapping_scope == "ORGANIZATION"
        assert role.is_system

    def test_the_payment_roles_arrived_with_the_task_that_posts_to_them(self) -> None:
        """
        The positive twin of Step 12's boundary marker: the 2.15 vocabulary
        waited for Task 2.15, and Task 2.15 seeded all three because its
        journal posts to all three.
        """
        assert (
            AccountRole.objects.filter(
                code__in=["SUPPLIER_ADVANCE", "SUPPLIER_PAYMENT_CASH", "SUPPLIER_PAYMENT_BANK"],
                domain="PURCHASING",
                is_system=True,
            ).count()
            == 3
        )

    def test_the_grni_balance_is_untouched_by_an_invoice(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        expense_account: Account,
        delivery: CostCenter,
        posted_receipt: GoodsReceipt,
        organization: Organization,
        mapped: None,
    ) -> None:
        """
        The receipt parked 70,000 in GRNI. An expense invoice posting beside it
        must not touch that figure — clearing GRNI is Task 2.12's act.
        """
        from django.db.models import Sum

        grni = Account.objects.get(organization=organization, code="2-01-02-001")
        before = JournalEntry.objects.filter(lines__account=grni).aggregate(
            total=Sum("lines__credit")
        )["total"]

        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-BESIDE")
        add_account_line(
            invoice=invoice,
            account=expense_account,
            cost_center=delivery,
            description="نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("5000.000000"),
        )
        approve_supplier_invoice(invoice=invoice, actor=controller)
        post_supplier_invoice(invoice=invoice, actor=controller)

        after = JournalEntry.objects.filter(lines__account=grni).aggregate(
            total=Sum("lines__credit")
        )["total"]
        assert after == before == Decimal("70000.000")


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


class TestReversal:
    def test_a_reversal_mirrors_exactly(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        grocery: Supplier,
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        reversed_invoice = reverse_supplier_invoice(
            invoice=posted, actor=controller, reason="فوترة مكررة"
        )

        assert reversed_invoice.status == SupplierInvoiceStatus.REVERSED
        assert reversed_invoice.reversal_journal_entry is not None
        debits = sum(row.debit for entry in JournalEntry.objects.all() for row in entry.lines.all())
        credits = sum(
            row.credit for entry in JournalEntry.objects.all() for row in entry.lines.all()
        )
        assert debits == credits == Decimal("150000.000")
        # The payable falls to nothing as a consequence, not a correction.
        assert supplier_outstanding(grocery) == Decimal("0.000")

    def test_a_reversal_needs_a_reason(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        with pytest.raises(ValidationError) as error:
            reverse_supplier_invoice(invoice=posted, actor=controller, reason="   ")
        assert error.value.code == "reason_required"

    def test_a_draft_cannot_be_reversed(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        with pytest.raises(ValidationError) as error:
            reverse_supplier_invoice(invoice=expense_invoice, actor=controller, reason="لا شيء")
        assert error.value.code == "invoice_not_posted"

    def test_a_reversal_cannot_be_reversed(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        once = reverse_supplier_invoice(invoice=posted, actor=controller, reason="خطأ")
        with pytest.raises(ValidationError) as error:
            reverse_supplier_invoice(invoice=once, actor=controller, reason="مرة أخرى")
        assert error.value.code == "already_reversed"

    def test_a_reversed_invoice_frees_its_supplier_reference(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
    ) -> None:
        """
        The uniqueness index excludes reversed rows, because a reversed invoice
        is corrected by re-entering the same supplier reference.
        """
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        reverse_supplier_invoice(invoice=posted, actor=controller, reason="خطأ")
        replacement = _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-001")
        assert replacement.pk != posted.pk

    def test_a_reversal_moves_no_stock(
        self, expense_invoice: SupplierInvoice, controller: User
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        before = StockMovement.objects.count()
        reverse_supplier_invoice(invoice=posted, actor=controller, reason="خطأ")
        assert StockMovement.objects.count() == before


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_a_posted_invoice_reconciles(
        self, expense_invoice: SupplierInvoice, controller: User, organization: Organization
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        assert verify_supplier_invoice(posted) == []
        assert verify_invoice_charges(organization) == []
        assert verify_supplier_payables(organization) == []
        assert verify_procurement(organization) == []

    def test_a_draft_is_skipped_rather_than_reported_clean(
        self, expense_invoice: SupplierInvoice
    ) -> None:
        assert verify_supplier_invoice(expense_invoice) == []

    def test_a_reversed_invoice_still_reconciles(
        self, expense_invoice: SupplierInvoice, controller: User, organization: Organization
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        reversed_invoice = reverse_supplier_invoice(invoice=posted, actor=controller, reason="خطأ")
        assert verify_supplier_invoice(reversed_invoice) == []
        assert verify_procurement(organization) == []

    def test_a_planted_journal_stays_visible(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        organization: Organization,
        branch: Branch,
        expense_account: Account,
        payable_account: Account,
        delivery: CostCenter,
    ) -> None:
        """No repair mode. It is reported and left exactly where it is."""
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine

        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        post_supplier_invoice(invoice=expense_invoice, actor=controller)
        post_entry(
            organization=organization,
            accounting_date=INVOICED,
            lines=[
                PostingLine(
                    account=expense_account,
                    branch=branch,
                    cost_center=delivery,
                    debit=Decimal("1.000"),
                ),
                PostingLine(account=payable_account, branch=branch, credit=Decimal("1.000")),
            ],
            idempotency_key="planted-invoice-entry",
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id="00000000-0000-0000-0000-000000000000",
            source_event="POSTED",
        )
        problems = verify_supplier_payables(organization)
        assert len(problems) == 1
        assert problems[0].field == "journal_cites_unknown_invoice"
        assert JournalEntry.objects.count() == 2


# ---------------------------------------------------------------------------
# Permissions, scope and screens
# ---------------------------------------------------------------------------


class TestScopeAndPermissions:
    def test_the_role_map_separates_entry_from_approval(self) -> None:
        accountant = permissions_for_role(Role.ACCOUNTANT)
        manager = permissions_for_role(Role.ACCOUNTING_MANAGER)
        assert CREATE_SUPPLIER_INVOICE in accountant
        assert APPROVE_SUPPLIER_INVOICE not in accountant
        assert POST_SUPPLIER_INVOICE not in accountant
        assert APPROVE_SUPPLIER_INVOICE in manager
        assert POST_SUPPLIER_INVOICE in manager
        assert CREATE_SUPPLIER_INVOICE not in manager

    def test_purchasing_reads_invoices_and_agrees_to_none(self) -> None:
        purchasing = permissions_for_role(Role.PURCHASING)
        assert VIEW_SUPPLIER_INVOICE in purchasing
        assert APPROVE_SUPPLIER_INVOICE not in purchasing
        assert POST_SUPPLIER_INVOICE not in purchasing

    def test_a_storekeeper_holds_no_invoice_permission(self) -> None:
        keeper_permissions = permissions_for_role(Role.STOREKEEPER)
        assert VIEW_SUPPLIER_INVOICE not in keeper_permissions
        assert CREATE_SUPPLIER_INVOICE not in keeper_permissions

    def test_a_branch_membership_does_not_reach_an_invoice(
        self, expense_invoice: SupplierInvoice, keeper: User
    ) -> None:
        """
        The scope, doing its job. A storekeeper reaches the organization
        through a branch, and an invoice is still not theirs to see.
        """
        assert visible_supplier_invoices(keeper).count() == 0
        with pytest.raises(OutOfScope):
            resolve_supplier_invoice(keeper, expense_invoice.pk)

    def test_another_organizations_invoice_is_a_404(
        self, expense_invoice: SupplierInvoice, other_organization: Organization
    ) -> None:
        outsider = User.objects.create_user(username="outsider", password=PASSWORD)
        grant_organization_access(
            user=outsider, organization=other_organization, role=Role.ACCOUNTING_MANAGER
        )
        with pytest.raises(OutOfScope):
            resolve_supplier_invoice(User.objects.get(pk=outsider.pk), expense_invoice.pk)

    def test_the_list_renders_for_an_authorized_user(
        self, expense_invoice: SupplierInvoice, controller: User, client: Client
    ) -> None:
        client.force_login(controller)
        response = client.get(reverse("procurement:supplier_invoice_list"))
        assert response.status_code == 200
        assert expense_invoice.supplier_invoice_number in response.content.decode()

    def test_an_htmx_request_returns_only_the_fragment(
        self, expense_invoice: SupplierInvoice, controller: User, client: Client
    ) -> None:
        client.force_login(controller)
        response = client.get(reverse("procurement:supplier_invoice_list"), headers=HX)
        assert response.status_code == 200
        assert "<html" not in response.content.decode().lower()

    def test_the_status_filter_narrows_the_list(
        self, expense_invoice: SupplierInvoice, controller: User, client: Client
    ) -> None:
        client.force_login(controller)
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        post_supplier_invoice(invoice=expense_invoice, actor=controller)
        response = client.get(
            reverse("procurement:supplier_invoice_list"),
            {"status": "DRAFT"},
            headers=HX,
        )
        body = response.content.decode()
        assert expense_invoice.supplier_invoice_number not in body

    def test_the_workspace_filters_reference_supplier_branch_and_matching(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        client: Client,
    ) -> None:
        client.force_login(controller)
        response = client.get(
            reverse("procurement:supplier_invoice_list"),
            {
                "supplier": str(expense_invoice.supplier_id),
                "branch": str(expense_invoice.branch_id),
                "reference": "does-not-match",
                "matching": "DIRECT",
            },
            headers=HX,
        )
        assert response.status_code == 200
        assert expense_invoice.supplier_invoice_number not in response.content.decode()

    def test_a_draft_header_has_an_htmx_edit_path(
        self,
        expense_invoice: SupplierInvoice,
        clerk: User,
        client: Client,
    ) -> None:
        client.force_login(clerk)
        url = reverse("procurement:supplier_invoice_update", args=[expense_invoice.pk])
        response = client.post(
            url,
            {
                "supplier": expense_invoice.supplier_id,
                "branch": expense_invoice.branch_id,
                "supplier_invoice_number": "INV-EDITED",
                "invoice_date": "2026-03-11",
                "business_date": "2026-03-11",
                "supplier_reference": "EVIDENCE-7",
                "currency_code": "IQD",
                "freight_amount": "0",
                "discount_amount": "0",
                "notes": "صححت المسودة",
            },
            headers=HX,
        )
        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == reverse(
            "procurement:supplier_invoice_detail", args=[expense_invoice.pk]
        )
        expense_invoice.refresh_from_db()
        assert expense_invoice.supplier_invoice_number == "INV-EDITED"
        assert expense_invoice.supplier_reference == "EVIDENCE-7"
        assert expense_invoice.currency_code == "IQD"

    def test_an_htmx_transition_swaps_the_detail_not_a_whole_document(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        client: Client,
    ) -> None:
        client.force_login(controller)
        response = client.post(
            reverse("procurement:supplier_invoice_approve", args=[expense_invoice.pk]),
            headers=HX,
        )
        body = response.content.decode().lower()
        assert response.status_code == 200
        assert '<section class="workspace-page" id="supplier-invoice-detail">' in body
        assert "<html" not in body

    def test_cost_is_absent_from_restricted_html(
        self,
        expense_invoice: SupplierInvoice,
        buyer: User,
        client: Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_has_perm = User.has_perm
        monkeypatch.setattr(
            User,
            "has_perm",
            lambda user, permission, obj=None: (
                False
                if user.username == buyer.username
                and permission == "procurement.view_supplier_cost"
                else original_has_perm(user, permission, obj)
            ),
        )
        client.force_login(buyer)
        response = client.get(
            reverse("procurement:supplier_invoice_detail", args=[expense_invoice.pk])
        )
        body = response.content.decode()
        assert response.status_code == 200
        assert "75000.000" not in body
        assert "صافي الفاتورة" not in body

    def test_the_detail_screen_shows_the_matching_boundary(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        client: Client,
        mapped: None,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-UI")
        add_inventory_line(
            invoice=invoice,
            item=rice,
            base_quantity=Decimal("10.000"),
            unit_price=Decimal("1400.000000"),
        )
        approve_supplier_invoice(invoice=invoice, actor=controller)
        client.force_login(controller)
        response = client.get(reverse("procurement:supplier_invoice_detail", args=[invoice.pk]))
        assert response.status_code == 200
        assert "ما يمنع الترحيل" in response.content.decode()

    def test_an_accountant_cannot_approve_through_the_route(
        self, expense_invoice: SupplierInvoice, clerk: User, client: Client
    ) -> None:
        """Hidden button plus a direct POST is still 403."""
        client.force_login(clerk)
        response = client.post(
            reverse("procurement:supplier_invoice_approve", args=[expense_invoice.pk])
        )
        assert response.status_code == 403
        expense_invoice.refresh_from_db()
        assert expense_invoice.status == SupplierInvoiceStatus.DRAFT

    def test_an_accountant_cannot_post_through_the_route(
        self, expense_invoice: SupplierInvoice, clerk: User, controller: User, client: Client
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        client.force_login(clerk)
        response = client.post(
            reverse("procurement:supplier_invoice_post", args=[expense_invoice.pk])
        )
        assert response.status_code == 403
        assert JournalEntry.objects.count() == 0

    def test_a_controller_cannot_create_through_the_route(
        self, controller: User, client: Client, organization: Organization
    ) -> None:
        """Whoever agrees a claim is real does not also type it in."""
        client.force_login(controller)
        response = client.get(reverse("procurement:supplier_invoice_create"))
        assert response.status_code == 403

    def test_the_post_route_posts_for_a_controller(
        self, expense_invoice: SupplierInvoice, controller: User, client: Client
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        client.force_login(controller)
        response = client.post(
            reverse("procurement:supplier_invoice_post", args=[expense_invoice.pk])
        )
        assert response.status_code == 302
        expense_invoice.refresh_from_db()
        assert expense_invoice.status == SupplierInvoiceStatus.POSTED

    def test_a_posted_detail_shows_the_journal_lines(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        client: Client,
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        posted = post_supplier_invoice(invoice=expense_invoice, actor=controller)
        client.force_login(controller)
        response = client.get(reverse("procurement:supplier_invoice_detail", args=[posted.pk]))
        body = response.content.decode()
        assert response.status_code == 200
        assert posted.journal_entry is not None
        assert posted.journal_entry.entry_number in body
        assert "5-01-02-003" in body
        assert "2-01-01-001" in body

    def test_the_direct_account_selector_omits_role_owned_accounts(
        self,
        expense_invoice: SupplierInvoice,
        clerk: User,
        client: Client,
    ) -> None:
        client.force_login(clerk)
        response = client.get(
            reverse("procurement:supplier_invoice_detail", args=[expense_invoice.pk])
        )
        body = response.content.decode()
        assert response.status_code == 200
        assert "5-01-02-003" in body
        assert "2-01-01-001 — ذمم الموردين" not in body

    def test_a_get_does_not_post(
        self, expense_invoice: SupplierInvoice, controller: User, client: Client
    ) -> None:
        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        client.force_login(controller)
        response = client.get(
            reverse("procurement:supplier_invoice_post", args=[expense_invoice.pk])
        )
        assert response.status_code == 405
        assert JournalEntry.objects.count() == 0


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class TestApi:
    def test_money_crosses_the_wire_as_strings(
        self, expense_invoice: SupplierInvoice, controller: User, client: Client
    ) -> None:
        client.force_login(controller)
        response = client.get(f"/api/v1/procurement/supplier-invoices/{expense_invoice.pk}/")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total_amount"] == "75000.000"
        assert isinstance(payload["lines"][0]["net_amount"], str)

    def test_no_float_appears_in_the_raw_json(
        self, expense_invoice: SupplierInvoice, controller: User, client: Client
    ) -> None:
        client.force_login(controller)
        raw = client.get(
            f"/api/v1/procurement/supplier-invoices/{expense_invoice.pk}/"
        ).content.decode()
        assert '"total_amount": "75000.000"' in raw or '"total_amount":"75000.000"' in raw
        assert "75000.0," not in raw

    def test_the_command_endpoints_drive_the_lifecycle(
        self, expense_invoice: SupplierInvoice, controller: User, client: Client
    ) -> None:
        client.force_login(controller)
        base = f"/api/v1/procurement/supplier-invoices/{expense_invoice.pk}"
        assert client.post(f"{base}/approve/").status_code == 200
        posted = client.post(f"{base}/post/")
        assert posted.status_code == 200
        assert posted.json()["status"] == "POSTED"
        reversed_response = client.post(
            f"{base}/reverse/",
            data={"reason": "فوترة مكررة"},
            content_type="application/json",
        )
        assert reversed_response.status_code == 200
        assert reversed_response.json()["status"] == "REVERSED"

    def test_patch_updates_only_a_draft_header(
        self,
        expense_invoice: SupplierInvoice,
        clerk: User,
        controller: User,
        client: Client,
    ) -> None:
        client.force_login(clerk)
        url = f"/api/v1/procurement/supplier-invoices/{expense_invoice.pk}/"
        changed = client.patch(
            url,
            data={"supplier_reference": "API-EVIDENCE", "currency_code": "IQD"},
            content_type="application/json",
        )
        assert changed.status_code == 200
        assert changed.json()["supplier_reference"] == "API-EVIDENCE"

        approve_supplier_invoice(invoice=expense_invoice, actor=controller)
        refused = client.patch(
            url,
            data={"supplier_reference": "MOVED"},
            content_type="application/json",
        )
        assert refused.status_code in {400, 409, 422}
        expense_invoice.refresh_from_db()
        assert expense_invoice.supplier_reference == "API-EVIDENCE"

    def test_cost_fields_are_omitted_from_the_restricted_api(
        self,
        expense_invoice: SupplierInvoice,
        buyer: User,
        client: Client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_has_perm = User.has_perm
        monkeypatch.setattr(
            User,
            "has_perm",
            lambda user, permission, obj=None: (
                False
                if user.username == buyer.username
                and permission == "procurement.view_supplier_cost"
                else original_has_perm(user, permission, obj)
            ),
        )
        client.force_login(buyer)
        response = client.get(f"/api/v1/procurement/supplier-invoices/{expense_invoice.pk}/")
        assert response.status_code == 200
        payload = response.json()
        assert "total_amount" not in payload
        assert "unit_price" not in payload["lines"][0]

    def test_the_api_exposes_currency_evidence_and_matching_state(
        self,
        expense_invoice: SupplierInvoice,
        controller: User,
        client: Client,
    ) -> None:
        client.force_login(controller)
        payload = client.get(f"/api/v1/procurement/supplier-invoices/{expense_invoice.pk}/").json()
        assert payload["currency_code"] == "IQD"
        assert payload["matching_status"] == "DIRECT"
        assert payload["created_by"] == expense_invoice.created_by.username

    def test_a_foreign_invoice_is_a_404_over_the_api(
        self, expense_invoice: SupplierInvoice, other_organization: Organization, client: Client
    ) -> None:
        outsider = User.objects.create_user(username="api-outsider", password=PASSWORD)
        grant_organization_access(
            user=outsider, organization=other_organization, role=Role.ACCOUNTING_MANAGER
        )
        client.force_login(User.objects.get(pk=outsider.pk))
        response = client.get(f"/api/v1/procurement/supplier-invoices/{expense_invoice.pk}/")
        assert response.status_code == 404

    def test_the_blocking_lines_are_reported(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        client: Client,
        mapped: None,
    ) -> None:
        invoice = _invoice(grocery=grocery, branch=branch, clerk=clerk, reference="INV-API")
        add_inventory_line(
            invoice=invoice,
            item=rice,
            base_quantity=Decimal("10.000"),
            unit_price=Decimal("1400.000000"),
        )
        approve_supplier_invoice(invoice=invoice, actor=controller)
        client.force_login(controller)
        payload = client.get(f"/api/v1/procurement/supplier-invoices/{invoice.pk}/").json()
        assert payload["is_ready_to_post"] is False
        assert payload["blocking_line_sequences"] == [1]


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


class TestDemoInvoices:
    def test_the_seed_is_idempotent_and_skips_what_is_missing(
        self, organization: Organization, clerk: User, controller: User
    ) -> None:
        """
        No inventory demo has run against this organization, so the seed finds
        no supplier and no chart and returns nothing rather than inventing one.
        """
        from apps.procurement.demo import seed_demo_invoices

        assert (
            seed_demo_invoices(organization=organization, recorder=clerk, approver=controller) == []
        )
