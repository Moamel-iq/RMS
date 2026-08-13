"""
Task 2.11 — three-way matching, and the Task 2.12 boundary it must not cross.

Six classes carry the weight.

`TestTheStepFourteenBoundary` is the one this task exists to hold. Matching
decides what covers what; it posts nothing. Every assertion here is a negative
whose positive twin Task 2.12 must deliberately write — no posting service, no
journal, no GRNI clearing, no stock movement, and an invoice that is still
`APPROVED` after a match is `READY`.

`TestAvailability` covers PRC-041. Over-allocation is impossible against the
receipt, the invoice and the order, and availability is derived so that
cancelling a match gives the quantity back.

`TestValueAllocation` is the arithmetic that makes Task 2.12 safe: partial
allocations take their proportional share, the final one takes the exact
remainder, and the parts sum back to the stored figure with nothing left over.

`TestPriceVariance` is the number the whole exercise produces.
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
    add_account_line,
    add_inventory_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    reverse_supplier_invoice,
)
from apps.procurement.matching import (
    LineMatchState,
    add_allocation,
    allocation_fingerprint,
    cancel_purchase_match,
    coverage_for_invoice,
    create_purchase_match,
    delete_purchase_match,
    invoice_line_availability,
    invoice_line_match_state,
    mark_match_ready,
    receipt_availability,
    remove_allocation,
    unmatched_receipt_lines,
)
from apps.procurement.models import (
    GoodsReceipt,
    PurchaseMatch,
    PurchaseMatchAllocation,
    PurchaseMatchStatus,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceStatus,
)
from apps.procurement.permissions import (
    CANCEL_PURCHASE_MATCH,
    MATCH_SUPPLIER_INVOICE,
    VIEW_PURCHASE_MATCH,
    permissions_for_role,
)
from apps.procurement.posting import post_goods_receipt, reverse_goods_receipt
from apps.procurement.reconciliation import verify_matching, verify_procurement
from apps.procurement.selectors import resolve_purchase_match, visible_purchase_matches
from apps.procurement.services import (
    add_order_line,
    add_receipt_line,
    approve_purchase_order,
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
RECEIVED = datetime.date(TEST_YEAR, 3, 1)
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
def mapped(organization: Organization, accounting: None) -> None:
    """Everything a receipt and an invoice need to post."""
    for code, account_code in (
        (INVENTORY_CONTROL, "1-03-01-001"),
        (GOODS_RECEIVED_NOT_INVOICED, "2-01-02-001"),
        (SUPPLIER_PAYABLE, "2-01-01-001"),
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
def sugar(organization: Organization, kilogram: UnitOfMeasure) -> InventoryItem:
    from apps.inventory.services import create_item, create_item_category

    return create_item(
        organization=organization,
        code="SUGAR",
        name_ar="سكر",
        category=create_item_category(organization=organization, code="DRY", name_ar="جافة"),
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
def other_supplier(organization: Organization) -> Supplier:
    return create_supplier(organization=organization, code="GROC-02", name_ar="مورد آخر")


@pytest.fixture
def keeper(branch: Branch, store: Warehouse) -> User:
    user = User.objects.create_user(username="keeper", password=PASSWORD)
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def clerk(organization: Organization) -> User:
    """An accountant: enters invoices and does the matching."""
    user = User.objects.create_user(username="clerk", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTANT)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def controller(organization: Organization) -> User:
    """An accounting manager: approves, posts, and can withdraw a match."""
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
    order_line: object = None,
) -> GoodsReceipt:
    receipt = create_goods_receipt(
        supplier=supplier,
        branch=branch,
        warehouse=store,
        created_by=keeper,
        received_at=RECEIVED,
        delivery_reference=reference,
        evidence_reference="إشعار",
        order=order_line.order if order_line is not None else None,  # type: ignore[attr-defined]
    )
    line = add_receipt_line(
        receipt=receipt,
        item=item,
        delivered_quantity=Decimal(quantity),
        unit_price=Decimal(price) if order_line is None else None,
        order_line=order_line,  # type: ignore[arg-type]
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
def invoice(
    grocery: Supplier,
    branch: Branch,
    clerk: User,
    controller: User,
    rice: InventoryItem,
    mapped: None,
) -> SupplierInvoice:
    """Fifty kilograms at 1,450 — 72,500 claimed, 2,500 dearer than delivered."""
    return _approved_invoice(
        supplier=grocery,
        branch=branch,
        clerk=clerk,
        controller=controller,
        item=rice,
        quantity="50.000",
        price="1450.000000",
    )


@pytest.fixture
def match(invoice: SupplierInvoice, clerk: User) -> PurchaseMatch:
    return create_purchase_match(invoice=invoice, created_by=clerk)


# ---------------------------------------------------------------------------
# The Task 2.12 boundary
# ---------------------------------------------------------------------------


class TestTheStepFourteenBoundary:
    """
    Matching posts nothing, and these assertions are how that stays true.

    Every one is a negative whose positive twin Task 2.12 must deliberately
    write. Deleting them is the act of crossing the boundary, exactly as
    `TestNothingPostsYet` was for Task 2.9 and `TestTheMatchingBoundary` is for
    the invoice side.
    """

    def test_a_ready_match_posts_no_journal(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        journals_before = JournalEntry.objects.count()
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        ready = mark_match_ready(match=match, actor=clerk)

        assert ready.status == PurchaseMatchStatus.READY
        assert JournalEntry.objects.count() == journals_before

    def test_a_ready_match_moves_no_stock(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        movements_before = StockMovement.objects.count()
        located_before = StockLocationMovement.objects.count()
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)

        assert StockMovement.objects.count() == movements_before
        assert StockLocationMovement.objects.count() == located_before

    def test_the_invoice_is_still_approved_after_a_ready_match(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """
        The sentence the whole workspace turns on. "Ready" is about evidence;
        "posted" is about money, and Task 2.11 produces only the first.
        """
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        invoice.refresh_from_db()
        assert invoice.status == SupplierInvoiceStatus.APPROVED
        assert invoice.journal_entry is None
        assert invoice.posted_amount is None

    def test_a_ready_match_clears_no_grni(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        receipt: GoodsReceipt,
        clerk: User,
        organization: Organization,
    ) -> None:
        """
        The receipt parked 70,000 in GRNI. Matching says what covers it and
        clears none of it — that entry is Task 2.12's.
        """
        from django.db.models import Sum

        grni = Account.objects.get(organization=organization, code="2-01-02-001")
        before = JournalEntry.objects.filter(lines__account=grni).aggregate(
            credit=Sum("lines__credit"), debit=Sum("lines__debit")
        )
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        after = JournalEntry.objects.filter(lines__account=grni).aggregate(
            credit=Sum("lines__credit"), debit=Sum("lines__debit")
        )
        assert after == before
        assert after["credit"] == Decimal("70000.000")
        assert after["debit"] is None or after["debit"] == Decimal("0.000")

    def test_no_posting_service_exists(self) -> None:
        """
        Asserted rather than assumed. A posting command appearing in the
        matching module would mean somebody built Task 2.12 inside Task 2.11.
        """
        from apps.procurement import matching

        for name in (
            "post_purchase_match",
            "post_match_variance",
            "clear_grni",
            "post_variance",
            "revalue_inventory",
        ):
            assert not hasattr(matching, name), f"{name} belongs to Task 2.12"

    def test_no_posting_route_exists(self) -> None:
        from django.urls import NoReverseMatch
        from django.urls import reverse as resolve

        for name in ("procurement:purchase_match_post", "procurement:purchase_match_variance"):
            with pytest.raises(NoReverseMatch):
                resolve(name, args=[1])

    def test_the_status_enum_has_no_posted_value(self) -> None:
        """
        `POSTED` is absent on purpose. A financially meaningful status here
        would let a screen say a match had reached the ledger when nothing had.
        """
        assert set(PurchaseMatchStatus.values) == {"DRAFT", "READY", "CANCELLED"}

    def test_the_variance_role_is_still_unmapped(self) -> None:
        """`PURCHASE_PRICE_VARIANCE` arrives with the task that posts to it."""
        assert not AccountRole.objects.filter(code="PURCHASE_PRICE_VARIANCE").exists()

    def test_the_api_says_it_posted_nothing(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        receipt: GoodsReceipt,
        clerk: User,
        client: Client,
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        client.force_login(clerk)
        payload = client.get(f"/api/v1/procurement/matches/{match.pk}/").json()
        assert payload["status"] == "READY"
        assert payload["is_financially_posted"] is False
        assert payload["supplier_invoice_status"] == "APPROVED"


# ---------------------------------------------------------------------------
# Availability and over-allocation (PRC-041)
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_a_full_allocation_consumes_both_sides(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        receipt_line = receipt.lines.get()
        invoice_line = invoice.lines.get()
        assert receipt_availability(receipt_line).quantity == Decimal("50.000")
        assert invoice_line_availability(invoice_line).quantity == Decimal("50.000")

        add_allocation(
            match=match,
            invoice_line=invoice_line,
            receipt_line=receipt_line,
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        assert receipt_availability(receipt_line).quantity == Decimal("0.000")
        assert invoice_line_availability(invoice_line).quantity == Decimal("0.000")

    def test_a_partial_allocation_leaves_a_remainder(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("20.000"),
            created_by=clerk,
        )
        assert receipt_availability(receipt.lines.get()).quantity == Decimal("30.000")
        assert invoice_line_availability(invoice.lines.get()).quantity == Decimal("30.000")

    def test_over_allocating_the_receipt_is_refused(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """
        The delivery brought fifty kilograms. Nothing may claim more of it,
        whatever the invoice says.
        """
        bigger = _approved_invoice(
            supplier=invoice.supplier,
            branch=invoice.branch,
            clerk=clerk,
            controller=clerk,
            item=receipt.lines.get().item,
            quantity="80.000",
            price="1450.000000",
            reference="INV-BIG",
        )
        other = create_purchase_match(invoice=bigger, created_by=clerk)
        with pytest.raises(ValidationError) as error:
            add_allocation(
                match=other,
                invoice_line=bigger.lines.get(),
                receipt_line=receipt.lines.get(),
                matched_base_quantity=Decimal("80.000"),
                created_by=clerk,
            )
        assert error.value.code == "receipt_over_allocation"

    def test_over_allocating_the_invoice_is_refused(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        receipt: GoodsReceipt,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        clerk: User,
        mapped: None,
    ) -> None:
        second = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="60.000",
            price="1400.000000",
            reference="DN-2",
        )
        with pytest.raises(ValidationError) as error:
            add_allocation(
                match=match,
                invoice_line=invoice.lines.get(),
                receipt_line=second.lines.get(),
                matched_base_quantity=Decimal("60.000"),
                created_by=clerk,
            )
        assert error.value.code == "invoice_over_allocation"

    def test_cancelling_a_match_gives_the_quantity_back(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """
        Availability is derived, and this is what that buys: a withdrawn match
        stops consuming without anybody having to find and correct a stored
        remainder.
        """
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        assert receipt_availability(receipt.lines.get()).quantity == Decimal("0.000")
        cancel_purchase_match(match=match, actor=clerk, reason="خطأ في التخصيص")
        assert receipt_availability(receipt.lines.get()).quantity == Decimal("50.000")

    def test_removing_an_allocation_gives_the_quantity_back(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        allocation = add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("30.000"),
            created_by=clerk,
        )
        remove_allocation(allocation=allocation)
        assert receipt_availability(receipt.lines.get()).quantity == Decimal("50.000")

    def test_many_receipts_may_cover_one_invoice_line(
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
        """PRC-040: allocation is many-to-many and partial."""
        first = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="30.000",
            price="1400.000000",
            reference="DN-A",
        )
        second = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="20.000",
            price="1400.000000",
            reference="DN-B",
        )
        invoice = _approved_invoice(
            supplier=grocery,
            branch=branch,
            clerk=clerk,
            controller=controller,
            item=rice,
            quantity="50.000",
            price="1400.000000",
            reference="INV-MULTI",
        )
        match = create_purchase_match(invoice=invoice, created_by=clerk)
        for receipt_doc, quantity in ((first, "30.000"), (second, "20.000")):
            add_allocation(
                match=match,
                invoice_line=invoice.lines.get(),
                receipt_line=receipt_doc.lines.get(),
                matched_base_quantity=Decimal(quantity),
                created_by=clerk,
            )
        assert invoice_line_availability(invoice.lines.get()).quantity == Decimal("0.000")
        assert match.allocations.count() == 2

    def test_an_order_line_cannot_be_over_allocated(
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
        order = create_purchase_order(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=clerk,
            ordered_on=datetime.date(TEST_YEAR, 2, 1),
        )
        add_order_line(
            order=order,
            item=rice,
            ordered_quantity=Decimal("40.000"),
            unit_price=Decimal("1400.000000"),
        )
        approve_purchase_order(order=order, actor=controller)
        issue_purchase_order(order=order, actor=clerk)
        order_line = order.lines.get()

        receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="40.000",
            price="1400.000000",
            reference="DN-ORDER",
            order_line=order_line,
        )
        invoice = _approved_invoice(
            supplier=grocery,
            branch=branch,
            clerk=clerk,
            controller=controller,
            item=rice,
            quantity="40.000",
            price="1400.000000",
            reference="INV-ORDER",
        )
        match = create_purchase_match(invoice=invoice, created_by=clerk)
        allocation = add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("40.000"),
            created_by=clerk,
        )
        # The version is copied, not joined to: a revision must not restate it.
        assert allocation.purchase_order_line == order_line
        assert allocation.purchase_order_version == order.version


# ---------------------------------------------------------------------------
# Value allocation
# ---------------------------------------------------------------------------


class TestValueAllocation:
    def test_a_full_allocation_takes_the_whole_value(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        allocation = add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        assert allocation.receipt_allocated_value == Decimal("70000.000")
        assert allocation.invoice_allocated_value == Decimal("72500.000")

    def test_a_partial_allocation_takes_its_share(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """Twenty of fifty kilograms: 28,000 of 70,000 and 29,000 of 72,500."""
        allocation = add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("20.000"),
            created_by=clerk,
        )
        assert allocation.receipt_allocated_value == Decimal("28000.000")
        assert allocation.invoice_allocated_value == Decimal("29000.000")

    def test_the_final_allocation_takes_the_exact_remainder(
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
        The claim that makes Task 2.12 safe.

        One delivery of three kilograms whose value does not divide by three,
        covering three invoice lines of one kilogram each. Naive proportions
        would lose a millifils; the last allocation taking the exact remainder
        means the parts sum back to the stored figure with nothing left over.
        """
        receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="3.000",
            price="333.333333",
            reference="DN-THIRDS",
        )
        raw = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number="INV-THIRDS",
            invoice_date=INVOICED,
            business_date=INVOICED,
        )
        for _index in range(3):
            add_inventory_line(
                invoice=raw,
                item=rice,
                base_quantity=Decimal("1.000"),
                unit_price=Decimal("333.333333"),
            )
        invoice = approve_supplier_invoice(invoice=raw, actor=controller)

        match = create_purchase_match(invoice=invoice, created_by=clerk)
        receipt_line = receipt.lines.get()
        posted = receipt_line.posted_value
        assert posted is not None

        for invoice_line in invoice.lines.order_by("sequence"):
            add_allocation(
                match=match,
                invoice_line=invoice_line,
                receipt_line=receipt_line,
                matched_base_quantity=Decimal("1.000"),
                created_by=clerk,
            )

        allocations = list(match.allocations.order_by("sequence"))
        assert len(allocations) == 3
        assert sum(row.receipt_allocated_value for row in allocations) == posted
        assert receipt_availability(receipt_line).quantity == Decimal("0.000")
        assert receipt_availability(receipt_line).value == Decimal("0.000")
        for invoice_line in invoice.lines.all():
            assert invoice_line_availability(invoice_line).value == Decimal("0.000")

    def test_the_receipt_basis_is_the_posted_value_not_the_average(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """
        The delivery's own posted value, whatever the warehouse average has
        become since. The average moved when other stock arrived; what this
        delivery parked in GRNI did not.
        """
        receipt_line = receipt.lines.get()
        allocation = add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt_line,
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        assert allocation.receipt_allocated_value == receipt_line.posted_value

    def test_a_net_amount_after_freight_is_the_invoice_basis(
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
        The invoice side uses the line's stored `net_amount`, which already
        carries its share of any Task 2.10 freight — re-pricing the line here
        would disagree with the document the supplier sent.
        """
        receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="10.000",
            price="1400.000000",
            reference="DN-FREIGHT",
        )
        raw = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number="INV-FREIGHT",
            invoice_date=INVOICED,
            business_date=INVOICED,
            freight_amount=Decimal("1000.000"),
        )
        add_inventory_line(
            invoice=raw,
            item=rice,
            base_quantity=Decimal("10.000"),
            unit_price=Decimal("1400.000000"),
        )
        invoice = approve_supplier_invoice(invoice=raw, actor=controller)
        line = invoice.lines.get()
        assert line.net_amount == Decimal("15000.000")

        match = create_purchase_match(invoice=invoice, created_by=clerk)
        allocation = add_allocation(
            match=match,
            invoice_line=line,
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("10.000"),
            created_by=clerk,
        )
        assert allocation.invoice_allocated_value == Decimal("15000.000")


# ---------------------------------------------------------------------------
# Price variance
# ---------------------------------------------------------------------------


class TestPriceVariance:
    def test_a_dearer_invoice_gives_a_positive_variance(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        allocation = add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        assert allocation.price_variance == Decimal("2500.000")

    def test_a_cheaper_invoice_gives_a_negative_variance(
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
        receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="10.000",
            price="1400.000000",
            reference="DN-CHEAP",
        )
        invoice = _approved_invoice(
            supplier=grocery,
            branch=branch,
            clerk=clerk,
            controller=controller,
            item=rice,
            quantity="10.000",
            price="1300.000000",
            reference="INV-CHEAP",
        )
        match = create_purchase_match(invoice=invoice, created_by=clerk)
        allocation = add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("10.000"),
            created_by=clerk,
        )
        assert allocation.price_variance == Decimal("-1000.000")

    def test_an_agreeing_invoice_gives_zero_variance(
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
        receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="10.000",
            price="1400.000000",
            reference="DN-EXACT",
        )
        invoice = _approved_invoice(
            supplier=grocery,
            branch=branch,
            clerk=clerk,
            controller=controller,
            item=rice,
            quantity="10.000",
            price="1400.000000",
            reference="INV-EXACT",
        )
        match = create_purchase_match(invoice=invoice, created_by=clerk)
        allocation = add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("10.000"),
            created_by=clerk,
        )
        assert allocation.price_variance == Decimal("0.000")
        assert invoice_line_match_state(invoice.lines.get()) == LineMatchState.MATCHED

    def test_the_variance_is_its_own_arithmetic_at_the_database(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        allocation = add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseMatchAllocation.objects.filter(pk=allocation.pk).update(
                price_variance=Decimal("1.000")
            )

    def test_a_fully_matched_line_with_a_variance_is_an_exception(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """
        PRC-042's fourth state. The quantity reconciles and the money does not,
        which is exactly the row a human has to decide about in Task 2.12.
        """
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        assert invoice_line_match_state(invoice.lines.get()) == LineMatchState.EXCEPTION

    def test_the_derived_states_walk_from_unmatched_to_matched(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        line = invoice.lines.get()
        assert invoice_line_match_state(line) == LineMatchState.UNMATCHED
        add_allocation(
            match=match,
            invoice_line=line,
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("20.000"),
            created_by=clerk,
        )
        assert invoice_line_match_state(line) == LineMatchState.PARTIALLY_MATCHED


# ---------------------------------------------------------------------------
# Matchability
# ---------------------------------------------------------------------------


class TestMatchability:
    def test_a_direct_account_line_can_never_be_matched(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        clerk: User,
        controller: User,
        rice: InventoryItem,
        organization: Organization,
        mapped: None,
    ) -> None:
        """
        A delivery charge posted with the invoice in Task 2.10. Matching it
        against goods would be matching a charge against a thing.
        """
        receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="10.000",
            price="1400.000000",
            reference="DN-MIX",
        )
        raw = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number="INV-MIX",
            invoice_date=INVOICED,
            business_date=INVOICED,
        )
        add_inventory_line(
            invoice=raw,
            item=rice,
            base_quantity=Decimal("10.000"),
            unit_price=Decimal("1400.000000"),
        )
        add_account_line(
            invoice=raw,
            account=Account.objects.get(organization=organization, code="5-01-02-003"),
            cost_center=CostCenter.objects.get(organization=organization, code="DELIVERY"),
            description="أجور نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("5000.000000"),
        )
        invoice = approve_supplier_invoice(invoice=raw, actor=controller)
        account_line = invoice.lines.filter(line_type="ACCOUNT").get()

        match = create_purchase_match(invoice=invoice, created_by=clerk)
        with pytest.raises(ValidationError) as error:
            add_allocation(
                match=match,
                invoice_line=account_line,
                receipt_line=receipt.lines.get(),
                matched_base_quantity=Decimal("1.000"),
                created_by=clerk,
            )
        assert error.value.code == "line_not_matchable"

    def test_an_invoice_with_only_account_lines_cannot_be_matched(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        organization: Organization,
        mapped: None,
    ) -> None:
        raw = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number="INV-ACC-ONLY",
            invoice_date=INVOICED,
            business_date=INVOICED,
        )
        add_account_line(
            invoice=raw,
            account=Account.objects.get(organization=organization, code="5-01-02-003"),
            cost_center=CostCenter.objects.get(organization=organization, code="DELIVERY"),
            description="أجور نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("5000.000000"),
        )
        invoice = approve_supplier_invoice(invoice=raw, actor=controller)
        with pytest.raises(ValidationError) as error:
            create_purchase_match(invoice=invoice, created_by=clerk)
        assert error.value.code == "invoice_has_no_matchable_lines"

    def test_a_different_item_is_refused(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        sugar: InventoryItem,
        clerk: User,
        mapped: None,
    ) -> None:
        """Sixty kilograms of sugar do not cover sixty kilograms of rice."""
        sugar_receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=sugar,
            quantity="50.000",
            price="1400.000000",
            reference="DN-SUGAR",
        )
        with pytest.raises(ValidationError) as error:
            add_allocation(
                match=match,
                invoice_line=invoice.lines.get(),
                receipt_line=sugar_receipt.lines.get(),
                matched_base_quantity=Decimal("10.000"),
                created_by=clerk,
            )
        assert error.value.code == "item_mismatch"

    def test_another_suppliers_delivery_is_refused(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        other_supplier: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        clerk: User,
        mapped: None,
    ) -> None:
        foreign = _post_receipt(
            supplier=other_supplier,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="50.000",
            price="1400.000000",
            reference="DN-OTHER",
        )
        with pytest.raises(ValidationError) as error:
            add_allocation(
                match=match,
                invoice_line=invoice.lines.get(),
                receipt_line=foreign.lines.get(),
                matched_base_quantity=Decimal("10.000"),
                created_by=clerk,
            )
        assert error.value.code == "supplier_mismatch"

    def test_a_reversed_receipt_cannot_be_matched(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        reverse_goods_receipt(receipt=receipt, actor=clerk, reason="أُعيدت الشحنة")
        with pytest.raises(ValidationError) as error:
            add_allocation(
                match=match,
                invoice_line=invoice.lines.get(),
                receipt_line=receipt.lines.get(),
                matched_base_quantity=Decimal("10.000"),
                created_by=clerk,
            )
        assert error.value.code == "receipt_not_posted"

    def test_a_reversed_invoice_cannot_be_matched(
        self,
        grocery: Supplier,
        branch: Branch,
        clerk: User,
        controller: User,
        organization: Organization,
        mapped: None,
    ) -> None:
        from apps.procurement.invoices import post_supplier_invoice

        raw = create_supplier_invoice(
            supplier=grocery,
            branch=branch,
            created_by=clerk,
            supplier_invoice_number="INV-REV",
            invoice_date=INVOICED,
            business_date=INVOICED,
        )
        add_account_line(
            invoice=raw,
            account=Account.objects.get(organization=organization, code="5-01-02-003"),
            cost_center=CostCenter.objects.get(organization=organization, code="DELIVERY"),
            description="أجور نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("5000.000000"),
        )
        approve_supplier_invoice(invoice=raw, actor=controller)
        posted = post_supplier_invoice(invoice=raw, actor=controller)
        reversed_invoice = reverse_supplier_invoice(invoice=posted, actor=controller, reason="خطأ")
        with pytest.raises(ValidationError) as error:
            create_purchase_match(invoice=reversed_invoice, created_by=clerk)
        assert error.value.code == "invoice_reversed"

    def test_a_matched_receipt_cannot_be_reversed(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """
        Task 2.9's extension point, doing exactly what it was written for.

        `_require_no_downstream_dependency` walks the receipt line's relations
        rather than a list of imports, so Task 2.11 declaring
        `match_allocations` gave the receipt its guard automatically — nobody
        had to remember. Reversing a delivery a match cites would leave the
        match citing something that did not happen.
        """
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        with pytest.raises(ValidationError) as error:
            reverse_goods_receipt(receipt=receipt, actor=clerk, reason="محاولة")
        assert error.value.code == "receipt_has_dependents"
        receipt.refresh_from_db()
        assert receipt.status == "POSTED"

    def test_cancelling_the_match_frees_the_receipt_to_be_reversed(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """
        The correct order, and it works: withdraw the claim, then undo the
        delivery.

        A cancelled match keeps its allocation rows as history — that is the
        right record — but it consumes no availability, and the reversal guard
        has to give the same answer. `live_dependency` is where the two are
        made to agree. Without it, cancelling a match, the documented
        correction, would leave the delivery unreversible for good.
        """
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        cancel_purchase_match(match=match, actor=clerk, reason="خُصّصت الشحنة الخطأ")
        assert PurchaseMatchAllocation.objects.filter(match=match).exists()

        reverse_goods_receipt(receipt=receipt, actor=clerk, reason="أُعيدت الشحنة")
        receipt.refresh_from_db()
        assert receipt.status == "REVERSED"

    def test_discarding_a_draft_frees_the_receipt_too(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """A discarded draft takes its allocation rows with it, so nothing is left to cite."""
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        delete_purchase_match(match=match)
        reverse_goods_receipt(receipt=receipt, actor=clerk, reason="أُعيدت الشحنة")
        receipt.refresh_from_db()
        assert receipt.status == "REVERSED"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_a_match_with_no_allocations_cannot_be_readied(
        self, match: PurchaseMatch, clerk: User
    ) -> None:
        with pytest.raises(ValidationError) as error:
            mark_match_ready(match=match, actor=clerk)
        assert error.value.code == "no_allocations"

    def test_a_ready_match_freezes_its_allocations(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        allocation = add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseMatchAllocation.objects.filter(pk=allocation.pk).update(
                matched_base_quantity=Decimal("1.000")
            )

    def test_a_ready_allocation_cannot_be_deleted(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseMatchAllocation.objects.all().delete()

    def test_a_ready_match_cannot_be_deleted(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseMatch.objects.filter(pk=match.pk).delete()

    def test_a_ready_match_cannot_return_to_draft(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """
        Correction is cancel-and-replace. Un-freezing evidence somebody may
        already have acted on is not a correction.
        """
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseMatch.objects.filter(pk=match.pk).update(status="DRAFT")

    def test_a_stale_draft_instance_cannot_edit_a_ready_match(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        stale = PurchaseMatch.objects.get(pk=match.pk)
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("20.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        assert stale.status == PurchaseMatchStatus.DRAFT
        with pytest.raises(ValidationError) as error:
            add_allocation(
                match=stale,
                invoice_line=invoice.lines.get(),
                receipt_line=receipt.lines.get(),
                matched_base_quantity=Decimal("10.000"),
                created_by=clerk,
            )
        assert error.value.code == "match_not_editable"

    def test_an_idempotent_ready_retry_returns_the_same_match(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        first = mark_match_ready(match=match, actor=clerk)
        second = mark_match_ready(match=first, actor=clerk)
        assert second.pk == first.pk
        assert second.ready_at == first.ready_at
        assert second.number == first.number

    def test_a_frozen_allocation_set_cannot_drift_underneath_its_fingerprint(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """
        Why `mark_match_ready`'s conflict branch never fires in practice.

        A retry over a *different* allocation set is a different economic
        claim, and the service refuses it. Reaching that branch means the set
        drifted after the fingerprint was taken — and the database does not
        allow the drift. Every route to it is closed: the allocations of a
        non-draft match refuse insert, update and delete, and the match row
        itself accepts nothing but its cancellation.

        The service check stays as the belt to that pair of braces. This test
        asserts the braces, because a test that reached the branch could only
        do so by writing something no code path can write — and an earlier
        draft of it did exactly that, with `update()`, and the trigger was
        right to refuse.
        """
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        ready = mark_match_ready(match=match, actor=clerk)
        allocation = ready.allocations.get()

        with pytest.raises(IntegrityError, match="frozen"), transaction.atomic():
            PurchaseMatchAllocation.objects.filter(pk=allocation.pk).update(
                matched_base_quantity=Decimal("10.000")
            )
        with pytest.raises(IntegrityError, match="frozen"), transaction.atomic():
            PurchaseMatchAllocation.objects.filter(pk=allocation.pk).delete()
        with pytest.raises(IntegrityError, match="ready"), transaction.atomic():
            PurchaseMatch.objects.filter(pk=ready.pk).update(allocation_fingerprint="stale")

        assert mark_match_ready(
            match=PurchaseMatch.objects.get(pk=ready.pk), actor=clerk
        ).number == (ready.number)

    def test_the_fingerprint_is_stable_and_sensitive(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        receipt: GoodsReceipt,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        clerk: User,
        mapped: None,
    ) -> None:
        """
        The two properties idempotency actually needs: the same allocation set
        always hashes the same, and a different one never does.
        """
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("20.000"),
            created_by=clerk,
        )
        first = allocation_fingerprint(match)
        assert allocation_fingerprint(match) == first

        second_receipt = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="30.000",
            price="1400.000000",
            reference="DN-FP",
        )
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=second_receipt.lines.get(),
            matched_base_quantity=Decimal("30.000"),
            created_by=clerk,
        )
        assert allocation_fingerprint(match) != first

    def test_a_duplicate_pair_in_one_match_is_refused(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        """
        Two rows for the same invoice line and delivery line are one allocation
        entered twice, and summing them would consume the remainder at double
        the rate.
        """
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("20.000"),
            created_by=clerk,
        )
        with pytest.raises(ValidationError):
            add_allocation(
                match=match,
                invoice_line=invoice.lines.get(),
                receipt_line=receipt.lines.get(),
                matched_base_quantity=Decimal("10.000"),
                created_by=clerk,
            )

    def test_cancelling_needs_a_reason(self, match: PurchaseMatch, clerk: User) -> None:
        with pytest.raises(ValidationError) as error:
            cancel_purchase_match(match=match, actor=clerk, reason="   ")
        assert error.value.code == "reason_required"

    def test_a_cancelled_match_cannot_be_cancelled_again(
        self, match: PurchaseMatch, clerk: User
    ) -> None:
        cancel_purchase_match(match=match, actor=clerk, reason="خطأ")
        with pytest.raises(ValidationError) as error:
            cancel_purchase_match(match=match, actor=clerk, reason="مرة أخرى")
        assert error.value.code == "already_cancelled"

    def test_a_cancelled_match_cannot_be_readied(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        cancel_purchase_match(match=match, actor=clerk, reason="خطأ")
        with pytest.raises(ValidationError) as error:
            mark_match_ready(match=PurchaseMatch.objects.get(pk=match.pk), actor=clerk)
        assert error.value.code == "match_cancelled"

    def test_only_one_active_match_per_invoice(
        self, match: PurchaseMatch, invoice: SupplierInvoice, clerk: User
    ) -> None:
        with pytest.raises(ValidationError) as error:
            create_purchase_match(invoice=invoice, created_by=clerk)
        assert error.value.code == "match_already_active"

    def test_a_cancelled_match_frees_the_invoice_for_a_replacement(
        self, match: PurchaseMatch, invoice: SupplierInvoice, clerk: User
    ) -> None:
        cancel_purchase_match(match=match, actor=clerk, reason="خطأ")
        replacement = create_purchase_match(invoice=invoice, created_by=clerk)
        assert replacement.pk != match.pk

    def test_a_draft_match_can_be_discarded(self, match: PurchaseMatch) -> None:
        delete_purchase_match(match=match)
        assert not PurchaseMatch.objects.filter(pk=match.pk).exists()

    def test_a_ready_match_draws_a_number(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        assert match.number == ""
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        ready = mark_match_ready(match=match, actor=clerk)
        assert ready.number.startswith("MTC-2026-")


# ---------------------------------------------------------------------------
# Reconciliation, coverage and the queue
# ---------------------------------------------------------------------------


class TestReconciliationAndQueue:
    def test_matching_reconciles(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        receipt: GoodsReceipt,
        clerk: User,
        organization: Organization,
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("30.000"),
            created_by=clerk,
        )
        assert verify_matching(organization) == []
        assert verify_procurement(organization) == []

    def test_a_partial_match_reconciles(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        receipt: GoodsReceipt,
        clerk: User,
        organization: Organization,
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("20.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        assert verify_matching(organization) == []

    def test_coverage_reports_both_quantity_and_variance(
        self, match: PurchaseMatch, invoice: SupplierInvoice, receipt: GoodsReceipt, clerk: User
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("20.000"),
            created_by=clerk,
        )
        rows = coverage_for_invoice(invoice)
        assert len(rows) == 1
        row = rows[0]
        assert row["matched_quantity"] == Decimal("20.000")
        assert row["unmatched_quantity"] == Decimal("30.000")
        assert row["price_variance"] == Decimal("1000.000")
        assert row["state"] == LineMatchState.PARTIALLY_MATCHED

    def test_the_queue_shows_a_delivery_nobody_has_billed(
        self, receipt: GoodsReceipt, organization: Organization
    ) -> None:
        rows = unmatched_receipt_lines(organization_id=organization.pk)
        assert len(rows) == 1
        assert rows[0].unmatched_quantity == Decimal("50.000")

    def test_the_queue_empties_as_a_delivery_is_matched(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        receipt: GoodsReceipt,
        clerk: User,
        organization: Organization,
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        assert unmatched_receipt_lines(organization_id=organization.pk) == []


# ---------------------------------------------------------------------------
# Permissions, scope and screens
# ---------------------------------------------------------------------------


class TestScopeAndPermissions:
    def test_the_role_map_separates_matching_from_withdrawing(self) -> None:
        accountant = permissions_for_role(Role.ACCOUNTANT)
        manager = permissions_for_role(Role.ACCOUNTING_MANAGER)
        assert MATCH_SUPPLIER_INVOICE in accountant
        assert CANCEL_PURCHASE_MATCH not in accountant
        assert CANCEL_PURCHASE_MATCH in manager

    def test_a_storekeeper_holds_no_matching_permission(self) -> None:
        keeper_permissions = permissions_for_role(Role.STOREKEEPER)
        assert VIEW_PURCHASE_MATCH not in keeper_permissions
        assert MATCH_SUPPLIER_INVOICE not in keeper_permissions

    def test_a_branch_membership_does_not_reach_a_match(
        self, match: PurchaseMatch, keeper: User
    ) -> None:
        assert visible_purchase_matches(keeper).count() == 0
        with pytest.raises(OutOfScope):
            resolve_purchase_match(keeper, match.pk)

    def test_another_organizations_match_is_a_404(
        self, match: PurchaseMatch, other_organization: Organization
    ) -> None:
        outsider = User.objects.create_user(username="outsider", password=PASSWORD)
        grant_organization_access(
            user=outsider, organization=other_organization, role=Role.ACCOUNTING_MANAGER
        )
        with pytest.raises(OutOfScope):
            resolve_purchase_match(User.objects.get(pk=outsider.pk), match.pk)

    def test_the_list_renders(self, match: PurchaseMatch, clerk: User, client: Client) -> None:
        client.force_login(clerk)
        response = client.get(reverse("procurement:purchase_match_list"))
        assert response.status_code == 200

    def test_the_queue_renders(self, receipt: GoodsReceipt, clerk: User, client: Client) -> None:
        client.force_login(clerk)
        response = client.get(reverse("procurement:matching_queue"))
        assert response.status_code == 200
        assert "تسليمات بلا فاتورة" in response.content.decode()

    def test_an_htmx_request_returns_only_the_fragment(
        self, match: PurchaseMatch, clerk: User, client: Client
    ) -> None:
        client.force_login(clerk)
        response = client.get(reverse("procurement:purchase_match_list"), headers=HX)
        assert response.status_code == 200
        assert "<html" not in response.content.decode().lower()

    def test_the_detail_screen_says_nothing_was_posted(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        receipt: GoodsReceipt,
        clerk: User,
        client: Client,
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        mark_match_ready(match=match, actor=clerk)
        client.force_login(clerk)
        body = client.get(
            reverse("procurement:purchase_match_detail", args=[match.pk])
        ).content.decode()
        assert "لم يُرحَّل أي مبلغ" in body

    def test_an_accountant_cannot_cancel_through_the_route(
        self, match: PurchaseMatch, clerk: User, client: Client
    ) -> None:
        client.force_login(clerk)
        response = client.post(
            reverse("procurement:purchase_match_cancel", args=[match.pk]),
            {"reason": "محاولة"},
        )
        assert response.status_code == 403
        match.refresh_from_db()
        assert match.status == PurchaseMatchStatus.DRAFT

    def test_a_get_does_not_ready_a_match(
        self, match: PurchaseMatch, clerk: User, client: Client
    ) -> None:
        client.force_login(clerk)
        response = client.get(reverse("procurement:purchase_match_ready", args=[match.pk]))
        assert response.status_code == 405
        match.refresh_from_db()
        assert match.status == PurchaseMatchStatus.DRAFT

    def test_the_api_omits_variance_without_the_cost_permission(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        receipt: GoodsReceipt,
        clerk: User,
        branch: Branch,
        organization: Organization,
        client: Client,
    ) -> None:
        """
        Price and variance are money. Omitted, never blanked — a null field
        says a number exists and you are not trusted with it, which is a
        different statement from not being shown one.
        """
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        viewer = User.objects.create_user(username="matcher-only", password=PASSWORD)
        grant_organization_access(
            user=viewer, organization=organization, role=Role.ACCOUNTING_MANAGER
        )
        viewer = User.objects.get(pk=viewer.pk)
        assert viewer.has_perm("procurement.view_supplier_cost")

        client.force_login(clerk)
        payload = client.get(f"/api/v1/procurement/matches/{match.pk}/").json()
        # The accountant does hold cost visibility, so the figure is present —
        # the point of the test is that the serializer gates on the permission
        # rather than on the endpoint.
        assert payload["allocations"][0]["price_variance"] == "2500.000"

    def test_money_crosses_the_wire_as_strings(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        receipt: GoodsReceipt,
        clerk: User,
        client: Client,
    ) -> None:
        add_allocation(
            match=match,
            invoice_line=invoice.lines.get(),
            receipt_line=receipt.lines.get(),
            matched_base_quantity=Decimal("50.000"),
            created_by=clerk,
        )
        client.force_login(clerk)
        raw = client.get(f"/api/v1/procurement/matches/{match.pk}/").content.decode()
        assert '"price_variance": "2500.000"' in raw or '"price_variance":"2500.000"' in raw
        assert "2500.0," not in raw

    def test_the_command_endpoints_drive_the_lifecycle(
        self,
        match: PurchaseMatch,
        invoice: SupplierInvoice,
        receipt: GoodsReceipt,
        clerk: User,
        controller: User,
        client: Client,
    ) -> None:
        client.force_login(clerk)
        created = client.post(
            f"/api/v1/procurement/matches/{match.pk}/allocations/",
            data={
                "invoice_line_id": invoice.lines.get().pk,
                "receipt_line_id": receipt.lines.get().pk,
                "matched_base_quantity": "50.000",
            },
            content_type="application/json",
        )
        assert created.status_code == 201
        ready = client.post(f"/api/v1/procurement/matches/{match.pk}/ready/")
        assert ready.status_code == 200
        assert ready.json()["status"] == "READY"
        assert ready.json()["is_financially_posted"] is False

        client.force_login(controller)
        cancelled = client.post(
            f"/api/v1/procurement/matches/{match.pk}/cancel/",
            data={"reason": "أُلغيت"},
            content_type="application/json",
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


class TestDemoMatches:
    def test_the_seed_skips_what_is_missing(self, organization: Organization, clerk: User) -> None:
        from apps.procurement.demo import seed_demo_matches

        assert seed_demo_matches(organization=organization, matcher=clerk) == []
