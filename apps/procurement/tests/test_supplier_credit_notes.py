"""
Task 2.14 — the supplier's agreed figure arriving on paper.

    Dr  SUPPLIER_PAYABLE             the agreed credit
        Cr  SUPPLIER_RETURN_CLEARING   the claim closed at the return's book value
        Cr/Dr PURCHASE_RETURN_VARIANCE the difference, either direction

These are the positive twins of `TestTheCreditNoteBoundary` in
`test_supplier_returns.py`: the variance the return refused to guess is
recognised here, against a figure somebody actually signed.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.test import Client
from django.urls import reverse

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
    JournalLine,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.inventory.models import InventoryItem, ItemType, StockMovement, Warehouse
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access, grant_organization_access
from apps.procurement.credit_notes import (
    add_credit_allocation,
    add_return_allocation,
    create_supplier_credit_note,
    delete_supplier_credit_note,
    post_supplier_credit_note,
    remaining_book_value,
    remaining_credit_quantity,
    remove_return_allocation,
    reverse_supplier_credit_note,
    unallocated_credit,
)
from apps.procurement.invoices import (
    add_account_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    outstanding_amount,
    post_supplier_invoice,
)
from apps.procurement.models import (
    Supplier,
    SupplierCreditNote,
    SupplierCreditNoteStatus,
    SupplierInvoice,
    SupplierReturn,
    SupplierReturnStatus,
)
from apps.procurement.posting import post_goods_receipt
from apps.procurement.reconciliation import verify_procurement, verify_supplier_credit_notes
from apps.procurement.returns import (
    add_return_line,
    create_supplier_return,
    post_supplier_return,
    reverse_supplier_return,
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
RECEIVED = datetime.date(TEST_YEAR, 3, 1)
RETURNED = datetime.date(TEST_YEAR, 3, 5)
CREDITED = datetime.date(TEST_YEAR, 3, 12)
PASSWORD = "pw-not-real-1234"

INVENTORY_CODE = "1-03-01-001"
GRNI_CODE = "2-01-02-001"
PAYABLE_CODE = "2-01-01-001"
CLEARING_CODE = "8-01-04-001"
RETURN_VARIANCE_CODE = "7-09-04-001"
EXPENSE_CODE = "5-01-02-003"


# ---------------------------------------------------------------------------
# Fixtures — the return file's scene, plus the variance mapping this task adds
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
    """Everything a delivery, a return and a credit note need to post."""
    for code, account_code in (
        (INVENTORY_CONTROL, INVENTORY_CODE),
        (GOODS_RECEIVED_NOT_INVOICED, GRNI_CODE),
        (SUPPLIER_PAYABLE, PAYABLE_CODE),
        (SUPPLIER_RETURN_CLEARING, CLEARING_CODE),
        # Task 2.14's first deliberate act: the role Task 2.13 seeded and
        # refused to map is mapped here, because now something posts to it.
        (PURCHASE_RETURN_VARIANCE, RETURN_VARIANCE_CODE),
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
        name="رز",
        category=create_item_category(organization=organization, code="GRAINS", name="حبوب"),
        item_type=ItemType.RAW_MATERIAL,
        base_unit=kilogram,
    )


@pytest.fixture
def store(branch: Branch) -> Warehouse:
    from apps.inventory.services import create_warehouse

    return create_warehouse(branch=branch, code="MAIN", name="مخزن")


@pytest.fixture
def grocery(organization: Organization) -> Supplier:
    return create_supplier(organization=organization, code="GROC-01", name="مورد")


@pytest.fixture
def keeper(branch: Branch, store: Warehouse) -> User:
    user = User.objects.create_user(username="keeper", password=PASSWORD)
    grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def manager(organization: Organization) -> User:
    user = User.objects.create_user(username="manager", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def posted_return(
    grocery: Supplier,
    branch: Branch,
    store: Warehouse,
    keeper: User,
    rice: InventoryItem,
    mapped: None,
) -> SupplierReturn:
    """Ten of fifty kilograms at 1,400 go back: book value 14,000 in clearing."""
    receipt = create_goods_receipt(
        supplier=grocery,
        branch=branch,
        warehouse=store,
        created_by=keeper,
        received_at=RECEIVED,
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
    post_goods_receipt(receipt=receipt, actor=keeper)

    supplier_return = create_supplier_return(
        organization=receipt.organization,
        branch=receipt.branch,
        supplier=receipt.supplier,
        warehouse=receipt.warehouse,
        location=receipt.location,
        created_by=keeper,
        returned_at=RETURNED,
        reason="بضاعة تالفة",
        evidence_reference="وصل السائق",
    )
    add_return_line(
        supplier_return=supplier_return,
        item=rice,
        returned_base_quantity=Decimal("10.000"),
    )
    return post_supplier_return(supplier_return=supplier_return, actor=keeper)


def _draft_note(
    *,
    posted_return: SupplierReturn,
    keeper: User,
    amount: str,
    reference: str = "SCN-77",
    allocate: bool = True,
) -> SupplierCreditNote:
    """A draft note, by default settling the return's single line in full."""
    note = create_supplier_credit_note(
        supplier_return=posted_return,
        created_by=keeper,
        supplier_document_number=reference,
        credit_date=CREDITED,
        amount=Decimal(amount),
    )
    if allocate:
        line = posted_return.lines.get()
        add_return_allocation(
            credit_note=note,
            return_line=line,
            credited_base_quantity=remaining_credit_quantity(line),
            allocated_credit_amount=note.amount,
        )
    return note


def _expense_invoice(
    *, organization: Organization, supplier: Supplier, branch: Branch, keeper: User, amount: str
) -> SupplierInvoice:
    """A posted direct-charge invoice: the payable a note can net against."""
    invoice = create_supplier_invoice(
        supplier=supplier,
        branch=branch,
        created_by=keeper,
        supplier_invoice_number=f"INV-{amount}",
        invoice_date=RECEIVED,
    )
    add_account_line(
        invoice=invoice,
        account=Account.objects.get(organization=organization, code=EXPENSE_CODE),
        cost_center=CostCenter.objects.filter(organization=organization).first(),
        description="أجور نقل",
        quantity=Decimal("1.000"),
        unit_price=Decimal(amount),
    )
    approve_supplier_invoice(invoice=invoice, actor=keeper)
    return post_supplier_invoice(invoice=invoice, actor=keeper)


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


# ---------------------------------------------------------------------------
# The entry
# ---------------------------------------------------------------------------


class TestTheEntry:
    def test_an_agreeing_credit_posts_two_lines_and_no_variance(
        self, posted_return: SupplierReturn, keeper: User, organization: Organization
    ) -> None:
        """Amount == book value: the claim closes exactly, nothing to recognise."""
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        posted = post_supplier_credit_note(credit_note=note, actor=keeper)

        assert posted.status == SupplierCreditNoteStatus.POSTED
        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            PAYABLE_CODE: Decimal("14000.000"),
            CLEARING_CODE: Decimal("-14000.000"),
        }
        assert _balance(organization, CLEARING_CODE) == Decimal("0.000")
        assert _balance(organization, RETURN_VARIANCE_CODE) == Decimal("0.000")

    def test_a_dearer_credit_recognises_the_gain(
        self, posted_return: SupplierReturn, keeper: User, organization: Organization
    ) -> None:
        """
        The supplier credits the receipt price while the average had blended
        lower — the ADR-022 §2 difference, recognised only now, against paper.
        """
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="15000.000")
        posted = post_supplier_credit_note(credit_note=note, actor=keeper)

        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            PAYABLE_CODE: Decimal("15000.000"),
            CLEARING_CODE: Decimal("-14000.000"),
            RETURN_VARIANCE_CODE: Decimal("-1000.000"),
        }
        assert _balance(organization, CLEARING_CODE) == Decimal("0.000")

    def test_a_cheaper_credit_recognises_the_loss(
        self, posted_return: SupplierReturn, keeper: User, organization: Organization
    ) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="12500.000")
        posted = post_supplier_credit_note(credit_note=note, actor=keeper)

        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            PAYABLE_CODE: Decimal("12500.000"),
            CLEARING_CODE: Decimal("-14000.000"),
            RETURN_VARIANCE_CODE: Decimal("1500.000"),
        }
        assert _balance(organization, RETURN_VARIANCE_CODE) == Decimal("1500.000")

    def test_it_never_moves_stock(self, posted_return: SupplierReturn, keeper: User) -> None:
        """PRC-051's last clause: the goods left with the return."""
        before = StockMovement.objects.count()
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        post_supplier_credit_note(credit_note=note, actor=keeper)
        assert StockMovement.objects.count() == before

    def test_the_source_identity_is_complete_and_the_number_gapless(
        self, posted_return: SupplierReturn, keeper: User
    ) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        posted = post_supplier_credit_note(credit_note=note, actor=keeper)
        assert posted.number.startswith("SCN-")
        assert posted.journal_entry is not None
        assert posted.journal_entry.source_document_type == "PROCUREMENT_SUPPLIER_CREDIT_NOTE"
        assert posted.journal_entry.source_document_id == str(posted.public_id)

    def test_reconciliation_is_clean_after_a_posting(
        self, posted_return: SupplierReturn, keeper: User, organization: Organization
    ) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="15000.000")
        post_supplier_credit_note(credit_note=note, actor=keeper)
        assert verify_procurement(organization) == []


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


class TestTheClaim:
    def test_only_a_posted_return_takes_a_note(
        self, posted_return: SupplierReturn, keeper: User, manager: User
    ) -> None:
        reverse_supplier_return(
            supplier_return=posted_return, actor=manager, reason="فحص المورد أكّدها"
        )
        with pytest.raises(ValidationError) as refusal:
            _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        assert refusal.value.code == "return_not_posted"

    def test_two_partial_notes_settle_one_return(
        self, posted_return: SupplierReturn, keeper: User, organization: Organization
    ) -> None:
        """
        The approved rule: settlement is partial and may take several notes.
        Five of ten kilograms, then the other five — the first takes its
        proportional share of the claim, the second takes the exact remainder,
        and the clearing account ends at zero with no residual.
        """
        line = posted_return.lines.get()
        first = _draft_note(
            posted_return=posted_return, keeper=keeper, amount="7500.000", allocate=False
        )
        add_return_allocation(
            credit_note=first,
            return_line=line,
            credited_base_quantity=Decimal("5.000"),
            allocated_credit_amount=Decimal("7500.000"),
        )
        post_supplier_credit_note(credit_note=first, actor=keeper)
        assert remaining_credit_quantity(line) == Decimal("5.000")
        assert remaining_book_value(line) == Decimal("7000.000")
        assert _balance(organization, CLEARING_CODE) == Decimal("7000.000")

        second = _draft_note(
            posted_return=posted_return,
            keeper=keeper,
            amount="6500.000",
            reference="SCN-88",
            allocate=False,
        )
        add_return_allocation(
            credit_note=second,
            return_line=line,
            credited_base_quantity=Decimal("5.000"),
            allocated_credit_amount=Decimal("6500.000"),
        )
        post_supplier_credit_note(credit_note=second, actor=keeper)
        assert remaining_credit_quantity(line) == Decimal("0.000")
        assert remaining_book_value(line) == Decimal("0.000")
        assert _balance(organization, CLEARING_CODE) == Decimal("0.000")
        # 7,500 − 7,000 favorable, 6,500 − 7,000 unfavorable: net variance 0.
        assert _balance(organization, RETURN_VARIANCE_CODE) == Decimal("0.000")
        assert verify_procurement(organization) == []

    def test_a_proportional_slice_and_an_exact_remainder_on_an_odd_value(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        mapped: None,
        organization: Organization,
    ) -> None:
        """
        An indivisible book value: 10,000.001 over three of seven kilograms is
        4,285.715 by ROUND_HALF_UP, and the closing slice takes 5,714.286 —
        the exact remainder, not its own rounding — so the two sum to the fils.
        """
        receipt = create_goods_receipt(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            delivery_reference="DN-ODD",
            evidence_reference="إشعار",
        )
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("7.000"),
            unit_price=Decimal("1428.571571"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("7.000"), actor=keeper)
        post_goods_receipt(receipt=receipt, actor=keeper)
        supplier_return = create_supplier_return(
            organization=receipt.organization,
            branch=receipt.branch,
            supplier=receipt.supplier,
            warehouse=receipt.warehouse,
            location=receipt.location,
            created_by=keeper,
            returned_at=RETURNED,
            evidence_reference="وصل",
        )
        add_return_line(
            supplier_return=supplier_return,
            item=rice,
            returned_base_quantity=Decimal("7.000"),
        )
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)
        return_line = posted.lines.get()
        book = return_line.posted_value
        assert book == Decimal("10000.001")

        first = create_supplier_credit_note(
            supplier_return=posted,
            created_by=keeper,
            supplier_document_number="SCN-ODD-1",
            credit_date=CREDITED,
            amount=Decimal("4000.000"),
        )
        add_return_allocation(
            credit_note=first,
            return_line=return_line,
            credited_base_quantity=Decimal("3.000"),
            allocated_credit_amount=Decimal("4000.000"),
        )
        post_supplier_credit_note(credit_note=first, actor=keeper)
        first_slice = first.return_allocations.get()
        first_slice.refresh_from_db()
        assert first_slice.settled_book_value == Decimal("4285.715")

        second = create_supplier_credit_note(
            supplier_return=posted,
            created_by=keeper,
            supplier_document_number="SCN-ODD-2",
            credit_date=CREDITED,
            amount=Decimal("6000.000"),
        )
        add_return_allocation(
            credit_note=second,
            return_line=return_line,
            credited_base_quantity=Decimal("4.000"),
            allocated_credit_amount=Decimal("6000.000"),
        )
        post_supplier_credit_note(credit_note=second, actor=keeper)
        second_slice = second.return_allocations.get()
        second_slice.refresh_from_db()
        assert second_slice.settled_book_value == book - Decimal("4285.715")
        assert remaining_book_value(return_line) == Decimal("0.000")
        assert verify_procurement(organization) == []

    def test_one_note_covers_two_return_lines(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        kilogram: UnitOfMeasure,
        mapped: None,
        organization: Organization,
    ) -> None:
        from apps.inventory.services import create_item, create_item_category

        sugar = create_item(
            organization=organization,
            code="SUGAR",
            name="سكر",
            category=create_item_category(organization=organization, code="SWEET", name="سكر"),
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        receipt = create_goods_receipt(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            delivery_reference="DN-TWO",
            evidence_reference="إشعار",
        )
        for item, price in ((rice, "1400.000000"), (sugar, "900.000000")):
            line = add_receipt_line(
                receipt=receipt,
                item=item,
                delivered_quantity=Decimal("10.000"),
                unit_price=Decimal(price),
            )
            inspect_receipt_line(line=line, accepted_base_quantity=Decimal("10.000"), actor=keeper)
        post_goods_receipt(receipt=receipt, actor=keeper)
        supplier_return = create_supplier_return(
            organization=receipt.organization,
            branch=receipt.branch,
            supplier=receipt.supplier,
            warehouse=receipt.warehouse,
            location=receipt.location,
            created_by=keeper,
            returned_at=RETURNED,
            evidence_reference="وصل",
        )
        for receipt_line in receipt.lines.select_related("item", "lot").order_by("sequence"):
            add_return_line(
                supplier_return=supplier_return,
                item=receipt_line.item,
                lot=receipt_line.lot,
                returned_base_quantity=Decimal("10.000"),
            )
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)
        rice_line, sugar_line = posted.lines.order_by("sequence")

        note = create_supplier_credit_note(
            supplier_return=posted,
            created_by=keeper,
            supplier_document_number="SCN-TWO",
            credit_date=CREDITED,
            amount=Decimal("23000.000"),
        )
        add_return_allocation(
            credit_note=note,
            return_line=rice_line,
            credited_base_quantity=Decimal("10.000"),
            allocated_credit_amount=Decimal("14000.000"),
        )
        add_return_allocation(
            credit_note=note,
            return_line=sugar_line,
            credited_base_quantity=Decimal("10.000"),
            allocated_credit_amount=Decimal("9000.000"),
        )
        posted_note = post_supplier_credit_note(credit_note=note, actor=keeper)
        assert posted_note.journal_entry is not None
        # 14,000 + 9,000 settled exactly: no variance line at all.
        assert _lines(posted_note.journal_entry) == {
            PAYABLE_CODE: Decimal("23000.000"),
            CLEARING_CODE: Decimal("-23000.000"),
        }
        assert verify_procurement(organization) == []

    def test_crediting_more_than_the_line_returned_is_refused(
        self, posted_return: SupplierReturn, keeper: User
    ) -> None:
        line = posted_return.lines.get()
        note = _draft_note(
            posted_return=posted_return, keeper=keeper, amount="15000.000", allocate=False
        )
        with pytest.raises(ValidationError) as refusal:
            add_return_allocation(
                credit_note=note,
                return_line=line,
                credited_base_quantity=Decimal("10.001"),
                allocated_credit_amount=Decimal("15000.000"),
            )
        assert refusal.value.code == "credit_over_quantity"

    def test_the_settled_book_value_cannot_exceed_the_line(
        self, posted_return: SupplierReturn, keeper: User, organization: Organization
    ) -> None:
        """
        Over-settlement is structurally impossible: the value is derived from
        the remaining claim, the final slice takes the exact remainder, and a
        third note finds no quantity left to hang a value on.
        """
        line = posted_return.lines.get()
        for index, quantity in enumerate(("6.000", "4.000"), start=1):
            note = _draft_note(
                posted_return=posted_return,
                keeper=keeper,
                amount="1000.000",
                reference=f"SCN-CAP-{index}",
                allocate=False,
            )
            add_return_allocation(
                credit_note=note,
                return_line=line,
                credited_base_quantity=Decimal(quantity),
                allocated_credit_amount=Decimal("1000.000"),
            )
            post_supplier_credit_note(credit_note=note, actor=keeper)
        assert remaining_book_value(line) == Decimal("0.000")

        third = _draft_note(
            posted_return=posted_return,
            keeper=keeper,
            amount="1.000",
            reference="SCN-CAP-3",
            allocate=False,
        )
        with pytest.raises(ValidationError) as refusal:
            add_return_allocation(
                credit_note=third,
                return_line=line,
                credited_base_quantity=Decimal("0.001"),
                allocated_credit_amount=Decimal("1.000"),
            )
        assert refusal.value.code == "credit_over_quantity"
        assert verify_procurement(organization) == []

    def test_a_partial_note_reversal_releases_its_exact_slice(
        self,
        posted_return: SupplierReturn,
        keeper: User,
        manager: User,
        organization: Organization,
    ) -> None:
        line = posted_return.lines.get()
        note = _draft_note(
            posted_return=posted_return, keeper=keeper, amount="7000.000", allocate=False
        )
        add_return_allocation(
            credit_note=note,
            return_line=line,
            credited_base_quantity=Decimal("5.000"),
            allocated_credit_amount=Decimal("7000.000"),
        )
        post_supplier_credit_note(credit_note=note, actor=keeper)
        assert remaining_credit_quantity(line) == Decimal("5.000")
        assert remaining_book_value(line) == Decimal("7000.000")

        reverse_supplier_credit_note(credit_note=note, actor=manager, reason="خطأ")
        assert remaining_credit_quantity(line) == Decimal("10.000")
        assert remaining_book_value(line) == Decimal("14000.000")
        assert _balance(organization, CLEARING_CODE) == Decimal("14000.000")

        replacement = _draft_note(
            posted_return=posted_return, keeper=keeper, amount="14000.000", reference="SCN-92"
        )
        post_supplier_credit_note(credit_note=replacement, actor=keeper)
        assert _balance(organization, CLEARING_CODE) == Decimal("0.000")
        assert verify_procurement(organization) == []

    def test_a_note_that_settles_nothing_cannot_post(
        self, posted_return: SupplierReturn, keeper: User
    ) -> None:
        note = _draft_note(
            posted_return=posted_return, keeper=keeper, amount="14000.000", allocate=False
        )
        with pytest.raises(ValidationError) as refusal:
            post_supplier_credit_note(credit_note=note, actor=keeper)
        assert refusal.value.code == "no_return_allocations"

    def test_the_credit_must_be_fully_attributed(
        self, posted_return: SupplierReturn, keeper: User
    ) -> None:
        """Every fils of the note names the line it answers."""
        line = posted_return.lines.get()
        note = _draft_note(
            posted_return=posted_return, keeper=keeper, amount="14000.000", allocate=False
        )
        add_return_allocation(
            credit_note=note,
            return_line=line,
            credited_base_quantity=Decimal("10.000"),
            allocated_credit_amount=Decimal("13000.000"),
        )
        with pytest.raises(ValidationError) as refusal:
            post_supplier_credit_note(credit_note=note, actor=keeper)
        assert refusal.value.code == "credit_not_fully_attributed"
        # Completing the attribution is an edit of the slice: remove and
        # re-add, then the posting goes through.
        remove_return_allocation(allocation=note.return_allocations.get())
        add_return_allocation(
            credit_note=note,
            return_line=line,
            credited_base_quantity=Decimal("10.000"),
            allocated_credit_amount=Decimal("14000.000"),
        )
        assert post_supplier_credit_note(credit_note=note, actor=keeper).journal_entry

    def test_a_reversed_note_frees_the_return_for_another(
        self, posted_return: SupplierReturn, keeper: User, manager: User
    ) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        post_supplier_credit_note(credit_note=note, actor=keeper)
        reverse_supplier_credit_note(
            credit_note=note, actor=manager, reason="رقم المستند كان خاطئاً"
        )
        replacement = _draft_note(
            posted_return=posted_return, keeper=keeper, amount="14000.000", reference="SCN-89"
        )
        assert replacement.status == SupplierCreditNoteStatus.DRAFT

    def test_a_standing_note_blocks_the_returns_reversal(
        self, posted_return: SupplierReturn, keeper: User, manager: User
    ) -> None:
        """
        The return's dependency guard walks its relations, so the note gets
        the protection by declaring `live_dependency` — nobody edited the
        guard, which is the convention working as designed.
        """
        _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        with pytest.raises(ValidationError) as refusal:
            reverse_supplier_return(supplier_return=posted_return, actor=manager, reason="محاولة")
        assert refusal.value.code == "return_has_dependents"

    def test_reversing_the_note_frees_the_return(
        self, posted_return: SupplierReturn, keeper: User, manager: User
    ) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        post_supplier_credit_note(credit_note=note, actor=keeper)
        reverse_supplier_credit_note(credit_note=note, actor=manager, reason="خطأ")
        reversed_return = reverse_supplier_return(
            supplier_return=posted_return, actor=manager, reason="فحص المورد أكّدها"
        )
        assert reversed_return.status == SupplierReturnStatus.REVERSED

    def test_the_same_document_number_twice_is_refused(
        self, posted_return: SupplierReturn, keeper: User, manager: User
    ) -> None:
        """PRC-052, with the invoice's folding: case and whitespace collapse."""
        note = _draft_note(
            posted_return=posted_return, keeper=keeper, amount="14000.000", reference="scn-100"
        )
        post_supplier_credit_note(credit_note=note, actor=keeper)
        reverse_supplier_credit_note(credit_note=note, actor=manager, reason="خطأ")
        # Reversed: the number is free again (the index excludes REVERSED)…
        replacement = _draft_note(
            posted_return=posted_return,
            keeper=keeper,
            amount="14000.000",
            reference="SCN-100",
            allocate=False,
        )
        # …but a second standing use of it is refused at the database.
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                SupplierCreditNote.objects.create(
                    organization=replacement.organization,
                    branch=replacement.branch,
                    supplier=replacement.supplier,
                    supplier_return=replacement.supplier_return,
                    supplier_document_number=replacement.supplier_document_number,
                    supplier_document_number_key=replacement.supplier_document_number_key,
                    credit_date=CREDITED,
                    business_date=CREDITED,
                    amount=Decimal("1.000"),
                    created_by=keeper,
                )


# ---------------------------------------------------------------------------
# Allocations
# ---------------------------------------------------------------------------


class TestAllocations:
    def test_an_allocation_reduces_the_invoice_outstanding(
        self,
        posted_return: SupplierReturn,
        keeper: User,
        grocery: Supplier,
        branch: Branch,
        organization: Organization,
    ) -> None:
        invoice = _expense_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            keeper=keeper,
            amount="20000.000000",
        )
        assert outstanding_amount(invoice) == Decimal("20000.000")

        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        add_credit_allocation(
            credit_note=note, invoice=invoice, allocated_amount=Decimal("9000.000")
        )
        # A draft's allocations are intent, not fact.
        assert outstanding_amount(invoice) == Decimal("20000.000")

        post_supplier_credit_note(credit_note=note, actor=keeper)
        assert outstanding_amount(invoice) == Decimal("11000.000")
        note.refresh_from_db()
        assert unallocated_credit(note) == Decimal("5000.000")

    def test_over_allocating_the_invoice_is_refused(
        self,
        posted_return: SupplierReturn,
        keeper: User,
        grocery: Supplier,
        branch: Branch,
        organization: Organization,
    ) -> None:
        invoice = _expense_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            keeper=keeper,
            amount="5000.000000",
        )
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        with pytest.raises(ValidationError) as refusal:
            add_credit_allocation(
                credit_note=note, invoice=invoice, allocated_amount=Decimal("6000.000")
            )
        assert refusal.value.code == "allocation_over_invoice"

    def test_allocations_may_not_exceed_the_note(
        self,
        posted_return: SupplierReturn,
        keeper: User,
        grocery: Supplier,
        branch: Branch,
        organization: Organization,
    ) -> None:
        invoice = _expense_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            keeper=keeper,
            amount="50000.000000",
        )
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        with pytest.raises(ValidationError) as refusal:
            add_credit_allocation(
                credit_note=note, invoice=invoice, allocated_amount=Decimal("14000.001")
            )
        assert refusal.value.code == "allocation_over_note"

    def test_a_posted_notes_allocation_blocks_the_invoice_reversal(
        self,
        posted_return: SupplierReturn,
        keeper: User,
        manager: User,
        grocery: Supplier,
        branch: Branch,
        organization: Organization,
    ) -> None:
        """The invoice guard reads `live_dependency` — again, unedited."""
        from apps.procurement.invoices import reverse_supplier_invoice

        invoice = _expense_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            keeper=keeper,
            amount="20000.000000",
        )
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        add_credit_allocation(
            credit_note=note, invoice=invoice, allocated_amount=Decimal("9000.000")
        )
        post_supplier_credit_note(credit_note=note, actor=keeper)

        with pytest.raises(ValidationError) as refusal:
            reverse_supplier_invoice(invoice=invoice, actor=manager, reason="محاولة")
        assert refusal.value.code == "invoice_has_dependents"

    def test_a_posted_notes_allocations_are_frozen_at_the_database(
        self,
        posted_return: SupplierReturn,
        keeper: User,
        grocery: Supplier,
        branch: Branch,
        organization: Organization,
    ) -> None:
        invoice = _expense_invoice(
            organization=organization,
            supplier=grocery,
            branch=branch,
            keeper=keeper,
            amount="20000.000000",
        )
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        allocation = add_credit_allocation(
            credit_note=note, invoice=invoice, allocated_amount=Decimal("9000.000")
        )
        post_supplier_credit_note(credit_note=note, actor=keeper)

        with pytest.raises(Exception, match="frozen"):
            with transaction.atomic():
                SupplierCreditNote.objects.none()  # keep the atomic block honest
                type(allocation).objects.filter(pk=allocation.pk).update(
                    allocated_amount=Decimal("1.000")
                )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_posting_twice_is_refused(self, posted_return: SupplierReturn, keeper: User) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        post_supplier_credit_note(credit_note=note, actor=keeper)
        with pytest.raises(ValidationError) as refusal:
            post_supplier_credit_note(credit_note=note, actor=keeper)
        assert refusal.value.code == "already_posted"

    def test_a_reversed_note_cannot_be_posted_again(
        self, posted_return: SupplierReturn, keeper: User, manager: User
    ) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        post_supplier_credit_note(credit_note=note, actor=keeper)
        reverse_supplier_credit_note(credit_note=note, actor=manager, reason="خطأ")
        with pytest.raises(ValidationError) as refusal:
            post_supplier_credit_note(credit_note=note, actor=keeper)
        assert refusal.value.code == "credit_note_not_draft"

    def test_the_reversal_mirrors_exactly_and_reopens_the_claim(
        self,
        posted_return: SupplierReturn,
        keeper: User,
        manager: User,
        organization: Organization,
    ) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="15000.000")
        post_supplier_credit_note(credit_note=note, actor=keeper)
        assert _balance(organization, CLEARING_CODE) == Decimal("0.000")

        reversed_note = reverse_supplier_credit_note(
            credit_note=note, actor=manager, reason="رقم خاطئ"
        )
        assert reversed_note.reversal_journal_entry is not None
        assert _lines(reversed_note.reversal_journal_entry) == {
            PAYABLE_CODE: Decimal("-15000.000"),
            CLEARING_CODE: Decimal("14000.000"),
            RETURN_VARIANCE_CODE: Decimal("1000.000"),
        }
        # The claim stands open again, to the fils.
        assert _balance(organization, CLEARING_CODE) == Decimal("14000.000")
        assert _balance(organization, RETURN_VARIANCE_CODE) == Decimal("0.000")
        assert verify_procurement(organization) == []

    def test_a_draft_can_be_discarded_and_a_posted_one_cannot(
        self, posted_return: SupplierReturn, keeper: User
    ) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        delete_supplier_credit_note(credit_note=note)
        assert not SupplierCreditNote.objects.filter(pk=note.pk).exists()

        second = _draft_note(
            posted_return=posted_return, keeper=keeper, amount="14000.000", reference="SCN-90"
        )
        post_supplier_credit_note(credit_note=second, actor=keeper)
        with pytest.raises(ValidationError):
            delete_supplier_credit_note(credit_note=second)

    def test_a_posted_note_is_immutable_at_the_database(
        self, posted_return: SupplierReturn, keeper: User
    ) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        post_supplier_credit_note(credit_note=note, actor=keeper)
        with pytest.raises(Exception, match="posted"):
            with transaction.atomic():
                SupplierCreditNote.objects.filter(pk=note.pk).update(amount=Decimal("99999.000"))

    def test_an_unmapped_variance_role_rolls_everything_back(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        organization: Organization,
        accounting: None,
    ) -> None:
        """
        A note whose figures disagree needs the variance account; one whose
        figures agree does not. The mapping is demanded exactly when a line
        will exist — the Task 2.13 rule that a role with no posting behind it
        must not need mapping, kept precisely.
        """
        for code, account_code in (
            (INVENTORY_CONTROL, INVENTORY_CODE),
            (GOODS_RECEIVED_NOT_INVOICED, GRNI_CODE),
            (SUPPLIER_PAYABLE, PAYABLE_CODE),
            (SUPPLIER_RETURN_CLEARING, CLEARING_CODE),
        ):
            create_account_mapping(
                organization=organization,
                account_role=AccountRole.objects.get(code=code),
                account=Account.objects.get(organization=organization, code=account_code),
                effective_from=JAN_1,
            )
        receipt = create_goods_receipt(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            delivery_reference="DN-2",
            evidence_reference="إشعار",
        )
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("50.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("50.000"), actor=keeper)
        post_goods_receipt(receipt=receipt, actor=keeper)
        supplier_return = create_supplier_return(
            organization=receipt.organization,
            branch=receipt.branch,
            supplier=receipt.supplier,
            warehouse=receipt.warehouse,
            location=receipt.location,
            created_by=keeper,
            returned_at=RETURNED,
            evidence_reference="وصل",
        )
        add_return_line(
            supplier_return=supplier_return,
            item=rice,
            returned_base_quantity=Decimal("10.000"),
        )
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)

        journals_before = JournalEntry.objects.count()
        disagreeing = _draft_note(posted_return=posted, keeper=keeper, amount="15000.000")
        with pytest.raises(ValidationError):
            post_supplier_credit_note(credit_note=disagreeing, actor=keeper)
        disagreeing.refresh_from_db()
        assert disagreeing.status == SupplierCreditNoteStatus.DRAFT
        assert disagreeing.number == ""
        assert JournalEntry.objects.count() == journals_before

        # The agreeing note posts without the variance mapping existing.
        delete_supplier_credit_note(credit_note=disagreeing)
        agreeing = _draft_note(
            posted_return=posted, keeper=keeper, amount="14000.000", reference="SCN-91"
        )
        assert post_supplier_credit_note(credit_note=agreeing, actor=keeper).journal_entry

    def test_the_dedicated_verifier_stands_alone(
        self, posted_return: SupplierReturn, keeper: User, organization: Organization
    ) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="12500.000")
        post_supplier_credit_note(credit_note=note, actor=keeper)
        assert verify_supplier_credit_notes(organization) == []


# ---------------------------------------------------------------------------
# The screens and the API
# ---------------------------------------------------------------------------


class TestTheSurface:
    def test_the_list_and_detail_render(
        self, posted_return: SupplierReturn, keeper: User, manager: User, client: Client
    ) -> None:
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="15000.000")
        post_supplier_credit_note(credit_note=note, actor=keeper)
        note.refresh_from_db()

        client.force_login(manager)
        listing = client.get(reverse("procurement:supplier_credit_note_list"))
        assert listing.status_code == 200
        assert note.number in listing.content.decode()

        detail = client.get(reverse("procurement:supplier_credit_note_detail", args=[note.pk]))
        assert detail.status_code == 200
        page = detail.content.decode()
        assert note.journal_entry is not None
        assert note.journal_entry.entry_number in page

        fragment = client.get(
            reverse("procurement:supplier_credit_note_list"), HTTP_HX_REQUEST="true"
        )
        assert fragment.status_code == 200
        assert len(fragment.content) < len(listing.content)

    def test_the_create_screen_opens_a_draft(
        self, posted_return: SupplierReturn, manager: User, client: Client
    ) -> None:
        client.force_login(manager)
        response = client.post(
            reverse("procurement:supplier_credit_note_create"),
            {
                "supplier_return": posted_return.pk,
                "supplier_document_number": "SCN-UI-1",
                "credit_date": "2026-03-12",
                "amount": "14000.000",
            },
        )
        assert response.status_code == 302
        created = SupplierCreditNote.objects.get(supplier_document_number="SCN-UI-1")
        assert created.status == SupplierCreditNoteStatus.DRAFT
        assert created.supplier_id == posted_return.supplier_id

    def test_the_command_routes_post_and_refuse_get(
        self,
        posted_return: SupplierReturn,
        keeper: User,
        manager: User,
        accounting_manager: User,
        client: Client,
    ) -> None:
        """
        The maker-checker split, exercised through the routes: the manager who
        can record a note cannot post it, and the accounting manager can.
        """
        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        client.force_login(accounting_manager)
        refused = client.get(reverse("procurement:supplier_credit_note_post", args=[note.pk]))
        assert refused.status_code == 405

        client.force_login(manager)
        forbidden = client.post(reverse("procurement:supplier_credit_note_post", args=[note.pk]))
        assert forbidden.status_code == 403
        note.refresh_from_db()
        assert note.status == SupplierCreditNoteStatus.DRAFT

        client.force_login(accounting_manager)
        posted = client.post(reverse("procurement:supplier_credit_note_post", args=[note.pk]))
        assert posted.status_code == 302
        note.refresh_from_db()
        assert note.status == SupplierCreditNoteStatus.POSTED

    def test_a_storekeeper_reaches_no_credit_note(
        self, posted_return: SupplierReturn, keeper: User, client: Client
    ) -> None:
        """Money is organization-scoped; custody of a store is not authority over a debt."""
        _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        storekeeper = User.objects.create_user(username="pure-keeper", password=PASSWORD)
        grant_branch_access(
            user=storekeeper,
            branch=posted_return.branch,
            role=Role.STOREKEEPER,
        )
        storekeeper = User.objects.get(pk=storekeeper.pk)
        client.force_login(storekeeper)
        response = client.get(reverse("procurement:supplier_credit_note_list"))
        assert response.status_code == 403

    def test_the_api_drives_the_lifecycle_and_money_is_strings(
        self,
        posted_return: SupplierReturn,
        manager: User,
        accounting_manager: User,
        client: Client,
    ) -> None:
        client.force_login(manager)
        created = client.post(
            "/api/v1/procurement/supplier-credit-notes/",
            data={
                "supplier_return_id": posted_return.pk,
                "supplier_document_number": "SCN-API-1",
                "credit_date": "2026-03-12",
                "amount": "15000.000",
            },
            content_type="application/json",
        )
        assert created.status_code == 201
        note_id = created.json()["id"]

        allocated = client.post(
            f"/api/v1/procurement/supplier-credit-notes/{note_id}/return-allocations/",
            data={
                "return_line_id": posted_return.lines.get().pk,
                "credited_base_quantity": "10.000",
                "allocated_credit_amount": "15000.000",
            },
            content_type="application/json",
        )
        assert allocated.status_code == 201
        assert allocated.json()["return_allocations"][0]["credited_base_quantity"] == "10.000"

        client.force_login(accounting_manager)
        posted = client.post(f"/api/v1/procurement/supplier-credit-notes/{note_id}/post/")
        assert posted.status_code == 200
        payload = posted.json()
        assert payload["status"] == "POSTED"
        assert payload["journal_entry"]
        raw = client.get(f"/api/v1/procurement/supplier-credit-notes/{note_id}/").content.decode()
        assert '"amount": "15000.000"' in raw or '"amount":"15000.000"' in raw

        reversed_response = client.post(
            f"/api/v1/procurement/supplier-credit-notes/{note_id}/reverse/",
            data={"reason": "رقم خاطئ"},
            content_type="application/json",
        )
        assert reversed_response.status_code == 200
        assert reversed_response.json()["status"] == "REVERSED"

    def test_out_of_scope_is_404(
        self, posted_return: SupplierReturn, keeper: User, client: Client
    ) -> None:
        from apps.organizations.services import create_organization

        note = _draft_note(posted_return=posted_return, keeper=keeper, amount="14000.000")
        outsider = User.objects.create_user(username="scn-outsider", password=PASSWORD)
        rival = create_organization(code="RIV3", name="منافس")
        grant_organization_access(user=outsider, organization=rival, role=Role.MANAGER)
        outsider = User.objects.get(pk=outsider.pk)
        client.force_login(outsider)
        assert (
            client.get(
                reverse("procurement:supplier_credit_note_detail", args=[note.pk])
            ).status_code
            == 404
        )
        assert (
            client.get(f"/api/v1/procurement/supplier-credit-notes/{note.pk}/").status_code == 404
        )

    def test_the_navigation_names_the_credit_note_screen(self) -> None:
        from apps.core.navigation import MODULES

        procurement = next(m for m in MODULES if m.key == "procurement")
        section = next(
            s for s in procurement.sections if str(s.label) == "إشعارات الموردين الدائنة"
        )
        assert section.available is True
        assert section.url_name == "procurement:supplier_credit_note_list"


class TestDemoCreditNotes:
    def test_the_seed_skips_what_is_missing(
        self, organization: Organization, keeper: User, manager: User
    ) -> None:
        from apps.procurement.demo import seed_demo_credit_notes

        assert (
            seed_demo_credit_notes(organization=organization, recorder=keeper, poster=manager) == []
        )
