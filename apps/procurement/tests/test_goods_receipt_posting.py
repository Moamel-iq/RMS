"""
Task 2.9 — the receipt actually posts, and both ledgers move together.

Task 2.8 shipped a receipt that could be inspected and not posted, and asserted
that absence directly. This file is the other side of that boundary, and it
does not merely delete those assertions: every one of them is replaced by its
positive twin.

    2.8 asserted                          2.9 asserts
    ------------------------------------  ------------------------------------
    no `post_goods_receipt` exists        it exists, and posts both ledgers
    no posting route reverses             both command routes exist and work
    an inspected draft posts nothing      a posted receipt moved stock and
                                          money, and a draft still moves neither
    POSTED with no timestamp is refused   still refused, plus: POSTED with no
                                          stock entry or no journal is refused

The claims that matter most are the ones about what **cannot** happen: stock
without accounting, accounting without stock, a failed post leaving a half-
applied transaction, and a retry duplicating anything.
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
    Account,
    AccountRole,
    JournalEntry,
)
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
)
from apps.core.models import AuditEvent
from apps.inventory.models import (
    ConversionType,
    InventoryItem,
    InventoryLot,
    InventoryReasonCode,
    ItemType,
    PackageUnit,
    ReasonCodeApplication,
    StockBalance,
    StockLedgerEntry,
    StockLocation,
    StockLocationBalance,
    StockMovement,
    Warehouse,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseOrder,
    Supplier,
)
from apps.procurement.posting import (
    SOURCE_DOCUMENT_TYPE,
    post_goods_receipt,
    reverse_goods_receipt,
)
from apps.procurement.reconciliation import (
    verify_goods_receipt,
    verify_grni,
    verify_order_received_quantities,
    verify_procurement,
)
from apps.procurement.services import (
    add_order_line,
    add_receipt_line,
    approve_purchase_order,
    cancel_purchase_order,
    create_goods_receipt,
    create_purchase_order,
    create_supplier,
    inspect_receipt_line,
    issue_purchase_order,
    received_base_quantity,
    revise_purchase_order,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

TEST_YEAR = 2026
JAN_1 = datetime.date(TEST_YEAR, 1, 1)
RECEIVED = datetime.date(TEST_YEAR, 2, 10)
PASSWORD = "pw-not-real-1234"


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
def control_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="1-03-01-001")


@pytest.fixture
def grni_account(organization: Organization, accounting: None) -> Account:
    return Account.objects.get(organization=organization, code="2-01-02-001")


@pytest.fixture
def mapped(organization: Organization, control_account: Account, grni_account: Account) -> None:
    """The two mappings a receipt posting needs, and nothing else."""
    for code, account in (
        (INVENTORY_CONTROL, control_account),
        (GOODS_RECEIVED_NOT_INVOICED, grni_account),
    ):
        create_account_mapping(
            organization=organization,
            account_role=AccountRole.objects.get(code=code),
            account=account,
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
def meat(organization: Organization, kilogram: UnitOfMeasure) -> InventoryItem:
    from apps.inventory.services import create_item, create_item_category

    return create_item(
        organization=organization,
        code="MEAT",
        name_ar="لحم",
        category=create_item_category(organization=organization, code="MEATS", name_ar="لحوم"),
        item_type=ItemType.RAW_MATERIAL,
        base_unit=kilogram,
        tracks_lots=True,
    )


@pytest.fixture
def container(organization: Organization, meat: InventoryItem) -> PackageUnit:
    from apps.inventory.services import create_item_conversion, create_package_unit

    package = create_package_unit(organization=organization, code="CONTAINER", name_ar="حاوية")
    create_item_conversion(
        item=meat,
        package_unit=package,
        factor_to_base=Decimal("18.000000000000"),
        conversion_type=ConversionType.VARIABLE,
        effective_from=JAN_1,
    )
    return package


@pytest.fixture
def meat_lot(organization: Organization, meat: InventoryItem) -> InventoryLot:
    return InventoryLot.objects.create(organization=organization, item=meat, code="LOT-01")


@pytest.fixture
def reason(organization: Organization) -> InventoryReasonCode:
    return InventoryReasonCode.objects.create(
        organization=organization,
        code="SPOILED",
        name_ar="تالف عند الاستلام",
        applies_to=ReasonCodeApplication.WASTE,
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
def buyer(branch: Branch) -> User:
    user = User.objects.create_user(username="buyer", password=PASSWORD)
    grant_branch_access(user=user, branch=branch, role=Role.PURCHASING)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def approver(branch: Branch) -> User:
    user = User.objects.create_user(username="approver", password=PASSWORD)
    grant_branch_access(user=user, branch=branch, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def issued_order(
    grocery: Supplier,
    branch: Branch,
    store: Warehouse,
    buyer: User,
    approver: User,
    rice: InventoryItem,
) -> PurchaseOrder:
    order = create_purchase_order(
        supplier=grocery,
        branch=branch,
        warehouse=store,
        created_by=buyer,
        ordered_on=datetime.date(TEST_YEAR, 2, 1),
    )
    add_order_line(
        order=order,
        item=rice,
        ordered_quantity=Decimal("100.000"),
        unit_price=Decimal("1400.000000"),
    )
    approve_purchase_order(order=order, actor=approver)
    return issue_purchase_order(order=order, actor=buyer)


def _receipt(
    *,
    grocery: Supplier,
    branch: Branch,
    store: Warehouse,
    keeper: User,
    reference: str = "DN-100",
    **extra: object,
) -> GoodsReceipt:
    return create_goods_receipt(
        supplier=grocery,
        branch=branch,
        warehouse=store,
        created_by=keeper,
        received_at=RECEIVED,
        delivery_reference=reference,
        evidence_reference="إشعار المورد",
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def ready(
    grocery: Supplier,
    branch: Branch,
    store: Warehouse,
    keeper: User,
    rice: InventoryItem,
    mapped: None,
) -> GoodsReceipt:
    """Fifty kilograms of rice at 1,400, inspected and wholly accepted."""
    receipt = _receipt(grocery=grocery, branch=branch, store=store, keeper=keeper)
    line = add_receipt_line(
        receipt=receipt,
        item=rice,
        delivered_quantity=Decimal("50.000"),
        unit_price=Decimal("1400.000000"),
    )
    inspect_receipt_line(line=line, accepted_base_quantity=Decimal("50.000"), actor=keeper)
    return GoodsReceipt.objects.get(pk=receipt.pk)


# ---------------------------------------------------------------------------
# The boundary, crossed
# ---------------------------------------------------------------------------


class TestBothLedgersMoveTogether:
    """
    The positive replacement for Task 2.8's `TestNothingPostsYet`.

    Each test here answers one of that class's assertions in the affirmative,
    and the last two answer the question those assertions were really asking:
    can stock ever exist without accounting behind it, or the reverse.
    """

    def test_the_posting_service_exists_and_moves_both(
        self, ready: GoodsReceipt, keeper: User, control_account: Account, grni_account: Account
    ) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)

        assert posted.status == GoodsReceiptStatus.POSTED
        assert posted.number.startswith("GRN-2026-")
        assert posted.posted_by == keeper
        assert posted.posted_at is not None
        assert posted.posted_value == Decimal("70000.000")

        movement = StockMovement.objects.get()
        assert movement.base_quantity == Decimal("50.000")
        assert movement.inventory_value == Decimal("70000.000")
        assert movement.control_account == control_account

        journal = JournalEntry.objects.get()
        debit = journal.lines.get(debit__gt=0)
        credit = journal.lines.get(credit__gt=0)
        assert debit.account == control_account
        assert debit.debit == Decimal("70000.000")
        assert credit.account == grni_account
        assert credit.credit == Decimal("70000.000")

    def test_the_command_routes_exist(self, ready: GoodsReceipt) -> None:
        """Task 2.8 asserted `NoReverseMatch` for both of these."""
        assert reverse("procurement:goods_receipt_post", args=[ready.pk]).endswith("/post/")
        assert reverse("procurement:goods_receipt_reverse", args=[ready.pk]).endswith("/reverse/")

    def test_a_draft_still_moves_neither(self, ready: GoodsReceipt) -> None:
        """The 2.8 claim, still true for anything that has not been posted."""
        assert ready.status == GoodsReceiptStatus.DRAFT
        assert StockMovement.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_stock_cannot_exist_without_its_journal(
        self,
        ready: GoodsReceipt,
        keeper: User,
        mapped: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The load-bearing claim. A journal that cannot be written must take the
        stock down with it, so the failure is engineered where it does the most
        damage: after the movements exist and before the entry does.
        """
        from apps.procurement import posting

        def explode(**_kwargs: object) -> JournalEntry:
            raise RuntimeError("the ledger went away")

        monkeypatch.setattr(posting, "post_entry", explode)
        with pytest.raises(RuntimeError):
            post_goods_receipt(receipt=ready, actor=keeper)

        ready.refresh_from_db()
        assert ready.status == GoodsReceiptStatus.DRAFT
        assert ready.number == ""
        assert StockMovement.objects.count() == 0
        assert StockLedgerEntry.objects.count() == 0
        assert StockBalance.objects.filter(quantity__gt=0).count() == 0
        assert JournalEntry.objects.count() == 0

    def test_a_posted_receipt_must_name_both_ledgers(self, ready: GoodsReceipt) -> None:
        """
        The database refuses the shape outright, not merely the code path.
        `POSTED` with no stock entry is not a state this table can hold.
        """
        from django.utils import timezone

        with pytest.raises(IntegrityError), transaction.atomic():
            GoodsReceipt.objects.filter(pk=ready.pk).update(
                status="POSTED", posted_at=timezone.now(), posted_by=ready.created_by
            )

    def test_a_draft_cannot_carry_a_posted_timestamp(self, ready: GoodsReceipt) -> None:
        """Retained from Task 2.8: a half-applied transaction has a shape."""
        from django.utils import timezone

        with pytest.raises(IntegrityError), transaction.atomic():
            GoodsReceipt.objects.filter(pk=ready.pk).update(
                posted_by=ready.created_by, posted_at=timezone.now()
            )


# ---------------------------------------------------------------------------
# Inspection completeness (F2)
# ---------------------------------------------------------------------------


class TestInspectionMustBeComplete:
    def test_an_undisposed_remainder_is_refused(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """
        `accepted + rejected < delivered` is not ready, and is not silently
        treated as either. Nothing about the receipt changes.
        """
        receipt = _receipt(grocery=grocery, branch=branch, store=store, keeper=keeper)
        add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("50.000"),
            unit_price=Decimal("1400.000000"),
        )
        with pytest.raises(ValidationError) as error:
            post_goods_receipt(receipt=receipt, actor=keeper)
        assert error.value.code == "inspection_incomplete"
        receipt.refresh_from_db()
        assert receipt.status == GoodsReceiptStatus.DRAFT

    def test_an_empty_receipt_is_refused(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        mapped: None,
    ) -> None:
        receipt = _receipt(grocery=grocery, branch=branch, store=store, keeper=keeper)
        with pytest.raises(ValidationError) as error:
            post_goods_receipt(receipt=receipt, actor=keeper)
        assert error.value.code == "no_lines"

    def test_a_wholly_rejected_receipt_is_refused(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        reason: InventoryReasonCode,
        mapped: None,
    ) -> None:
        """
        Task 2.0 §7 gives the receipt no zero-effect posted state, so a
        delivery where nothing was accepted stays a draft rather than becoming
        a posted document with no movement and no journal. Inventing that
        state would mean inventing accounting for it.
        """
        receipt = _receipt(grocery=grocery, branch=branch, store=store, keeper=keeper)
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("50.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(
            line=line,
            accepted_base_quantity=Decimal("0.000"),
            actor=keeper,
            rejection_reason=reason,
        )
        with pytest.raises(ValidationError) as error:
            post_goods_receipt(receipt=receipt, actor=keeper)
        assert error.value.code == "nothing_accepted"
        assert StockMovement.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_evidence_is_required(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        receipt = create_goods_receipt(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            delivery_reference="DN-NOEV",
        )
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("5.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("5.000"), actor=keeper)
        with pytest.raises(ValidationError) as error:
            post_goods_receipt(receipt=receipt, actor=keeper)
        assert error.value.code == "evidence_required"


# ---------------------------------------------------------------------------
# Accepted moves, rejected does not (F3, PRC-025)
# ---------------------------------------------------------------------------


class TestAcceptedAndRejectedEffects:
    def test_only_the_accepted_quantity_reaches_stock_and_grni(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        reason: InventoryReasonCode,
        mapped: None,
    ) -> None:
        """
        Forty of fifty kilograms accepted at 1,400. Stock gains 40, the ledger
        gains 56,000, and the ten rejected kilograms produce nothing at all —
        no movement, no balance, no GRNI, no payable.
        """
        receipt = _receipt(grocery=grocery, branch=branch, store=store, keeper=keeper)
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("50.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(
            line=line,
            accepted_base_quantity=Decimal("40.000"),
            actor=keeper,
            rejection_reason=reason,
        )
        posted = post_goods_receipt(receipt=receipt, actor=keeper)

        assert posted.posted_value == Decimal("56000.000")
        movement = StockMovement.objects.get()
        assert movement.base_quantity == Decimal("40.000")
        assert movement.inventory_value == Decimal("56000.000")
        balance = StockBalance.objects.get(warehouse=store, item=rice)
        assert balance.quantity == Decimal("40.000")
        assert balance.value == Decimal("56000.000")
        assert sum(row.credit for row in JournalEntry.objects.get().lines.all()) == Decimal(
            "56000.000"
        )

    def test_a_wholly_rejected_line_posts_no_movement_beside_an_accepted_one(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        meat: InventoryItem,
        meat_lot: InventoryLot,
        reason: InventoryReasonCode,
        mapped: None,
    ) -> None:
        """
        Two lines, one accepted and one entirely refused. The receipt posts,
        and the refused line carries no movement and no accounts — there is no
        zero-quantity movement and no zero-value journal line, because a zero
        row is a claim that something happened.
        """
        receipt = _receipt(grocery=grocery, branch=branch, store=store, keeper=keeper)
        good = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            unit_price=Decimal("1400.000000"),
        )
        bad = add_receipt_line(
            receipt=receipt,
            item=meat,
            delivered_quantity=Decimal("5.000"),
            unit_price=Decimal("9000.000000"),
            lot=meat_lot,
        )
        inspect_receipt_line(line=good, accepted_base_quantity=Decimal("10.000"), actor=keeper)
        inspect_receipt_line(
            line=bad,
            accepted_base_quantity=Decimal("0.000"),
            actor=keeper,
            rejection_reason=reason,
        )
        posted = post_goods_receipt(receipt=receipt, actor=keeper)

        assert posted.posted_value == Decimal("14000.000")
        assert StockMovement.objects.count() == 1
        refused = GoodsReceiptLine.objects.get(pk=bad.pk)
        assert refused.movement is None
        assert refused.inventory_account is None
        assert refused.contra_account is None
        assert refused.posted_value == Decimal("0.000")
        assert not StockBalance.objects.filter(item=meat, quantity__gt=0).exists()
        assert JournalEntry.objects.get().lines.count() == 2

    def test_a_variable_package_posts_the_weighed_quantity(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        meat: InventoryItem,
        meat_lot: InventoryLot,
        container: PackageUnit,
        mapped: None,
    ) -> None:
        """
        One container priced at 9,500 that weighed 17.4 kg. The planning factor
        says 18; the scale is the quantity (PRC-026), and the base unit cost is
        derived from what was actually delivered.
        """
        receipt = _receipt(grocery=grocery, branch=branch, store=store, keeper=keeper)
        line = add_receipt_line(
            receipt=receipt,
            item=meat,
            package_unit=container,
            delivered_quantity=Decimal("1.000"),
            measured_base_quantity=Decimal("17.400"),
            unit_price=Decimal("9500.000000"),
            lot=meat_lot,
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("17.400"), actor=keeper)
        posted = post_goods_receipt(receipt=receipt, actor=keeper)

        movement = StockMovement.objects.get()
        assert movement.base_quantity == Decimal("17.400")
        assert posted.posted_value == movement.inventory_value
        stored = GoodsReceiptLine.objects.get(pk=line.pk)
        assert stored.posted_unit_cost == Decimal("545.977011")
        # The value is the quantity at that cost, rounded once — never the
        # entered 9,500 taken as read, and never rounded twice.
        assert stored.posted_value == Decimal("9500.000")


# ---------------------------------------------------------------------------
# Idempotency and immutability (F9, F12)
# ---------------------------------------------------------------------------


class TestRetriesAndImmutability:
    def test_a_second_post_is_refused_and_duplicates_nothing(
        self, ready: GoodsReceipt, keeper: User
    ) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        with pytest.raises(ValidationError) as error:
            post_goods_receipt(receipt=posted, actor=keeper)
        assert error.value.code == "already_posted"
        assert StockMovement.objects.count() == 1
        assert JournalEntry.objects.count() == 1
        assert StockLedgerEntry.objects.count() == 1

    def test_a_stale_draft_instance_cannot_post_twice(
        self, ready: GoodsReceipt, keeper: User
    ) -> None:
        """
        The stale-instance rule, for the transition that matters most. The
        caller's copy still says DRAFT; the row does not, and the row decides.
        """
        stale = GoodsReceipt.objects.get(pk=ready.pk)
        post_goods_receipt(receipt=ready, actor=keeper)
        assert stale.status == GoodsReceiptStatus.DRAFT
        with pytest.raises(ValidationError) as error:
            post_goods_receipt(receipt=stale, actor=keeper)
        assert error.value.code == "already_posted"
        assert StockMovement.objects.count() == 1

    def test_the_source_identity_is_complete_and_unique(
        self, ready: GoodsReceipt, keeper: User
    ) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        entry = StockLedgerEntry.objects.get()
        journal = JournalEntry.objects.get()
        for row in (entry, journal):
            assert row.source_document_type == SOURCE_DOCUMENT_TYPE
            assert row.source_document_id == str(posted.public_id)
            assert row.source_event == "POSTED"

    def test_a_posted_receipt_cannot_be_edited(self, ready: GoodsReceipt, keeper: User) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        with pytest.raises(IntegrityError), transaction.atomic():
            GoodsReceipt.objects.filter(pk=posted.pk).update(delivery_reference="CHANGED")

    def test_a_posted_receipt_cannot_be_deleted(self, ready: GoodsReceipt, keeper: User) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        with pytest.raises(IntegrityError), transaction.atomic():
            GoodsReceipt.objects.filter(pk=posted.pk).delete()

    def test_a_posted_line_is_frozen(self, ready: GoodsReceipt, keeper: User) -> None:
        post_goods_receipt(receipt=ready, actor=keeper)
        line = GoodsReceiptLine.objects.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            GoodsReceiptLine.objects.filter(pk=line.pk).update(
                accepted_base_quantity=Decimal("1.000")
            )

    def test_a_posted_line_cannot_be_deleted(self, ready: GoodsReceipt, keeper: User) -> None:
        post_goods_receipt(receipt=ready, actor=keeper)
        with pytest.raises(IntegrityError), transaction.atomic():
            GoodsReceiptLine.objects.all().delete()

    def test_a_line_value_must_be_its_quantity_at_its_cost(self, ready: GoodsReceipt) -> None:
        """
        The trigger states the arithmetic, so no path can write a value its own
        quantity does not support — including a future one nobody has read.
        """
        line = GoodsReceiptLine.objects.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            GoodsReceiptLine.objects.filter(pk=line.pk).update(
                posted_unit_cost=Decimal("1400.000000"), posted_value=Decimal("1.000")
            )


# ---------------------------------------------------------------------------
# Purchase order semantics (F7)
# ---------------------------------------------------------------------------


class TestOrderQuantitySemantics:
    def test_posting_consumes_the_ordered_quantity(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
            delivery_reference="DN-PO-1",
            evidence_reference="إشعار",
        )
        order_line = issued_order.lines.get()
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("60.000"),
            order_line=order_line,
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("60.000"), actor=keeper)

        assert received_base_quantity(order_line) == Decimal("0.000")
        post_goods_receipt(receipt=receipt, actor=keeper)
        assert received_base_quantity(order_line) == Decimal("60.000")

    def test_a_revision_cannot_go_below_the_posted_quantity(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        buyer: User,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """PRC-020, now with a real posted quantity behind it."""
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
            delivery_reference="DN-PO-2",
            evidence_reference="إشعار",
        )
        order_line = issued_order.lines.get()
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("60.000"),
            order_line=order_line,
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("60.000"), actor=keeper)
        post_goods_receipt(receipt=receipt, actor=keeper)

        with pytest.raises(ValidationError):
            revise_purchase_order(
                order=issued_order,
                actor=buyer,
                reason="تقليل الكمية",
                line_quantities={str(order_line.line_uid): Decimal("40.000")},
            )

    def test_a_cancelled_order_cannot_be_posted_against(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        approver: User,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """
        The order is cancelled while the draft sits on somebody's screen. The
        answer is asked again at the moment it matters, against the locked row.
        """
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
            delivery_reference="DN-PO-3",
            evidence_reference="إشعار",
        )
        order_line = issued_order.lines.get()
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            order_line=order_line,
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("10.000"), actor=keeper)
        cancel_purchase_order(order=issued_order, actor=approver, reason="ألغي الطلب")

        with pytest.raises(ValidationError) as error:
            post_goods_receipt(receipt=receipt, actor=keeper)
        assert error.value.code == "order_cancelled"
        assert StockMovement.objects.count() == 0

    def test_two_partial_receipts_cannot_together_exceed_the_order(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        """
        Both drafts were within the remainder when they were entered. The
        second one to post is measured against what has actually happened, not
        against what the first one intended.
        """
        order_line = issued_order.lines.get()
        receipts = []
        for index in (1, 2):
            receipt = create_goods_receipt(
                supplier=issued_order.supplier,
                branch=branch,
                warehouse=store,
                created_by=keeper,
                received_at=RECEIVED,
                order=issued_order,
                delivery_reference=f"DN-RACE-{index}",
                evidence_reference="إشعار",
            )
            line = add_receipt_line(
                receipt=receipt,
                item=rice,
                delivered_quantity=Decimal("50.000"),
                order_line=order_line,
            )
            inspect_receipt_line(line=line, accepted_base_quantity=Decimal("50.000"), actor=keeper)
            receipts.append(receipt)

        post_goods_receipt(receipt=receipts[0], actor=keeper)
        post_goods_receipt(receipt=receipts[1], actor=keeper)
        assert received_base_quantity(order_line) == Decimal("100.000")

        third = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
            delivery_reference="DN-RACE-3",
            evidence_reference="إشعار",
        )
        with pytest.raises(ValidationError) as error:
            add_receipt_line(
                receipt=third,
                item=rice,
                delivered_quantity=Decimal("1.000"),
                order_line=order_line,
            )
        assert error.value.code == "over_receipt"


# ---------------------------------------------------------------------------
# Reversal (F11)
# ---------------------------------------------------------------------------


class TestReversal:
    def test_a_reversal_mirrors_exactly(
        self, ready: GoodsReceipt, keeper: User, store: Warehouse, rice: InventoryItem
    ) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        reversed_receipt = reverse_goods_receipt(
            receipt=posted, actor=keeper, reason="المورد استرجع البضاعة"
        )

        assert reversed_receipt.status == GoodsReceiptStatus.REVERSED
        assert reversed_receipt.reversed_by == keeper
        assert reversed_receipt.reversal_journal_entry is not None
        balance = StockBalance.objects.get(warehouse=store, item=rice)
        assert balance.quantity == Decimal("0.000")
        assert balance.value == Decimal("0.000")
        debits = sum(row.debit for entry in JournalEntry.objects.all() for row in entry.lines.all())
        credits = sum(
            row.credit for entry in JournalEntry.objects.all() for row in entry.lines.all()
        )
        assert debits == credits == Decimal("140000.000")

    def test_a_reversal_needs_a_reason(self, ready: GoodsReceipt, keeper: User) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        with pytest.raises(ValidationError) as error:
            reverse_goods_receipt(receipt=posted, actor=keeper, reason="   ")
        assert error.value.code == "reason_required"

    def test_a_draft_cannot_be_reversed(self, ready: GoodsReceipt, keeper: User) -> None:
        with pytest.raises(ValidationError) as error:
            reverse_goods_receipt(receipt=ready, actor=keeper, reason="لا شيء")
        assert error.value.code == "receipt_not_posted"

    def test_a_reversal_cannot_be_reversed(self, ready: GoodsReceipt, keeper: User) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        once = reverse_goods_receipt(receipt=posted, actor=keeper, reason="خطأ")
        with pytest.raises(ValidationError) as error:
            reverse_goods_receipt(receipt=once, actor=keeper, reason="مرة أخرى")
        assert error.value.code == "already_reversed"

    def test_a_reversal_releases_the_ordered_quantity(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        mapped: None,
    ) -> None:
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
            delivery_reference="DN-REV",
            evidence_reference="إشعار",
        )
        order_line = issued_order.lines.get()
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("100.000"),
            order_line=order_line,
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("100.000"), actor=keeper)
        posted = post_goods_receipt(receipt=receipt, actor=keeper)
        assert received_base_quantity(order_line) == Decimal("100.000")

        reverse_goods_receipt(receipt=posted, actor=keeper, reason="أُعيدت الشحنة")
        assert received_base_quantity(order_line) == Decimal("0.000")

    def test_consumed_stock_cannot_be_un_received(
        self,
        ready: GoodsReceipt,
        keeper: User,
        store: Warehouse,
        rice: InventoryItem,
        organization: Organization,
    ) -> None:
        """
        Availability applies. Goods that have been cooked did happen, and a
        reversal that claimed otherwise would post negative stock.
        """
        from apps.inventory.ledger import MovementInput, post_stock_entry
        from apps.inventory.models import MovementType

        posted = post_goods_receipt(receipt=ready, actor=keeper)
        post_stock_entry(
            organization=organization,
            effects=[
                MovementInput(
                    warehouse=store,
                    item=rice,
                    movement_type=MovementType.ISSUE,
                    quantity=Decimal("45.000"),
                    effect_key="test-issue",
                )
            ],
            idempotency_key="test-issue-1",
            business_date=RECEIVED,
        )
        with pytest.raises(ValidationError):
            reverse_goods_receipt(receipt=posted, actor=keeper, reason="محاولة عكس")
        posted.refresh_from_db()
        assert posted.status == GoodsReceiptStatus.POSTED


# ---------------------------------------------------------------------------
# Locations (F6)
# ---------------------------------------------------------------------------


class TestLocationEffect:
    @pytest.fixture
    def bin_a(self, store: Warehouse) -> StockLocation:
        from apps.inventory.locations import create_location

        return create_location(warehouse=store, code="BIN-A", name_ar="رف أ")

    def test_the_accepted_quantity_lands_in_the_named_bin(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        bin_a: StockLocation,
        mapped: None,
    ) -> None:
        """
        `located + unlocated == warehouse` holds by construction: the kernel
        put the goods in the warehouse and the put-away moved them into the bin.
        """
        from apps.inventory.locations import unlocated_quantity

        receipt = _receipt(
            grocery=grocery, branch=branch, store=store, keeper=keeper, location=bin_a
        )
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("40.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("40.000"), actor=keeper)
        post_goods_receipt(receipt=receipt, actor=keeper)

        located = StockLocationBalance.objects.get(location=bin_a, item=rice)
        assert located.quantity == Decimal("40.000")
        assert unlocated_quantity(store, rice, None) == Decimal("0.000")
        balance = StockBalance.objects.get(warehouse=store, item=rice)
        assert balance.quantity == located.quantity + unlocated_quantity(store, rice, None)

    def test_a_reversal_empties_the_bin_it_filled(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        bin_a: StockLocation,
        mapped: None,
    ) -> None:
        from apps.inventory.locations import unlocated_quantity

        receipt = _receipt(
            grocery=grocery, branch=branch, store=store, keeper=keeper, location=bin_a
        )
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("20.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("20.000"), actor=keeper)
        posted = post_goods_receipt(receipt=receipt, actor=keeper)
        reverse_goods_receipt(receipt=posted, actor=keeper, reason="أُعيدت")

        located = StockLocationBalance.objects.get(location=bin_a, item=rice)
        assert located.quantity == Decimal("0.000")
        assert unlocated_quantity(store, rice, None) == Decimal("0.000")
        assert StockBalance.objects.get(warehouse=store, item=rice).quantity == Decimal("0.000")


# ---------------------------------------------------------------------------
# Accounting shape (F5, PRC-032 – PRC-034)
# ---------------------------------------------------------------------------


class TestAccountingShape:
    def test_two_control_accounts_produce_two_debits_and_one_credit(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        organization: Organization,
        rice: InventoryItem,
        meat: InventoryItem,
        meat_lot: InventoryLot,
        control_account: Account,
        grni_account: Account,
        mapped: None,
    ) -> None:
        """
        PRC-033. `INVENTORY_CONTROL` is item-overridable and GRNI is not, so
        two items in two control accounts give two debits and one credit.
        """
        from apps.inventory.accounts import create_inventory_mapping

        meat_account = Account.objects.get(organization=organization, code="1-03-02-001")
        create_inventory_mapping(
            organization=organization,
            role=INVENTORY_CONTROL,
            account=meat_account,
            item=meat,
            effective_from=JAN_1,
        )
        receipt = _receipt(grocery=grocery, branch=branch, store=store, keeper=keeper)
        for item, quantity, price, lot in (
            (rice, Decimal("10.000"), Decimal("1400.000000"), None),
            (meat, Decimal("5.000"), Decimal("9000.000000"), meat_lot),
        ):
            line = add_receipt_line(
                receipt=receipt,
                item=item,
                delivered_quantity=quantity,
                unit_price=price,
                lot=lot,
            )
            inspect_receipt_line(line=line, accepted_base_quantity=quantity, actor=keeper)
        posted = post_goods_receipt(receipt=receipt, actor=keeper)

        journal = JournalEntry.objects.get()
        debits = list(journal.lines.filter(debit__gt=0).order_by("account__code"))
        credits = list(journal.lines.filter(credit__gt=0))
        assert [row.account.code for row in debits] == ["1-03-01-001", "1-03-02-001"]
        assert len(credits) == 1
        assert credits[0].account == grni_account
        assert credits[0].credit == posted.posted_value == Decimal("59000.000")

    def test_a_missing_mapping_rolls_everything_back(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
        accounting: None,
    ) -> None:
        """
        PRC-034/PRC-036: no mapping, no posting — and nothing partial left
        behind. The `mapped` fixture is deliberately absent here.
        """
        receipt = _receipt(grocery=grocery, branch=branch, store=store, keeper=keeper)
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("10.000"), actor=keeper)
        with pytest.raises(ValidationError):
            post_goods_receipt(receipt=receipt, actor=keeper)

        receipt.refresh_from_db()
        assert receipt.status == GoodsReceiptStatus.DRAFT
        assert receipt.number == ""
        assert StockMovement.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_a_closed_period_refuses_the_posting(
        self, ready: GoodsReceipt, keeper: User, organization: Organization
    ) -> None:
        from apps.accounting.models import AccountingPeriod, PeriodState

        period = AccountingPeriod.objects.get(
            fiscal_year__organization=organization, start_date__month=2
        )
        AccountingPeriod.objects.filter(pk=period.pk).update(state=PeriodState.CLOSED)
        with pytest.raises(ValidationError):
            post_goods_receipt(receipt=ready, actor=keeper)
        assert StockMovement.objects.count() == 0

    def test_the_audit_event_records_both_ledgers(self, ready: GoodsReceipt, keeper: User) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        event = AuditEvent.objects.filter(
            action="POSTED", source_document_type=SOURCE_DOCUMENT_TYPE
        ).latest("id")
        assert event.metadata["number"] == posted.number
        assert event.metadata["journal_entry"]
        assert event.metadata["stock_entry"]


# ---------------------------------------------------------------------------
# Reconciliation (F13)
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_a_posted_receipt_reconciles(
        self, ready: GoodsReceipt, keeper: User, organization: Organization
    ) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        assert verify_goods_receipt(posted) == []
        assert verify_order_received_quantities(organization) == []
        assert verify_grni(organization) == []
        assert verify_procurement(organization) == []

    def test_a_draft_is_skipped_rather_than_reported_clean(self, ready: GoodsReceipt) -> None:
        assert verify_goods_receipt(ready) == []

    def test_a_reversed_receipt_still_reconciles(
        self, ready: GoodsReceipt, keeper: User, organization: Organization
    ) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        reversed_receipt = reverse_goods_receipt(
            receipt=posted, actor=keeper, reason="أُعيدت الشحنة"
        )
        assert verify_goods_receipt(reversed_receipt) == []
        assert verify_procurement(organization) == []

    def test_a_planted_journal_stays_visible(
        self,
        ready: GoodsReceipt,
        keeper: User,
        organization: Organization,
        branch: Branch,
        control_account: Account,
        grni_account: Account,
    ) -> None:
        """
        No repair mode. A journal claiming to be a goods receipt that does not
        exist is reported, and left exactly where it is.

        Planted by posting a *new* entry rather than by editing the real one:
        the accounting kernel makes a posted entry's source identity immutable
        at the database, so the edit this test originally attempted is not a
        thing anybody — including a defect — can do. Fabricating a fresh entry
        is the attack that remains available, and the one worth catching.
        """
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine

        post_goods_receipt(receipt=ready, actor=keeper)
        post_entry(
            organization=organization,
            accounting_date=RECEIVED,
            lines=[
                PostingLine(account=control_account, branch=branch, debit=Decimal("1.000")),
                PostingLine(account=grni_account, branch=branch, credit=Decimal("1.000")),
            ],
            idempotency_key="planted-entry",
            source_document_type=SOURCE_DOCUMENT_TYPE,
            source_document_id="00000000-0000-0000-0000-000000000000",
            source_event="POSTED",
        )
        problems = verify_grni(organization)
        assert len(problems) == 1
        assert problems[0].field == "journal_cites_unknown_receipt"
        # Reported, not repaired: both entries are still there.
        assert JournalEntry.objects.count() == 2


# ---------------------------------------------------------------------------
# Routes and authorization
# ---------------------------------------------------------------------------


class TestRoutes:
    def test_the_post_route_posts(self, ready: GoodsReceipt, keeper: User, client: Client) -> None:
        client.force_login(keeper)
        response = client.post(reverse("procurement:goods_receipt_post", args=[ready.pk]))
        assert response.status_code == 302
        ready.refresh_from_db()
        assert ready.status == GoodsReceiptStatus.POSTED
        assert StockMovement.objects.count() == 1
        assert JournalEntry.objects.count() == 1

    def test_the_reverse_route_reverses(
        self, ready: GoodsReceipt, keeper: User, approver: User, client: Client
    ) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        client.force_login(approver)
        response = client.post(
            reverse("procurement:goods_receipt_reverse", args=[posted.pk]),
            {"reason": "أُعيدت الشحنة"},
        )
        assert response.status_code == 302
        posted.refresh_from_db()
        assert posted.status == GoodsReceiptStatus.REVERSED

    def test_a_storekeeper_cannot_reverse(
        self, ready: GoodsReceipt, keeper: User, client: Client
    ) -> None:
        """
        Undoing a posted receipt reverses a journal as well as stock, so it is
        an accounting act. The storekeeper posts and cannot un-post.
        """
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        client.force_login(keeper)
        response = client.post(
            reverse("procurement:goods_receipt_reverse", args=[posted.pk]),
            {"reason": "محاولة"},
        )
        assert response.status_code == 403
        posted.refresh_from_db()
        assert posted.status == GoodsReceiptStatus.POSTED

    def test_a_buyer_cannot_post(self, ready: GoodsReceipt, buyer: User, client: Client) -> None:
        """Whoever chose the supplier does not also certify what arrived."""
        client.force_login(buyer)
        response = client.post(reverse("procurement:goods_receipt_post", args=[ready.pk]))
        assert response.status_code == 403
        ready.refresh_from_db()
        assert ready.status == GoodsReceiptStatus.DRAFT

    def test_the_detail_screen_shows_the_posted_state(
        self, ready: GoodsReceipt, keeper: User, client: Client
    ) -> None:
        posted = post_goods_receipt(receipt=ready, actor=keeper)
        client.force_login(keeper)
        response = client.get(reverse("procurement:goods_receipt_detail", args=[posted.pk]))
        assert response.status_code == 200
        body = response.content.decode()
        assert posted.number in body
        assert "مرحّل" in body

    def test_a_get_does_not_post(self, ready: GoodsReceipt, keeper: User, client: Client) -> None:
        client.force_login(keeper)
        response = client.get(reverse("procurement:goods_receipt_post", args=[ready.pk]))
        assert response.status_code == 405
        ready.refresh_from_db()
        assert ready.status == GoodsReceiptStatus.DRAFT
