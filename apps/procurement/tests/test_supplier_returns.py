"""
Task 2.13 — goods going back out to the supplier they came from.

    Dr  SUPPLIER_RETURN_CLEARING     the book value that left
        Cr  Inventory control        the same figure

and nothing else. `TestTheCreditNoteBoundary` is the negative half: no
variance, no payable, no GRNI, and no credit note — all four are Task 2.14's,
and every assertion there is a negative whose positive twin that task must
deliberately write.
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
    SUPPLIER_PAYABLE,
    SUPPLIER_RETURN_CLEARING,
    Account,
    AccountClass,
    AccountRole,
    JournalEntry,
    JournalLine,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.inventory.models import (
    InventoryItem,
    ItemType,
    MovementType,
    StockBalance,
    StockMovement,
    Warehouse,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_organization,
    grant_branch_access,
    grant_organization_access,
)
from apps.procurement.models import (
    GoodsReceipt,
    Supplier,
    SupplierReturn,
    SupplierReturnStatus,
)
from apps.procurement.posting import post_goods_receipt, reverse_goods_receipt
from apps.procurement.reconciliation import verify_procurement
from apps.procurement.returns import (
    add_return_line,
    create_supplier_return,
    delete_supplier_return,
    post_supplier_return,
    return_availability,
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
PASSWORD = "pw-not-real-1234"

INVENTORY_CODE = "1-03-01-001"
GRNI_CODE = "2-01-02-001"
PAYABLE_CODE = "2-01-01-001"
CLEARING_CODE = "8-01-04-001"
RETURN_VARIANCE_CODE = "7-09-04-001"


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
    """Everything a delivery and a return need to post."""
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


@pytest.fixture
def unmapped_clearing(organization: Organization, accounting: None) -> None:
    """Everything except the return clearing role."""
    for code, account_code in (
        (INVENTORY_CONTROL, INVENTORY_CODE),
        (GOODS_RECEIVED_NOT_INVOICED, GRNI_CODE),
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
def manager(organization: Organization) -> User:
    """Holds the elevated reversal, which a storekeeper does not."""
    user = User.objects.create_user(username="manager", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.MANAGER)
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


@pytest.fixture
def receipt(
    grocery: Supplier,
    branch: Branch,
    store: Warehouse,
    keeper: User,
    rice: InventoryItem,
    mapped: None,
) -> GoodsReceipt:
    """Fifty kilograms at 1,400 — 70,000 of stock and the same in GRNI."""
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


def _draft_return(
    *, receipt: GoodsReceipt, keeper: User, quantity: str = "10.000", expected: str | None = None
) -> SupplierReturn:
    supplier_return = create_supplier_return(
        receipt=receipt,
        created_by=keeper,
        returned_at=RETURNED,
        reason="بضاعة تالفة",
        evidence_reference="وصل الاستلام من السائق",
    )
    add_return_line(
        supplier_return=supplier_return,
        receipt_line=receipt.lines.get(),
        returned_base_quantity=Decimal(quantity),
        expected_credit_value=Decimal(expected) if expected is not None else None,
    )
    return supplier_return


def _balance(organization: Organization, code: str) -> Decimal:
    account = Account.objects.get(organization=organization, code=code)
    row = JournalLine.objects.filter(account=account).aggregate(
        debit=Sum("debit"), credit=Sum("credit")
    )
    return (row["debit"] or Decimal("0.000")) - (row["credit"] or Decimal("0.000"))


def _lines(journal: JournalEntry) -> dict[str, Decimal]:
    return {
        row.account.code: row.debit - row.credit
        for row in journal.lines.select_related("account").all()
    }


# ---------------------------------------------------------------------------
# The entry
# ---------------------------------------------------------------------------


class TestTheEntry:
    def test_a_return_moves_stock_out_and_clears_its_book_value(
        self,
        receipt: GoodsReceipt,
        keeper: User,
        rice: InventoryItem,
        store: Warehouse,
        organization: Organization,
    ) -> None:
        """
        The whole of Task 2.13's accounting, in one assertion each side.

        Ten of fifty kilograms at a standing average of 1,400 is 14,000 of book
        value. It leaves inventory and lands in the clearing account, where it
        waits for the credit note that says what the supplier will allow.
        """
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)

        assert posted.status == SupplierReturnStatus.POSTED
        assert posted.posted_value == Decimal("14000.000")
        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            CLEARING_CODE: Decimal("14000.000"),
            INVENTORY_CODE: Decimal("-14000.000"),
        }

        balance = StockBalance.objects.get(warehouse=store, item=rice, lot__isnull=True)
        assert balance.quantity == Decimal("40.000")
        assert balance.value == Decimal("56000.000")
        # The average is untouched: an outbound leaves *at* it, never changes it.
        assert balance.average_cost == Decimal("1400.000000")

    def test_the_movement_is_return_out_and_not_return_in(
        self, receipt: GoodsReceipt, keeper: User
    ) -> None:
        """PRC-047, asserted on the movement the kernel actually wrote."""
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)

        assert posted.stock_entry is not None
        movement = posted.stock_entry.movements.get()
        assert movement.movement_type == MovementType.RETURN_OUT
        assert movement.base_quantity == Decimal("-10.000")
        assert StockMovement.objects.filter(movement_type=MovementType.RETURN_IN).count() == 0

    def test_it_leaves_at_the_standing_average_not_the_receipt_price(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """
        ADR-022 §1's worked example, and the thing that will look like a bug.

        A hundred kilos at 1,000 and a hundred at 2,000 make two hundred at
        1,500. Twenty kilos from the *second* delivery go back and 30,000
        leaves — not 40,000 — because under a moving average there is no
        kilogram in that warehouse that *is* the expensive one.
        """
        cheap = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="100.000",
            price="1000.000000",
            reference="DN-CHEAP",
        )
        dear = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="100.000",
            price="2000.000000",
            reference="DN-DEAR",
        )
        assert cheap.posted_value == Decimal("100000.000")

        supplier_return = _draft_return(receipt=dear, keeper=keeper, quantity="20.000")
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)

        assert posted.posted_value == Decimal("30000.000")
        balance = StockBalance.objects.get(warehouse=store, item=rice, lot__isnull=True)
        assert balance.quantity == Decimal("180.000")
        assert balance.average_cost == Decimal("1500.000000")

    def test_a_full_return_surrenders_the_whole_remaining_value(
        self, receipt: GoodsReceipt, keeper: User, rice: InventoryItem, store: Warehouse
    ) -> None:
        """ADR-018's full-depletion rule, inherited rather than reimplemented."""
        supplier_return = _draft_return(receipt=receipt, keeper=keeper, quantity="50.000")
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)

        assert posted.posted_value == Decimal("70000.000")
        balance = StockBalance.objects.get(warehouse=store, item=rice, lot__isnull=True)
        assert balance.quantity == Decimal("0.000")
        assert balance.value == Decimal("0.000")

    def test_the_source_identity_is_complete(self, receipt: GoodsReceipt, keeper: User) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)

        assert posted.journal_entry is not None
        assert posted.journal_entry.source_document_type == "PROCUREMENT_SUPPLIER_RETURN"
        assert posted.journal_entry.source_document_id == str(posted.public_id)
        assert posted.journal_entry.source_event == "POSTED"
        assert posted.stock_entry is not None
        assert posted.stock_entry.source_document_type == "PROCUREMENT_SUPPLIER_RETURN"

    def test_it_draws_a_gapless_number(self, receipt: GoodsReceipt, keeper: User) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)
        assert posted.number

    def test_a_missing_mapping_rolls_everything_back(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        unmapped_clearing: None,
    ) -> None:
        """PRC-034 and PRC-036: no movement, no journal, no status change."""
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
        supplier_return = _draft_return(receipt=receipt, keeper=keeper, quantity="5.000")
        movements = StockMovement.objects.count()
        journals = JournalEntry.objects.count()

        with pytest.raises(ValidationError) as error:
            post_supplier_return(supplier_return=supplier_return, actor=keeper)
        assert error.value.code == "account_role_unmapped"

        supplier_return.refresh_from_db()
        assert supplier_return.status == SupplierReturnStatus.DRAFT
        assert StockMovement.objects.count() == movements
        assert JournalEntry.objects.count() == journals


# ---------------------------------------------------------------------------
# The quantity bound
# ---------------------------------------------------------------------------


class TestTheQuantityBound:
    def test_availability_is_the_accepted_quantity_less_what_has_gone(
        self, receipt: GoodsReceipt, keeper: User
    ) -> None:
        line = receipt.lines.get()
        assert return_availability(line) == Decimal("50.000")

        supplier_return = _draft_return(receipt=receipt, keeper=keeper, quantity="20.000")
        assert return_availability(line) == Decimal("30.000")

        post_supplier_return(supplier_return=supplier_return, actor=keeper)
        assert return_availability(line) == Decimal("30.000")

    def test_returning_more_than_arrived_is_refused(
        self, receipt: GoodsReceipt, keeper: User
    ) -> None:
        """
        The kernel cannot do this. `_require_available` refuses to drive a
        warehouse position negative, which is a different question — with stock
        on hand from other deliveries a line that accepted fifty could be
        returned for fifty twice and every kernel check would pass.
        """
        supplier_return = create_supplier_return(
            receipt=receipt, created_by=keeper, returned_at=RETURNED, evidence_reference="وصل"
        )
        with pytest.raises(ValidationError) as error:
            add_return_line(
                supplier_return=supplier_return,
                receipt_line=receipt.lines.get(),
                returned_base_quantity=Decimal("50.001"),
            )
        assert error.value.code == "return_over_quantity"

    def test_the_same_delivery_cannot_be_returned_twice_over(
        self,
        receipt: GoodsReceipt,
        keeper: User,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """
        With plenty of stock on hand from a second delivery, so the kernel
        would happily move it. Only the per-delivery bound stops this.
        """
        _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="500.000",
            price="1400.000000",
            reference="DN-PLENTY",
        )
        first = _draft_return(receipt=receipt, keeper=keeper, quantity="50.000")
        post_supplier_return(supplier_return=first, actor=keeper)

        second = create_supplier_return(
            receipt=receipt, created_by=keeper, returned_at=RETURNED, evidence_reference="وصل"
        )
        with pytest.raises(ValidationError) as error:
            add_return_line(
                supplier_return=second,
                receipt_line=receipt.lines.get(),
                returned_base_quantity=Decimal("1.000"),
            )
        assert error.value.code == "return_over_quantity"

    def test_a_reversed_return_gives_the_quantity_back(
        self, receipt: GoodsReceipt, keeper: User, manager: User
    ) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper, quantity="50.000")
        post_supplier_return(supplier_return=supplier_return, actor=keeper)
        assert return_availability(receipt.lines.get()) == Decimal("0.000")

        reverse_supplier_return(
            supplier_return=supplier_return, actor=manager, reason="عاد الاتفاق"
        )
        assert return_availability(receipt.lines.get()) == Decimal("50.000")

    def test_a_wholly_rejected_delivery_line_cannot_be_returned(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """It never entered stock, so there is nothing to send back from."""
        receipt = create_goods_receipt(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            delivery_reference="DN-REJECT-ALL",
            evidence_reference="إشعار",
        )
        good = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            unit_price=Decimal("1000.000000"),
        )
        inspect_receipt_line(line=good, accepted_base_quantity=Decimal("10.000"), actor=keeper)
        bad = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("5.000"),
            unit_price=Decimal("1000.000000"),
        )
        from apps.inventory.models import InventoryReasonCode, ReasonCodeApplication

        reason_code = InventoryReasonCode.objects.create(
            organization=receipt.organization,
            code="SPOILED",
            name_ar="تالف عند الاستلام",
            applies_to=ReasonCodeApplication.WASTE,
        )
        inspect_receipt_line(
            line=bad,
            accepted_base_quantity=Decimal("0.000"),
            actor=keeper,
            rejection_reason=reason_code,
            note="وصلت تالفة",
        )
        posted = post_goods_receipt(receipt=receipt, actor=keeper)

        supplier_return = create_supplier_return(
            receipt=posted, created_by=keeper, returned_at=RETURNED, evidence_reference="وصل"
        )
        with pytest.raises(ValidationError) as error:
            add_return_line(
                supplier_return=supplier_return,
                receipt_line=posted.lines.get(sequence=2),
                returned_base_quantity=Decimal("1.000"),
            )
        assert error.value.code == "receipt_line_rejected"

    def test_a_line_from_another_delivery_is_refused(
        self,
        receipt: GoodsReceipt,
        keeper: User,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        other = _post_receipt(
            supplier=grocery,
            branch=branch,
            store=store,
            keeper=keeper,
            item=rice,
            quantity="10.000",
            price="1400.000000",
            reference="DN-OTHER",
        )
        supplier_return = create_supplier_return(
            receipt=receipt, created_by=keeper, returned_at=RETURNED, evidence_reference="وصل"
        )
        with pytest.raises(ValidationError) as error:
            add_return_line(
                supplier_return=supplier_return,
                receipt_line=other.lines.get(),
                returned_base_quantity=Decimal("1.000"),
            )
        assert error.value.code == "receipt_line_mismatch"


# ---------------------------------------------------------------------------
# Lifecycle, immutability and the correction path
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_a_returned_delivery_cannot_be_reversed(
        self, receipt: GoodsReceipt, keeper: User
    ) -> None:
        """
        Task 2.9's guard, doing what it was written for. Reversing the delivery
        underneath a return would leave the return citing goods that never
        arrived.
        """
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=supplier_return, actor=keeper)

        with pytest.raises(ValidationError) as error:
            reverse_goods_receipt(receipt=receipt, actor=keeper, reason="محاولة")
        assert error.value.code == "receipt_has_dependents"

    def test_reversing_the_return_frees_the_delivery(
        self, receipt: GoodsReceipt, keeper: User, manager: User
    ) -> None:
        """
        `live_dependency` again: a reversed return has put the goods back and
        holds the delivery hostage no longer.
        """
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=supplier_return, actor=keeper)
        reverse_supplier_return(supplier_return=supplier_return, actor=manager, reason="خطأ")

        reverse_goods_receipt(receipt=receipt, actor=keeper, reason="أُعيدت الشحنة")
        receipt.refresh_from_db()
        assert receipt.status == "REVERSED"

    def test_a_draft_return_also_blocks_the_delivery(
        self, receipt: GoodsReceipt, keeper: User
    ) -> None:
        """A draft is a live claim on that quantity, and it consumes it."""
        _draft_return(receipt=receipt, keeper=keeper)
        with pytest.raises(ValidationError) as error:
            reverse_goods_receipt(receipt=receipt, actor=keeper, reason="محاولة")
        assert error.value.code == "receipt_has_dependents"

    def test_reversing_puts_the_stock_and_the_ledger_back(
        self,
        receipt: GoodsReceipt,
        keeper: User,
        manager: User,
        organization: Organization,
        rice: InventoryItem,
        store: Warehouse,
    ) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=supplier_return, actor=keeper)
        reverse_supplier_return(supplier_return=supplier_return, actor=manager, reason="خطأ")

        supplier_return.refresh_from_db()
        assert supplier_return.status == SupplierReturnStatus.REVERSED
        assert supplier_return.reversal_journal_entry is not None
        assert _balance(organization, CLEARING_CODE) == Decimal("0.000")

        balance = StockBalance.objects.get(warehouse=store, item=rice, lot__isnull=True)
        assert balance.quantity == Decimal("50.000")
        assert balance.value == Decimal("70000.000")

    def test_posting_twice_is_refused(self, receipt: GoodsReceipt, keeper: User) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=supplier_return, actor=keeper)
        supplier_return.refresh_from_db()
        with pytest.raises(ValidationError) as error:
            post_supplier_return(supplier_return=supplier_return, actor=keeper)
        assert error.value.code == "already_posted"

    def test_a_reversed_return_cannot_be_posted_again(
        self, receipt: GoodsReceipt, keeper: User, manager: User
    ) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=supplier_return, actor=keeper)
        reverse_supplier_return(supplier_return=supplier_return, actor=manager, reason="خطأ")
        supplier_return.refresh_from_db()
        with pytest.raises(ValidationError) as error:
            post_supplier_return(supplier_return=supplier_return, actor=keeper)
        assert error.value.code == "return_not_draft"

    def test_posting_needs_its_evidence(self, receipt: GoodsReceipt, keeper: User) -> None:
        supplier_return = create_supplier_return(
            receipt=receipt, created_by=keeper, returned_at=RETURNED
        )
        add_return_line(
            supplier_return=supplier_return,
            receipt_line=receipt.lines.get(),
            returned_base_quantity=Decimal("1.000"),
        )
        with pytest.raises(ValidationError) as error:
            post_supplier_return(supplier_return=supplier_return, actor=keeper)
        assert error.value.code == "evidence_required"

    def test_an_empty_return_cannot_be_posted(self, receipt: GoodsReceipt, keeper: User) -> None:
        supplier_return = create_supplier_return(
            receipt=receipt, created_by=keeper, returned_at=RETURNED, evidence_reference="وصل"
        )
        with pytest.raises(ValidationError) as error:
            post_supplier_return(supplier_return=supplier_return, actor=keeper)
        assert error.value.code == "no_lines"

    def test_a_draft_can_be_discarded_and_a_posted_one_cannot(
        self, receipt: GoodsReceipt, keeper: User
    ) -> None:
        draft = _draft_return(receipt=receipt, keeper=keeper)
        delete_supplier_return(supplier_return=draft)
        assert not SupplierReturn.objects.filter(pk=draft.pk).exists()

        posted_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=posted_return, actor=keeper)
        with pytest.raises(ValidationError) as error:
            delete_supplier_return(supplier_return=posted_return)
        assert error.value.code == "return_not_editable"

    def test_a_posted_return_is_immutable_at_the_database(
        self, receipt: GoodsReceipt, keeper: User
    ) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)
        with pytest.raises(IntegrityError, match="posted"), transaction.atomic():
            SupplierReturn.objects.filter(pk=posted.pk).update(reason="شيء آخر")

    def test_a_posted_line_is_frozen_at_the_database(
        self, receipt: GoodsReceipt, keeper: User
    ) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)
        line = posted.lines.get()
        with pytest.raises(IntegrityError, match="frozen"), transaction.atomic():
            posted.lines.filter(pk=line.pk).update(returned_base_quantity=Decimal("1.000"))


# ---------------------------------------------------------------------------
# The Task 2.14 boundary
# ---------------------------------------------------------------------------


class TestTheCreditNoteBoundary:
    """
    A return says goods went back. It does not say what they are worth to the
    supplier, and every assertion here is a negative whose positive twin Task
    2.14 must deliberately write.
    """

    def test_it_posts_no_return_variance(
        self, receipt: GoodsReceipt, keeper: User, organization: Organization
    ) -> None:
        """
        The supplier has not agreed a figure yet. Booking a gain or a loss
        against an expectation would put a number on the profit and loss that
        nobody has signed.
        """
        supplier_return = _draft_return(receipt=receipt, keeper=keeper, expected="15000.000")
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)

        assert posted.journal_entry is not None
        assert RETURN_VARIANCE_CODE not in _lines(posted.journal_entry)
        assert _balance(organization, RETURN_VARIANCE_CODE) == Decimal("0.000")

    def test_the_expected_credit_is_metadata_and_posts_nothing(
        self, receipt: GoodsReceipt, keeper: User
    ) -> None:
        """
        Recorded for the screen and for Task 2.14 to compare against, and
        deliberately absent from every figure the journal carries — the book
        value is 14,000 whatever the operator expects to be credited.
        """
        supplier_return = _draft_return(receipt=receipt, keeper=keeper, expected="99999.000")
        posted = post_supplier_return(supplier_return=supplier_return, actor=keeper)

        assert posted.lines.get().expected_credit_value == Decimal("99999.000")
        assert posted.posted_value == Decimal("14000.000")
        assert posted.journal_entry is not None
        assert _lines(posted.journal_entry) == {
            CLEARING_CODE: Decimal("14000.000"),
            INVENTORY_CODE: Decimal("-14000.000"),
        }

    def test_it_touches_neither_the_payable_nor_grni(
        self, receipt: GoodsReceipt, keeper: User, organization: Organization
    ) -> None:
        """
        Task 2.13 depends on 2.9, not on the invoice. Debiting the payable
        would move a liability with no document stating its amount; debiting
        GRNI would make the accounting depend on whether an invoice arrived.
        """
        grni_before = _balance(organization, GRNI_CODE)
        payable_before = _balance(organization, PAYABLE_CODE)

        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=supplier_return, actor=keeper)

        assert _balance(organization, GRNI_CODE) == grni_before
        assert _balance(organization, PAYABLE_CODE) == payable_before

    def test_the_clearing_balance_stands_open(
        self, receipt: GoodsReceipt, keeper: User, organization: Organization
    ) -> None:
        """
        Nothing in this task empties it, and that is the point: the balance is
        the claim outstanding until a credit note settles it.
        """
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=supplier_return, actor=keeper)
        assert _balance(organization, CLEARING_CODE) == Decimal("14000.000")
        assert verify_procurement(organization) == []

    def test_the_credit_note_model_now_exists(self) -> None:
        """The positive twin of Step 15's boundary marker: Task 2.14 arrived."""
        from django.apps import apps as django_apps

        names = {model.__name__ for model in django_apps.get_app_config("procurement").get_models()}
        assert "SupplierCreditNote" in names

    def test_the_return_itself_still_never_touches_the_variance_role(
        self, organization: Organization, mapped: None
    ) -> None:
        """
        Task 2.14 maps and posts to `PURCHASE_RETURN_VARIANCE` — from the
        credit note. The *return* still refuses to guess: this fixture maps
        nothing to the role, and every return in this file posts anyway,
        which is the unmapped half of Step 15's boundary held where it still
        applies.
        """
        from apps.accounting.models import OrganizationAccountMapping

        assert AccountRole.objects.filter(code="PURCHASE_RETURN_VARIANCE").exists()
        assert not OrganizationAccountMapping.objects.filter(
            organization=organization, account_role__code="PURCHASE_RETURN_VARIANCE"
        ).exists()
        account = Account.objects.get(organization=organization, code=RETURN_VARIANCE_CODE)
        assert account.account_class == AccountClass.OTHER


# ---------------------------------------------------------------------------
# The screens
# ---------------------------------------------------------------------------


class TestTheScreens:
    def test_the_list_and_detail_render_for_a_storekeeper(
        self, receipt: GoodsReceipt, keeper: User, client: Client
    ) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=supplier_return, actor=keeper)
        supplier_return.refresh_from_db()

        client.force_login(keeper)
        listing = client.get(reverse("procurement:supplier_return_list"))
        assert listing.status_code == 200
        body = listing.content.decode()
        assert supplier_return.number in body

        detail = client.get(
            reverse("procurement:supplier_return_detail", args=[supplier_return.pk])
        )
        assert detail.status_code == 200
        page = detail.content.decode()
        assert supplier_return.number in page
        assert supplier_return.journal_entry is not None
        assert supplier_return.journal_entry.entry_number in page
        # The availability table names the bound: 50 accepted, 10 returned.
        assert "المتاح للإرجاع" in page

    def test_the_list_answers_htmx_with_a_fragment(
        self, receipt: GoodsReceipt, keeper: User, client: Client
    ) -> None:
        _draft_return(receipt=receipt, keeper=keeper)
        client.force_login(keeper)
        full = client.get(reverse("procurement:supplier_return_list"))
        fragment = client.get(reverse("procurement:supplier_return_list"), HTTP_HX_REQUEST="true")
        assert fragment.status_code == 200
        assert len(fragment.content) < len(full.content)

    def test_the_create_screen_opens_a_draft_against_the_delivery(
        self, receipt: GoodsReceipt, keeper: User, client: Client
    ) -> None:
        client.force_login(keeper)
        response = client.post(
            reverse("procurement:supplier_return_create"),
            {
                "receipt": receipt.pk,
                "returned_at": "2026-03-05",
                "reason": "تلف",
                "evidence_reference": "وصل",
            },
        )
        assert response.status_code == 302
        created = SupplierReturn.objects.get(receipt=receipt)
        assert created.status == SupplierReturnStatus.DRAFT
        assert created.warehouse_id == receipt.warehouse_id
        assert created.supplier_id == receipt.supplier_id

    def test_the_detail_screen_adds_and_removes_a_line(
        self, receipt: GoodsReceipt, keeper: User, client: Client
    ) -> None:
        supplier_return = create_supplier_return(
            receipt=receipt,
            created_by=keeper,
            returned_at=RETURNED,
            evidence_reference="وصل",
        )
        client.force_login(keeper)
        added = client.post(
            reverse("procurement:supplier_return_detail", args=[supplier_return.pk]),
            {
                "receipt_line": receipt.lines.get().pk,
                "returned_base_quantity": "10.000",
            },
        )
        assert added.status_code == 302
        line = supplier_return.lines.get()
        assert line.returned_base_quantity == Decimal("10.000")

        removed = client.post(
            reverse(
                "procurement:supplier_return_line_delete",
                args=[supplier_return.pk, line.pk],
            )
        )
        assert removed.status_code == 302
        assert supplier_return.lines.count() == 0

    def test_the_command_routes_post_and_refuse_get(
        self, receipt: GoodsReceipt, keeper: User, client: Client
    ) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        client.force_login(keeper)

        refused = client.get(reverse("procurement:supplier_return_post", args=[supplier_return.pk]))
        assert refused.status_code == 405
        supplier_return.refresh_from_db()
        assert supplier_return.status == SupplierReturnStatus.DRAFT

        posted = client.post(reverse("procurement:supplier_return_post", args=[supplier_return.pk]))
        assert posted.status_code == 302
        supplier_return.refresh_from_db()
        assert supplier_return.status == SupplierReturnStatus.POSTED

    def test_a_storekeeper_cannot_reverse_through_the_route(
        self, receipt: GoodsReceipt, keeper: User, client: Client
    ) -> None:
        """The same separation the receipt draws: undoing a posted movement is elevated."""
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=supplier_return, actor=keeper)

        client.force_login(keeper)
        response = client.post(
            reverse("procurement:supplier_return_reverse", args=[supplier_return.pk]),
            {"reason": "محاولة"},
        )
        assert response.status_code == 403
        supplier_return.refresh_from_db()
        assert supplier_return.status == SupplierReturnStatus.POSTED

    def test_a_manager_reverses_through_the_route(
        self, receipt: GoodsReceipt, keeper: User, manager: User, client: Client
    ) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=supplier_return, actor=keeper)

        client.force_login(manager)
        response = client.post(
            reverse("procurement:supplier_return_reverse", args=[supplier_return.pk]),
            {"reason": "فحص المورد أكّد سلامة الكمية"},
        )
        assert response.status_code == 302
        supplier_return.refresh_from_db()
        assert supplier_return.status == SupplierReturnStatus.REVERSED

    def test_out_of_scope_is_404_not_403(
        self, receipt: GoodsReceipt, keeper: User, client: Client
    ) -> None:
        """A 403 would confirm the return exists, and ids are sequential."""
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        outsider = User.objects.create_user(username="outsider", password=PASSWORD)
        rival = create_organization(code="RIV", name_ar="منافس", name_en="Rival")
        grant_organization_access(user=outsider, organization=rival, role=Role.MANAGER)
        outsider = User.objects.get(pk=outsider.pk)

        client.force_login(outsider)
        response = client.get(
            reverse("procurement:supplier_return_detail", args=[supplier_return.pk])
        )
        assert response.status_code == 404

    def test_the_navigation_names_the_returns_screen(self) -> None:
        """
        The entry inventory gave up ("supplier returns belong to Procurement")
        is live and points here, not at inventory.
        """
        from apps.core.navigation import MODULES

        procurement = next(m for m in MODULES if m.key == "procurement")
        section = next(s for s in procurement.sections if str(s.label) == "مرتجعات الموردين")
        assert section.available is True
        assert section.url_name == "procurement:supplier_return_list"
        inventory = next(m for m in MODULES if m.key == "inventory")
        assert all(str(s.label) != "مرتجعات الموردين" for s in inventory.sections)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


class TestTheApi:
    def test_the_command_endpoints_drive_the_lifecycle(
        self, receipt: GoodsReceipt, keeper: User, manager: User, client: Client
    ) -> None:
        client.force_login(keeper)
        created = client.post(
            "/api/v1/procurement/supplier-returns/",
            data={
                "receipt_id": receipt.pk,
                "returned_at": "2026-03-05",
                "reason": "تلف",
                "evidence_reference": "وصل السائق",
            },
            content_type="application/json",
        )
        assert created.status_code == 201
        return_id = created.json()["id"]

        lined = client.post(
            f"/api/v1/procurement/supplier-returns/{return_id}/lines/",
            data={
                "receipt_line_id": receipt.lines.get().pk,
                "returned_base_quantity": "10.000",
                "expected_credit_value": "14000.000",
            },
            content_type="application/json",
        )
        assert lined.status_code == 201

        posted = client.post(f"/api/v1/procurement/supplier-returns/{return_id}/post/")
        assert posted.status_code == 200
        payload = posted.json()
        assert payload["status"] == "POSTED"
        assert payload["journal_entry"]
        assert payload["lines"][0]["movement_id"] is not None

        client.force_login(manager)
        reversed_response = client.post(
            f"/api/v1/procurement/supplier-returns/{return_id}/reverse/",
            data={"reason": "أُعيدت إلى المخزن"},
            content_type="application/json",
        )
        assert reversed_response.status_code == 200
        assert reversed_response.json()["status"] == "REVERSED"

    def test_money_crosses_the_wire_as_strings(
        self, receipt: GoodsReceipt, keeper: User, manager: User, client: Client
    ) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        post_supplier_return(supplier_return=supplier_return, actor=keeper)

        client.force_login(manager)
        raw = client.get(
            f"/api/v1/procurement/supplier-returns/{supplier_return.pk}/"
        ).content.decode()
        assert '"posted_value": "14000.000"' in raw or '"posted_value":"14000.000"' in raw
        assert "14000.0," not in raw

    def test_the_api_omits_cost_without_the_permission(
        self, receipt: GoodsReceipt, keeper: User, client: Client
    ) -> None:
        """
        A storekeeper sends goods back and must never see what they cost.
        Omitted, never blanked — a null field says a number exists and you are
        not trusted with it, which is a different statement.
        """
        supplier_return = _draft_return(receipt=receipt, keeper=keeper, expected="14000.000")
        post_supplier_return(supplier_return=supplier_return, actor=keeper)

        keeper = User.objects.get(pk=keeper.pk)
        assert not keeper.has_perm("procurement.view_supplier_cost")
        client.force_login(keeper)
        payload = client.get(f"/api/v1/procurement/supplier-returns/{supplier_return.pk}/").json()
        assert "posted_value" not in payload or payload["posted_value"] is None
        line = payload["lines"][0]
        assert line.get("posted_value") is None
        assert line.get("expected_credit_value") is None
        # The quantity is custody, not money, and stays visible.
        assert line["returned_base_quantity"] == "10.000"

    def test_a_draft_can_be_discarded_and_a_line_removed(
        self, receipt: GoodsReceipt, keeper: User, client: Client
    ) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        line = supplier_return.lines.get()

        client.force_login(keeper)
        unlined = client.delete(
            f"/api/v1/procurement/supplier-returns/{supplier_return.pk}/lines/{line.pk}/"
        )
        assert unlined.status_code == 204
        assert supplier_return.lines.count() == 0

        discarded = client.delete(f"/api/v1/procurement/supplier-returns/{supplier_return.pk}/")
        assert discarded.status_code == 204
        assert not SupplierReturn.objects.filter(pk=supplier_return.pk).exists()

    def test_out_of_scope_is_404(self, receipt: GoodsReceipt, keeper: User, client: Client) -> None:
        supplier_return = _draft_return(receipt=receipt, keeper=keeper)
        outsider = User.objects.create_user(username="api-outsider", password=PASSWORD)
        rival = create_organization(code="RIV2", name_ar="منافس", name_en="Rival Two")
        grant_organization_access(user=outsider, organization=rival, role=Role.MANAGER)
        outsider = User.objects.get(pk=outsider.pk)

        client.force_login(outsider)
        response = client.get(f"/api/v1/procurement/supplier-returns/{supplier_return.pk}/")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


class TestDemoReturns:
    def test_the_seed_skips_what_is_missing(
        self, organization: Organization, keeper: User, manager: User
    ) -> None:
        """
        No procurement demo has run against this organization, so the seed
        finds no posted demo delivery and returns nothing rather than
        inventing one.
        """
        from apps.procurement.demo import seed_demo_returns

        assert (
            seed_demo_returns(organization=organization, storekeeper=keeper, manager=manager) == []
        )
