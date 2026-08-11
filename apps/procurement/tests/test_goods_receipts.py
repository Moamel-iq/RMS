"""
Goods receipts: what arrived, what was accepted, and what still posts nothing.

Three classes carry the weight.

`TestNothingPostsYet` is the boundary this task exists to hold. Stock and the
GRNI journal must commit in one transaction, so Task 2.8 ships no posting
command at all — and the tests assert that absence directly rather than
trusting that nobody added one.

`TestTheReceivedQuantitySeam` is the other half of Task 2.7. That task wrote
its guards against a function returning zero; this one gives the function a
body and proves the guards tighten, which is the whole reason it was a
function.

`TestAcceptedAndRejected` covers PRC-024 and PRC-025 — the single most
commonly broken rule in restaurant purchasing software, which is why the
rejected quantity is derived and the sum is a database constraint.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from apps.accounting.models import JournalEntry
from apps.core.models import AuditEvent
from apps.inventory.models import (
    ConversionType,
    InventoryItem,
    InventoryLot,
    InventoryReasonCode,
    ItemType,
    PackageUnit,
    ReasonCodeApplication,
    StockLedgerEntry,
    StockMovement,
    Warehouse,
)
from apps.organizations.authorization import OutOfScope
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseOrder,
    QualityResult,
    Supplier,
)
from apps.procurement.permissions import (
    CREATE_GOODS_RECEIPT,
    INSPECT_GOODS_RECEIPT,
    POST_GOODS_RECEIPT,
    REVERSE_GOODS_RECEIPT,
    VIEW_GOODS_RECEIPT,
    permissions_for_role,
)
from apps.procurement.selectors import (
    outstanding_order_lines,
    resolve_goods_receipt,
    visible_goods_receipts,
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

HX = {"hx-request": "true"}
RECEIVED = datetime.date(2026, 2, 10)


@pytest.fixture
def units() -> None:
    from django.core.management import call_command

    call_command("seed_units", verbosity=0)


@pytest.fixture
def kilogram(units: None) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="KG")


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
def sack(organization: Organization, rice: InventoryItem) -> PackageUnit:
    from apps.inventory.services import create_item_conversion, create_package_unit

    package = create_package_unit(organization=organization, code="SACK", name_ar="كيس")
    create_item_conversion(
        item=rice,
        package_unit=package,
        factor_to_base=Decimal("30.000000000000"),
        conversion_type=ConversionType.FIXED,
        effective_from=datetime.date(2026, 1, 1),
    )
    return package


@pytest.fixture
def container(organization: Organization, meat: InventoryItem) -> PackageUnit:
    """A VARIABLE package: the factor is a planning estimate, not a quantity."""
    from apps.inventory.services import create_item_conversion, create_package_unit

    package = create_package_unit(organization=organization, code="CONTAINER", name_ar="حاوية")
    create_item_conversion(
        item=meat,
        package_unit=package,
        factor_to_base=Decimal("18.000000000000"),
        conversion_type=ConversionType.VARIABLE,
        effective_from=datetime.date(2026, 1, 1),
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
def buyer(branch: Branch) -> User:
    user = User.objects.create_user(username="buyer", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.PURCHASING)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def approver(branch: Branch) -> User:
    user = User.objects.create_user(username="approver", password="pw-not-real-1234")
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
        ordered_on=datetime.date(2026, 2, 1),
    )
    add_order_line(
        order=order,
        item=rice,
        ordered_quantity=Decimal("100.000"),
        unit_price=Decimal("1400.000000"),
    )
    approve_purchase_order(order=order, actor=approver)
    return issue_purchase_order(order=order, actor=buyer)


@pytest.fixture
def draft(grocery: Supplier, branch: Branch, store: Warehouse, keeper: User) -> GoodsReceipt:
    return create_goods_receipt(
        supplier=grocery,
        branch=branch,
        warehouse=store,
        created_by=keeper,
        received_at=RECEIVED,
        delivery_reference="DN-100",
        evidence_reference="إشعار المورد",
    )


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


class TestNothingPostsYet:
    def test_no_stock_or_journal_exists_after_a_full_inspection(
        self, draft: GoodsReceipt, rice: InventoryItem, keeper: User
    ) -> None:
        """
        The boundary this task exists to hold. Stock and the GRNI journal
        commit together in Task 2.9; anything that moved stock here would
        create inventory with no accounting behind it.
        """
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("50.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("50.000"), actor=keeper)
        draft.refresh_from_db()
        assert draft.is_ready_to_post is True
        assert draft.status == GoodsReceiptStatus.DRAFT
        assert StockMovement.objects.count() == 0
        assert StockLedgerEntry.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_no_posting_service_is_exported(self) -> None:
        """
        Asserted rather than assumed. A stock-only post added later would be
        exactly the defect the boundary is here to prevent, and this fails the
        moment somebody writes one without the journal.
        """
        from apps.procurement import services

        for name in ("post_goods_receipt", "reverse_goods_receipt"):
            assert not hasattr(services, name), (
                f"{name} exists before Task 2.9. Stock and GRNI must commit in one transaction."
            )

    def test_no_route_offers_posting(self) -> None:
        from django.urls import NoReverseMatch
        from django.urls import reverse as resolve

        for name in ("procurement:goods_receipt_post", "procurement:goods_receipt_reverse"):
            with pytest.raises(NoReverseMatch):
                resolve(name, args=[1])

    def test_a_posted_status_with_no_timestamp_is_refused(self, draft: GoodsReceipt) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            GoodsReceipt.objects.filter(pk=draft.pk).update(status="POSTED")

    def test_a_draft_cannot_carry_a_posted_timestamp(
        self, draft: GoodsReceipt, keeper: User
    ) -> None:
        """Catches a half-applied posting transaction at the database."""
        from django.utils import timezone

        with pytest.raises(IntegrityError), transaction.atomic():
            GoodsReceipt.objects.filter(pk=draft.pk).update(
                posted_by=keeper, posted_at=timezone.now()
            )


# ---------------------------------------------------------------------------
# Accepted and rejected
# ---------------------------------------------------------------------------


class TestAcceptedAndRejected:
    def test_rejected_is_derived_from_delivered_minus_accepted(
        self, draft: GoodsReceipt, rice: InventoryItem, keeper: User, reason: InventoryReasonCode
    ) -> None:
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("120.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspected = inspect_receipt_line(
            line=line,
            accepted_base_quantity=Decimal("90.000"),
            actor=keeper,
            rejection_reason=reason,
        )
        assert inspected.accepted_base_quantity == Decimal("90.000")
        assert inspected.rejected_base_quantity == Decimal("30.000")
        assert inspected.quality_result == QualityResult.PARTIAL

    def test_a_full_acceptance_needs_no_reason(
        self, draft: GoodsReceipt, rice: InventoryItem, keeper: User
    ) -> None:
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            unit_price=Decimal("1.000000"),
        )
        inspected = inspect_receipt_line(
            line=line, accepted_base_quantity=Decimal("10.000"), actor=keeper
        )
        assert inspected.quality_result == QualityResult.ACCEPTED
        assert inspected.rejection_reason is None

    def test_rejecting_without_a_reason_is_refused(
        self, draft: GoodsReceipt, rice: InventoryItem, keeper: User
    ) -> None:
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            unit_price=Decimal("1.000000"),
        )
        with pytest.raises(ValidationError) as refused:
            inspect_receipt_line(line=line, accepted_base_quantity=Decimal("4.000"), actor=keeper)
        assert refused.value.code == "rejection_reason_required"

    def test_accepting_more_than_was_delivered_is_refused(
        self, draft: GoodsReceipt, rice: InventoryItem, keeper: User
    ) -> None:
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            unit_price=Decimal("1.000000"),
        )
        with pytest.raises(ValidationError) as refused:
            inspect_receipt_line(line=line, accepted_base_quantity=Decimal("11.000"), actor=keeper)
        assert refused.value.code == "accepted_above_delivered"

    def test_the_database_refuses_accepted_above_delivered(
        self, draft: GoodsReceipt, rice: InventoryItem
    ) -> None:
        """PRC-024. The database owns this one outright."""
        add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            unit_price=Decimal("1.000000"),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            GoodsReceiptLine.objects.filter(receipt=draft).update(
                accepted_base_quantity=Decimal("11.000")
            )

    def test_the_accepted_value_is_the_accepted_share(
        self, draft: GoodsReceipt, rice: InventoryItem, keeper: User, reason: InventoryReasonCode
    ) -> None:
        """120 kg at 1,400 is 168,000; 90 accepted is three quarters — 126,000."""
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("120.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(
            line=line,
            accepted_base_quantity=Decimal("90.000"),
            actor=keeper,
            rejection_reason=reason,
        )
        draft.refresh_from_db()
        assert draft.accepted_value == Decimal("126000.000")

    def test_a_receipt_is_not_ready_until_every_line_is_inspected(
        self,
        draft: GoodsReceipt,
        rice: InventoryItem,
        meat: InventoryItem,
        keeper: User,
        meat_lot: InventoryLot,
    ) -> None:
        first = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            unit_price=Decimal("1.000000"),
        )
        add_receipt_line(
            receipt=draft,
            item=meat,
            delivered_quantity=Decimal("5.000"),
            unit_price=Decimal("1.000000"),
            lot=meat_lot,
        )
        inspect_receipt_line(line=first, accepted_base_quantity=Decimal("10.000"), actor=keeper)
        draft.refresh_from_db()
        assert draft.is_ready_to_post is False

    def test_a_receipt_without_evidence_is_not_ready(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
    ) -> None:
        bare = create_goods_receipt(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
        )
        line = add_receipt_line(
            receipt=bare,
            item=rice,
            delivered_quantity=Decimal("1.000"),
            unit_price=Decimal("1.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("1.000"), actor=keeper)
        bare.refresh_from_db()
        assert bare.is_ready_to_post is False


# ---------------------------------------------------------------------------
# Price, packages and lots
# ---------------------------------------------------------------------------


class TestLineRules:
    def test_a_line_with_no_order_and_no_price_is_refused(
        self, draft: GoodsReceipt, rice: InventoryItem
    ) -> None:
        """
        PRC-028: value with no number is how zero-cost stock gets created.
        """
        with pytest.raises(ValidationError) as refused:
            add_receipt_line(receipt=draft, item=rice, delivered_quantity=Decimal("1.000"))
        assert refused.value.code == "price_required"

    def test_a_linked_order_line_supplies_the_price(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
    ) -> None:
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
        )
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            order_line=issued_order.lines.get(),
        )
        assert line.unit_price == Decimal("1400.000000")

    def test_a_variable_package_demands_the_scale_reading(
        self,
        draft: GoodsReceipt,
        meat: InventoryItem,
        container: PackageUnit,
        meat_lot: InventoryLot,
    ) -> None:
        """PRC-026. Twelve lambs is not a quantity of meat."""
        with pytest.raises(ValidationError) as refused:
            add_receipt_line(
                receipt=draft,
                item=meat,
                package_unit=container,
                delivered_quantity=Decimal("1.000"),
                unit_price=Decimal("9500.000000"),
                lot=meat_lot,
            )
        assert refused.value.code == "measured_quantity_required"

    def test_a_weighed_container_uses_the_scale_not_the_factor(
        self,
        draft: GoodsReceipt,
        meat: InventoryItem,
        container: PackageUnit,
        meat_lot: InventoryLot,
    ) -> None:
        line = add_receipt_line(
            receipt=draft,
            item=meat,
            package_unit=container,
            delivered_quantity=Decimal("1.000"),
            measured_base_quantity=Decimal("17.400"),
            unit_price=Decimal("9500.000000"),
            lot=meat_lot,
        )
        assert line.conversion_factor == Decimal("18.000000000000")
        assert line.delivered_base_quantity == Decimal("17.400")

    def test_a_fixed_package_converts_arithmetically(
        self, draft: GoodsReceipt, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            package_unit=sack,
            delivered_quantity=Decimal("4.000"),
            unit_price=Decimal("42000.000000"),
        )
        assert line.delivered_base_quantity == Decimal("120.000")

    def test_a_lot_tracked_item_requires_its_lot(
        self, draft: GoodsReceipt, meat: InventoryItem
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            add_receipt_line(
                receipt=draft,
                item=meat,
                delivered_quantity=Decimal("1.000"),
                unit_price=Decimal("1.000000"),
            )
        assert refused.value.code == "lot_required"

    def test_an_untracked_item_refuses_a_lot(
        self, draft: GoodsReceipt, rice: InventoryItem, meat_lot: InventoryLot
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            add_receipt_line(
                receipt=draft,
                item=rice,
                delivered_quantity=Decimal("1.000"),
                unit_price=Decimal("1.000000"),
                lot=meat_lot,
            )
        assert refused.value.code == "lot_prohibited"


# ---------------------------------------------------------------------------
# The received-quantity seam
# ---------------------------------------------------------------------------


class TestTheReceivedQuantitySeam:
    def test_a_draft_receipt_does_not_count_as_received(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
    ) -> None:
        """
        Task 2.0 gives this document three statuses and "inspected" is not one
        of them. Until a draft posts, no stock exists to protect.
        """
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
        )
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("40.000"),
            order_line=issued_order.lines.get(),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("40.000"), actor=keeper)
        assert received_base_quantity(issued_order.lines.get()) == Decimal("0.000")

    def test_a_posted_receipt_counts(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
    ) -> None:
        """
        Posting arrives in Task 2.9, so the status is set directly here — the
        point under test is the seam's query, not the posting service.
        """
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
        )
        order_line = issued_order.lines.get()
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("40.000"),
            order_line=order_line,
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("40.000"), actor=keeper)
        from django.utils import timezone

        GoodsReceipt.objects.filter(pk=receipt.pk).update(
            status=GoodsReceiptStatus.POSTED, posted_by=keeper, posted_at=timezone.now()
        )
        assert received_base_quantity(order_line) == Decimal("40.000")

    def test_a_reversed_receipt_gives_its_quantity_back(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
    ) -> None:
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
        )
        order_line = issued_order.lines.get()
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("40.000"),
            order_line=order_line,
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("40.000"), actor=keeper)
        GoodsReceipt.objects.filter(pk=receipt.pk).update(status=GoodsReceiptStatus.REVERSED)
        assert received_base_quantity(order_line) == Decimal("0.000")

    def test_a_revision_below_the_received_quantity_is_now_refused(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        buyer: User,
        rice: InventoryItem,
    ) -> None:
        """
        The Task 2.7 guard, activated. It was written against a function
        returning zero; this is the first test that can actually trip it.
        """
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
        )
        order_line = issued_order.lines.get()
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("80.000"),
            order_line=order_line,
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("80.000"), actor=keeper)
        from django.utils import timezone

        GoodsReceipt.objects.filter(pk=receipt.pk).update(
            status=GoodsReceiptStatus.POSTED, posted_by=keeper, posted_at=timezone.now()
        )

        with pytest.raises(ValidationError) as refused:
            revise_purchase_order(
                order=issued_order,
                actor=buyer,
                reason="تقليل",
                line_quantities={str(order_line.line_uid): Decimal("50.000")},
            )
        assert refused.value.code == "below_received_quantity"

    def test_a_revision_above_the_received_quantity_is_allowed(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        buyer: User,
        rice: InventoryItem,
    ) -> None:
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
        )
        order_line = issued_order.lines.get()
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("40.000"),
            order_line=order_line,
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("40.000"), actor=keeper)
        from django.utils import timezone

        GoodsReceipt.objects.filter(pk=receipt.pk).update(
            status=GoodsReceiptStatus.POSTED, posted_by=keeper, posted_at=timezone.now()
        )
        revised = revise_purchase_order(
            order=issued_order,
            actor=buyer,
            reason="تقليل جزئي",
            line_quantities={str(order_line.line_uid): Decimal("60.000")},
        )
        assert revised.lines.get().ordered_base_quantity == Decimal("60.000")

    def test_the_outstanding_selector_derives_the_remainder(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
    ) -> None:
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
        )
        order_line = issued_order.lines.get()
        line = add_receipt_line(
            receipt=receipt,
            item=rice,
            delivered_quantity=Decimal("30.000"),
            order_line=order_line,
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("30.000"), actor=keeper)
        from django.utils import timezone

        GoodsReceipt.objects.filter(pk=receipt.pk).update(
            status=GoodsReceiptStatus.POSTED, posted_by=keeper, posted_at=timezone.now()
        )
        rows = outstanding_order_lines(issued_order)
        assert rows[0]["ordered"] == Decimal("100.000")
        assert rows[0]["received"] == Decimal("30.000")
        assert rows[0]["outstanding"] == Decimal("70.000")


# ---------------------------------------------------------------------------
# Over-receipt and order state
# ---------------------------------------------------------------------------


class TestOrderRules:
    def test_over_receipt_is_refused_at_zero_tolerance(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
    ) -> None:
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
        )
        with pytest.raises(ValidationError) as refused:
            add_receipt_line(
                receipt=receipt,
                item=rice,
                delivered_quantity=Decimal("101.000"),
                order_line=issued_order.lines.get(),
            )
        assert refused.value.code == "over_receipt"

    def test_two_drafts_cannot_together_over_receive(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        rice: InventoryItem,
    ) -> None:
        """
        Neither draft posts, so `received_base_quantity` says zero for both —
        which is why `add_receipt_line` also subtracts other drafts.
        """
        order_line = issued_order.lines.get()
        for index in range(2):
            receipt = create_goods_receipt(
                supplier=issued_order.supplier,
                branch=branch,
                warehouse=store,
                created_by=keeper,
                received_at=RECEIVED,
                order=issued_order,
                delivery_reference=f"DN-{index}",
            )
            if index == 0:
                add_receipt_line(
                    receipt=receipt,
                    item=rice,
                    delivered_quantity=Decimal("70.000"),
                    order_line=order_line,
                )
            else:
                with pytest.raises(ValidationError) as refused:
                    add_receipt_line(
                        receipt=receipt,
                        item=rice,
                        delivered_quantity=Decimal("40.000"),
                        order_line=order_line,
                    )
                assert refused.value.code == "over_receipt"

    def test_a_cancelled_order_cannot_receive(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        approver: User,
    ) -> None:
        cancel_purchase_order(order=issued_order, actor=approver, reason="المورد اعتذر")
        with pytest.raises(ValidationError) as refused:
            create_goods_receipt(
                supplier=issued_order.supplier,
                branch=branch,
                warehouse=store,
                created_by=keeper,
                received_at=RECEIVED,
                order=issued_order,
            )
        assert refused.value.code == "order_cancelled"

    def test_a_stale_order_instance_cannot_bypass_the_cancellation_guard(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        approver: User,
    ) -> None:
        """The §D rule, applied to the newest lifecycle."""
        stale = PurchaseOrder.objects.get(pk=issued_order.pk)
        cancel_purchase_order(order=issued_order, actor=approver, reason="اعتذار")

        assert stale.status == "ISSUED"
        with pytest.raises(ValidationError) as refused:
            create_goods_receipt(
                supplier=stale.supplier,
                branch=branch,
                warehouse=store,
                created_by=keeper,
                received_at=RECEIVED,
                order=stale,
            )
        assert refused.value.code == "order_cancelled"

    def test_a_stale_receipt_instance_cannot_gain_a_line_after_posting(
        self, draft: GoodsReceipt, rice: InventoryItem, keeper: User
    ) -> None:
        from django.utils import timezone

        stale = GoodsReceipt.objects.get(pk=draft.pk)
        GoodsReceipt.objects.filter(pk=draft.pk).update(
            status=GoodsReceiptStatus.POSTED, posted_by=keeper, posted_at=timezone.now()
        )
        assert stale.status == GoodsReceiptStatus.DRAFT
        with pytest.raises(ValidationError) as refused:
            add_receipt_line(
                receipt=stale,
                item=rice,
                delivered_quantity=Decimal("1.000"),
                unit_price=Decimal("1.000000"),
            )
        assert refused.value.code == "receipt_not_editable"

    def test_a_draft_order_cannot_receive(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        buyer: User,
        keeper: User,
    ) -> None:
        drafted = create_purchase_order(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=buyer,
            ordered_on=datetime.date(2026, 2, 1),
        )
        with pytest.raises(ValidationError) as refused:
            create_goods_receipt(
                supplier=grocery,
                branch=branch,
                warehouse=store,
                created_by=keeper,
                received_at=RECEIVED,
                order=drafted,
            )
        assert refused.value.code == "order_not_approved"

    def test_the_order_version_is_snapshotted(
        self,
        issued_order: PurchaseOrder,
        branch: Branch,
        store: Warehouse,
        keeper: User,
        buyer: User,
    ) -> None:
        """A later revision must not change what this delivery was measured against."""
        receipt = create_goods_receipt(
            supplier=issued_order.supplier,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            order=issued_order,
        )
        assert receipt.order_version == 1
        revise_purchase_order(order=issued_order, actor=buyer, reason="تعديل")
        receipt.refresh_from_db()
        assert receipt.order_version == 1

    def test_a_supplier_mismatch_is_refused(
        self,
        issued_order: PurchaseOrder,
        organization: Organization,
        branch: Branch,
        store: Warehouse,
        keeper: User,
    ) -> None:
        other = create_supplier(organization=organization, code="OTHER-01", name_ar="آخر")
        with pytest.raises(ValidationError) as refused:
            create_goods_receipt(
                supplier=other,
                branch=branch,
                warehouse=store,
                created_by=keeper,
                received_at=RECEIVED,
                order=issued_order,
            )
        assert refused.value.code == "order_supplier_mismatch"


# ---------------------------------------------------------------------------
# Duplicate delivery notes, scope and permissions
# ---------------------------------------------------------------------------


class TestScopeAndDuplicates:
    def test_one_supplier_cannot_send_the_same_note_twice(
        self,
        draft: GoodsReceipt,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        keeper: User,
    ) -> None:
        with pytest.raises(ValidationError):
            create_goods_receipt(
                supplier=grocery,
                branch=branch,
                warehouse=store,
                created_by=keeper,
                received_at=RECEIVED,
                delivery_reference="DN-100",
            )

    def test_another_supplier_may_reuse_the_note_number(
        self,
        draft: GoodsReceipt,
        organization: Organization,
        branch: Branch,
        store: Warehouse,
        keeper: User,
    ) -> None:
        """Two suppliers numbering their notes "1" is not a conflict."""
        other = create_supplier(organization=organization, code="OTHER-02", name_ar="آخر")
        twin = create_goods_receipt(
            supplier=other,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            delivery_reference="DN-100",
        )
        assert twin.pk != draft.pk

    def test_a_warehouse_from_another_branch_is_refused(
        self, grocery: Supplier, branch: Branch, organization: Organization, keeper: User
    ) -> None:
        from apps.inventory.services import create_warehouse
        from apps.organizations.services import create_branch

        other = create_branch(
            organization=organization,
            code="FARBR",
            name_ar="فرع",
            name_en="Far",
            business_day_start_time=datetime.time(9, 0),
        )
        with pytest.raises(ValidationError) as refused:
            create_goods_receipt(
                supplier=grocery,
                branch=branch,
                warehouse=create_warehouse(branch=other, code="FARW", name_ar="بعيد"),
                created_by=keeper,
                received_at=RECEIVED,
            )
        assert refused.value.code == "warehouse_branch_mismatch"

    def test_a_receipt_in_an_unreachable_warehouse_is_out_of_scope(
        self, draft: GoodsReceipt, accounting_manager: User
    ) -> None:
        """
        The accounting manager holds organization authority but the receipt
        list is warehouse-scoped, so this proves the scope is the warehouse
        rather than the organization.
        """
        assert draft.pk in set(
            visible_goods_receipts(accounting_manager).values_list("pk", flat=True)
        )

    def test_a_foreign_receipt_is_out_of_scope(
        self, manager: User, other_organization: Organization
    ) -> None:
        from apps.inventory.services import create_warehouse
        from apps.organizations.services import create_branch

        their_branch = create_branch(
            organization=other_organization,
            code="RIVALB",
            name_ar="فرع",
            name_en="Branch",
            business_day_start_time=datetime.time(9, 0),
        )
        theirs = create_goods_receipt(
            supplier=create_supplier(
                organization=other_organization, code="RIVAL-01", name_ar="منافس"
            ),
            branch=their_branch,
            warehouse=create_warehouse(branch=their_branch, code="W", name_ar="م"),
            created_by=manager,
            received_at=RECEIVED,
        )
        assert theirs.pk not in set(visible_goods_receipts(manager).values_list("pk", flat=True))
        with pytest.raises(OutOfScope):
            resolve_goods_receipt(manager, theirs.pk)

    def test_a_storekeeper_receives_inspects_and_posts(self) -> None:
        held = permissions_for_role(Role.STOREKEEPER)
        assert {
            VIEW_GOODS_RECEIPT,
            CREATE_GOODS_RECEIPT,
            INSPECT_GOODS_RECEIPT,
            POST_GOODS_RECEIPT,
        } <= held
        # Undoing a posted receipt reverses a journal, which is not warehouse work.
        assert REVERSE_GOODS_RECEIPT not in held

    def test_purchasing_reads_deliveries_and_confirms_none(self) -> None:
        """Whoever chose the supplier does not certify what they sent."""
        held = permissions_for_role(Role.PURCHASING)
        assert VIEW_GOODS_RECEIPT in held
        assert CREATE_GOODS_RECEIPT not in held
        assert INSPECT_GOODS_RECEIPT not in held

    def test_an_accounting_manager_reverses_but_does_not_receive(self) -> None:
        held = permissions_for_role(Role.ACCOUNTING_MANAGER)
        assert REVERSE_GOODS_RECEIPT in held
        assert CREATE_GOODS_RECEIPT not in held


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class TestScreens:
    def test_the_list_renders_and_filters_by_status(
        self, client_for: Callable[[User], Client], keeper: User, draft: GoodsReceipt
    ) -> None:
        client = client_for(keeper)
        body = client.get(reverse("procurement:goods_receipt_list")).content.decode()
        assert "DN-100" in body

        filtered = client.get(
            reverse("procurement:goods_receipt_list"), {"status": "POSTED"}, headers=HX
        ).content.decode()
        assert "DN-100" not in filtered

    def test_an_hx_request_returns_only_the_results(
        self, client_for: Callable[[User], Client], keeper: User, draft: GoodsReceipt
    ) -> None:
        body = (
            client_for(keeper)
            .get(reverse("procurement:goods_receipt_list"), headers=HX)
            .content.decode()
        )
        assert "<html" not in body.lower()
        assert "DN-100" in body

    def test_the_detail_shows_accepted_and_rejected(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        draft: GoodsReceipt,
        rice: InventoryItem,
        reason: InventoryReasonCode,
    ) -> None:
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("120.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(
            line=line,
            accepted_base_quantity=Decimal("90.000"),
            actor=keeper,
            rejection_reason=reason,
        )
        body = (
            client_for(keeper)
            .get(reverse("procurement:goods_receipt_detail", args=[draft.pk]))
            .content.decode()
        )
        assert "90.000" in body
        assert "30.000" in body
        assert "تالف عند الاستلام" in body

    def test_the_detail_never_claims_posted_while_a_draft(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        draft: GoodsReceipt,
        rice: InventoryItem,
    ) -> None:
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("5.000"),
            unit_price=Decimal("1.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("5.000"), actor=keeper)
        body = (
            client_for(keeper)
            .get(reverse("procurement:goods_receipt_detail", args=[draft.pk]))
            .content.decode()
        )
        assert "مفحوص وجاهز للترحيل" in body
        assert "مرحّل<" not in body

    def test_inspecting_through_the_screen_records_the_split(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        draft: GoodsReceipt,
        rice: InventoryItem,
    ) -> None:
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            unit_price=Decimal("1.000000"),
        )
        response = client_for(keeper).post(
            reverse("procurement:goods_receipt_inspect", args=[draft.pk, line.pk]),
            {"accepted_base_quantity": "10.000"},
        )
        assert response.status_code == 302
        line.refresh_from_db()
        assert line.quality_result == QualityResult.ACCEPTED

    def test_line_delete_is_post_only(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        draft: GoodsReceipt,
        rice: InventoryItem,
    ) -> None:
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("1.000"),
            unit_price=Decimal("1.000000"),
        )
        url = reverse("procurement:goods_receipt_line_delete", args=[draft.pk, line.pk])
        client = client_for(keeper)
        assert client.get(url).status_code == 405
        assert client.post(url).status_code == 302
        assert draft.lines.count() == 0

    def test_a_line_from_another_receipt_is_a_404_on_this_route(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        draft: GoodsReceipt,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        rice: InventoryItem,
    ) -> None:
        other = create_goods_receipt(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=keeper,
            received_at=RECEIVED,
            delivery_reference="DN-999",
        )
        stray = add_receipt_line(
            receipt=other,
            item=rice,
            delivered_quantity=Decimal("1.000"),
            unit_price=Decimal("1.000000"),
        )
        response = client_for(keeper).post(
            reverse("procurement:goods_receipt_line_delete", args=[draft.pk, stray.pk])
        )
        assert response.status_code == 404
        assert GoodsReceiptLine.objects.filter(pk=stray.pk).exists()

    def test_inspection_is_audited(
        self, draft: GoodsReceipt, rice: InventoryItem, keeper: User
    ) -> None:
        line = add_receipt_line(
            receipt=draft,
            item=rice,
            delivered_quantity=Decimal("1.000"),
            unit_price=Decimal("1.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("1.000"), actor=keeper)
        assert AuditEvent.objects.filter(
            target_type="procurement.GoodsReceiptLine",
            target_id=str(line.pk),
            reason="inspection",
        ).exists()
