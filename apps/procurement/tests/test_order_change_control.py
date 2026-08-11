"""
Purchase order change control, and the stale-instance rule it depends on.

Two things are proved here that nothing else proves.

`TestTheStaleInstanceRule` is the regression suite for a defect class that
appeared three times across Tasks 2.3, 2.5 and 2.6: a service asked the object
it was handed what state it was in, and the object was out of date. Every
lifecycle service now re-reads under a lock, and each of these tests holds a
deliberately stale copy and confirms the database wins.

`TestReceivedQuantityGuards` is the interesting one, because goods receipts do
not exist yet. The guards are written against `received_base_quantity`, a real
function with real call sites that currently returns zero. When Task 2.8 gives
it a body, these guards tighten with it — which is the whole reason it is a
function rather than a hard-coded zero somebody would have to remember.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from decimal import Decimal
from typing import cast

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
    ItemType,
    PackageUnit,
    StockLedgerEntry,
    StockMovement,
    Warehouse,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.procurement.lifecycle import lock_and_require_status
from apps.procurement.models import (
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchaseOrderVersion,
    PurchaseRequest,
    Supplier,
    SupplierQuotation,
)
from apps.procurement.selectors import order_version_history
from apps.procurement.services import (
    add_order_line,
    add_quotation_line,
    add_request_line,
    approve_purchase_order,
    approve_purchase_request,
    cancel_purchase_order,
    create_purchase_order,
    create_purchase_request,
    create_supplier,
    create_supplier_quotation,
    decline_supplier_quotation,
    issue_purchase_order,
    received_base_quantity,
    revise_purchase_order,
    submit_purchase_request,
    submit_supplier_quotation,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

ORDERED = datetime.date(2026, 2, 1)


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
def issued(
    grocery: Supplier,
    branch: Branch,
    store: Warehouse,
    buyer: User,
    approver: User,
    rice: InventoryItem,
    sack: PackageUnit,
) -> PurchaseOrder:
    order = create_purchase_order(
        supplier=grocery,
        branch=branch,
        warehouse=store,
        created_by=buyer,
        ordered_on=ORDERED,
    )
    add_order_line(
        order=order,
        item=rice,
        package_unit=sack,
        ordered_quantity=Decimal("4.000"),
        unit_price=Decimal("42000.000000"),
    )
    approve_purchase_order(order=order, actor=approver)
    return issue_purchase_order(order=order, actor=buyer)


# ---------------------------------------------------------------------------
# The stale-instance rule
# ---------------------------------------------------------------------------


class TestTheStaleInstanceRule:
    """
    Each test holds a copy loaded before a transition and proves the service
    consults the database rather than the copy.
    """

    def test_a_request_line_cannot_be_added_from_a_stale_draft(
        self, branch: Branch, keeper: User, store: Warehouse, rice: InventoryItem
    ) -> None:
        document = create_purchase_request(
            branch=branch,
            requested_by=keeper,
            warehouse=store,
            required_date=ORDERED,
            purpose="مخزون",
        )
        add_request_line(request=document, item=rice, entered_quantity=Decimal("1.000"))
        stale = PurchaseRequest.objects.get(pk=document.pk)
        submit_purchase_request(request=document, actor=keeper)

        assert stale.status == "DRAFT"  # the copy still believes it
        with pytest.raises(ValidationError) as refused:
            add_request_line(request=stale, item=rice, entered_quantity=Decimal("2.000"))
        assert refused.value.code == "request_not_editable"

    def test_a_declined_quotation_cannot_be_awarded_from_a_stale_copy(
        self,
        branch: Branch,
        keeper: User,
        approver: User,
        store: Warehouse,
        rice: InventoryItem,
        grocery: Supplier,
        buyer: User,
    ) -> None:
        from apps.procurement.comparison import award_quotation

        request = create_purchase_request(
            branch=branch,
            requested_by=keeper,
            warehouse=store,
            required_date=ORDERED,
            purpose="رز",
        )
        add_request_line(request=request, item=rice, entered_quantity=Decimal("1.000"))
        submit_purchase_request(request=request, actor=keeper)
        approved = approve_purchase_request(request=request, actor=approver, reason="ok")

        quotation = create_supplier_quotation(
            supplier=grocery,
            recorded_by=buyer,
            request=approved,
            quoted_at=ORDERED,
            evidence_reference="ورقة",
        )
        add_quotation_line(
            quotation=quotation,
            item=rice,
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.000000"),
        )
        submit_supplier_quotation(quotation=quotation, actor=buyer)

        stale = SupplierQuotation.objects.get(pk=quotation.pk)
        decline_supplier_quotation(quotation=quotation, actor=buyer, reason="أغلى")

        assert stale.status == "SUBMITTED"
        with pytest.raises(ValidationError) as refused:
            award_quotation(
                request=approved, quotation=stale, actor=buyer, reason="أريده", on=ORDERED
            )
        assert refused.value.code == "quotation_not_awardable"

    def test_an_order_line_cannot_be_added_from_a_stale_draft(
        self, issued: PurchaseOrder, rice: InventoryItem, grocery: Supplier
    ) -> None:
        stale = PurchaseOrder.objects.get(pk=issued.pk)
        stale.status = PurchaseOrderStatus.DRAFT  # a copy loaded before approval

        with pytest.raises(ValidationError) as refused:
            add_order_line(
                order=stale,
                item=rice,
                ordered_quantity=Decimal("1.000"),
                unit_price=Decimal("1.000000"),
            )
        assert refused.value.code == "order_not_editable"

    def test_a_cancelled_order_cannot_be_revised_from_a_stale_copy(
        self, issued: PurchaseOrder, approver: User, buyer: User
    ) -> None:
        stale = PurchaseOrder.objects.get(pk=issued.pk)
        cancel_purchase_order(order=issued, actor=approver, reason="المورد اعتذر")

        assert stale.status == PurchaseOrderStatus.ISSUED
        with pytest.raises(ValidationError) as refused:
            revise_purchase_order(order=stale, actor=buyer, reason="تعديل")
        assert refused.value.code == "order_not_revisable"

    def test_the_helper_returns_the_locked_row_not_the_caller_copy(
        self, issued: PurchaseOrder, buyer: User
    ) -> None:
        """
        Mutating the caller's copy must not reach the database through the
        helper — it hands back the row it locked, and that is what services
        write.
        """
        stale = PurchaseOrder.objects.get(pk=issued.pk)
        stale.supplier_reference = "TAMPERED"

        locked = lock_and_require_status(
            PurchaseOrder,
            stale.pk,
            {PurchaseOrderStatus.ISSUED},
            code="unused",
        )
        assert locked.supplier_reference != "TAMPERED"
        assert locked.pk == stale.pk

    def test_the_helper_refuses_a_status_outside_the_allowed_set(
        self, issued: PurchaseOrder
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            lock_and_require_status(
                PurchaseOrder,
                issued.pk,
                {PurchaseOrderStatus.DRAFT},
                code="order_not_editable",
            )
        assert refused.value.code == "order_not_editable"


# ---------------------------------------------------------------------------
# Revision
# ---------------------------------------------------------------------------


class TestRevision:
    def test_a_draft_is_edited_not_revised(
        self, grocery: Supplier, branch: Branch, store: Warehouse, buyer: User
    ) -> None:
        drafted = create_purchase_order(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=buyer,
            ordered_on=ORDERED,
        )
        with pytest.raises(ValidationError) as refused:
            revise_purchase_order(order=drafted, actor=buyer, reason="تعديل")
        assert refused.value.code == "order_not_revisable"

    def test_a_revision_freezes_the_previous_version_and_bumps_the_live_one(
        self, issued: PurchaseOrder, buyer: User
    ) -> None:
        line = issued.lines.get()
        assert issued.version == 1

        revised = revise_purchase_order(
            order=issued,
            actor=buyer,
            reason="المورد أكّد كمية أقل",
            line_quantities={str(line.line_uid): Decimal("3.000")},
        )
        assert revised.version == 2
        assert revised.revised_at is not None

        frozen = PurchaseOrderVersion.objects.get(order=revised, version=1)
        assert frozen.lines[0]["ordered_quantity"] == "4.000"
        assert frozen.lines[0]["line_total"] == "168000.000"
        assert frozen.reason == "المورد أكّد كمية أقل"
        assert frozen.revised_by_id == buyer.pk

        line.refresh_from_db()
        assert line.ordered_quantity == Decimal("3.000")
        assert line.ordered_base_quantity == Decimal("90.000")
        assert line.line_total == Decimal("126000.000")

    def test_the_snapshot_stores_decimals_as_strings(
        self, issued: PurchaseOrder, buyer: User
    ) -> None:
        """A snapshot that went through binary float would record a price
        nobody agreed to."""
        revise_purchase_order(order=issued, actor=buyer, reason="تعديل")
        frozen = PurchaseOrderVersion.objects.get(order=issued, version=1)
        for value in ("ordered_quantity", "unit_price", "line_total"):
            assert isinstance(frozen.lines[0][value], str)

    def test_a_revision_needs_a_reason(self, issued: PurchaseOrder, buyer: User) -> None:
        with pytest.raises(ValidationError) as refused:
            revise_purchase_order(order=issued, actor=buyer, reason="   ")
        assert refused.value.code == "reason_required"

    def test_the_database_refuses_a_version_with_no_reason(
        self, issued: PurchaseOrder, buyer: User
    ) -> None:
        from django.utils import timezone

        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseOrderVersion.objects.create(
                order=issued,
                version=99,
                reason="",
                revised_by=buyer,
                revised_at=timezone.now(),
            )

    def test_two_versions_cannot_share_a_number(self, issued: PurchaseOrder, buyer: User) -> None:
        from django.utils import timezone

        revise_purchase_order(order=issued, actor=buyer, reason="أول تعديل")
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseOrderVersion.objects.create(
                order=issued,
                version=1,
                reason="تكرار",
                revised_by=buyer,
                revised_at=timezone.now(),
            )

    def test_a_zero_quantity_revision_is_refused(self, issued: PurchaseOrder, buyer: User) -> None:
        line = issued.lines.get()
        with pytest.raises(ValidationError) as refused:
            revise_purchase_order(
                order=issued,
                actor=buyer,
                reason="صفر",
                line_quantities={str(line.line_uid): Decimal("0.000")},
            )
        assert refused.value.code == "quantity_not_positive"

    def test_a_line_from_another_order_cannot_be_revised(
        self, issued: PurchaseOrder, buyer: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            revise_purchase_order(
                order=issued,
                actor=buyer,
                reason="خطأ",
                line_quantities={"00000000-0000-0000-0000-000000000000": Decimal("1.000")},
            )
        assert refused.value.code == "line_not_on_order"

    def test_the_supplier_cannot_be_revised_at_all(self) -> None:
        """
        Not by an allowlist that could be bypassed — the signature simply has
        no such parameter, and a caller passing one is a `TypeError`.
        """
        import inspect

        parameters = inspect.signature(revise_purchase_order).parameters
        assert "supplier" not in parameters
        assert "ordered_on" not in parameters

    def test_a_revision_creates_no_stock_and_no_journal(
        self, issued: PurchaseOrder, buyer: User
    ) -> None:
        revise_purchase_order(order=issued, actor=buyer, reason="تعديل")
        assert StockMovement.objects.count() == 0
        assert StockLedgerEntry.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_a_revision_is_audited(self, issued: PurchaseOrder, buyer: User) -> None:
        revise_purchase_order(order=issued, actor=buyer, reason="تعديل مهم")
        assert AuditEvent.objects.filter(
            target_type="procurement.PurchaseOrder",
            target_id=str(issued.pk),
            action="UPDATED",
            reason="تعديل مهم",
        ).exists()


# ---------------------------------------------------------------------------
# Guards that tighten when receipts arrive
# ---------------------------------------------------------------------------


class TestReceivedQuantityGuards:
    def test_the_received_quantity_helper_exists_and_is_called(self) -> None:
        """
        Goods receipts land in Task 2.8. The guards are written against this
        function, not against a hard-coded zero — so when it gains a body they
        tighten, instead of silently continuing to pass.
        """
        import inspect

        source = inspect.getsource(revise_purchase_order)
        assert "received_base_quantity" in source
        assert inspect.getsource(cancel_purchase_order).count("received_base_quantity") >= 1

    def test_it_returns_zero_while_no_receipt_model_exists(self, issued: PurchaseOrder) -> None:
        assert received_base_quantity(issued.lines.get()) == Decimal("0.000")

    def test_a_quantity_above_the_received_amount_is_allowed_today(
        self, issued: PurchaseOrder, buyer: User
    ) -> None:
        line = issued.lines.get()
        revised = revise_purchase_order(
            order=issued,
            actor=buyer,
            reason="زيادة",
            line_quantities={str(line.line_uid): Decimal("6.000")},
        )
        assert revised.lines.get().ordered_base_quantity == Decimal("180.000")


# ---------------------------------------------------------------------------
# Version history
# ---------------------------------------------------------------------------


class TestVersionHistory:
    def test_an_unrevised_order_has_no_history(self, issued: PurchaseOrder, buyer: User) -> None:
        assert order_version_history(buyer, order=issued) == []

    def test_the_history_names_what_changed(self, issued: PurchaseOrder, buyer: User) -> None:
        line = issued.lines.get()
        revise_purchase_order(
            order=issued,
            actor=buyer,
            reason="كمية أقل",
            line_quantities={str(line.line_uid): Decimal("3.000")},
        )
        issued.refresh_from_db()
        entries = order_version_history(buyer, order=issued)

        assert [entry["version"] for entry in entries] == [2, 1]
        assert entries[0]["is_current"] is True
        changes = cast(list[str], entries[0]["changes"])
        assert any("4.000 → 3.000" in change for change in changes)
        assert any("168000.000 → 126000.000" in change for change in changes)

    def test_two_revisions_produce_two_frozen_versions(
        self, issued: PurchaseOrder, buyer: User
    ) -> None:
        line = issued.lines.get()
        revise_purchase_order(
            order=issued,
            actor=buyer,
            reason="أول",
            line_quantities={str(line.line_uid): Decimal("3.000")},
        )
        issued.refresh_from_db()
        revise_purchase_order(
            order=issued,
            actor=buyer,
            reason="ثانٍ",
            line_quantities={str(line.line_uid): Decimal("2.000")},
        )
        issued.refresh_from_db()
        assert issued.version == 3
        assert PurchaseOrderVersion.objects.filter(order=issued).count() == 2
        assert [entry["version"] for entry in order_version_history(buyer, order=issued)] == [
            3,
            2,
            1,
        ]


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class TestScreens:
    def test_the_history_screen_renders_both_versions(
        self, client_for: Callable[[User], Client], buyer: User, issued: PurchaseOrder
    ) -> None:
        line = issued.lines.get()
        revise_purchase_order(
            order=issued,
            actor=buyer,
            reason="المورد أكّد كمية أقل",
            line_quantities={str(line.line_uid): Decimal("3.000")},
        )
        body = (
            client_for(buyer)
            .get(reverse("procurement:purchase_order_history", args=[issued.pk]))
            .content.decode()
        )
        assert "المورد أكّد كمية أقل" in body
        assert "4.000" in body
        assert "3.000" in body

    def test_revising_through_the_screen_creates_a_version(
        self, client_for: Callable[[User], Client], buyer: User, issued: PurchaseOrder
    ) -> None:
        line = issued.lines.get()
        response = client_for(buyer).post(
            reverse("procurement:purchase_order_revise", args=[issued.pk]),
            {f"quantity-{line.line_uid}": "3.000", "reason": "كمية أقل"},
        )
        assert response.status_code == 302
        issued.refresh_from_db()
        assert issued.version == 2

    def test_revising_is_post_only(
        self, client_for: Callable[[User], Client], buyer: User, issued: PurchaseOrder
    ) -> None:
        response = client_for(buyer).get(
            reverse("procurement:purchase_order_revise", args=[issued.pk])
        )
        assert response.status_code == 405

    def test_a_bad_decimal_is_reported_rather_than_raised(
        self, client_for: Callable[[User], Client], buyer: User, issued: PurchaseOrder
    ) -> None:
        line = issued.lines.get()
        response = client_for(buyer).post(
            reverse("procurement:purchase_order_revise", args=[issued.pk]),
            {f"quantity-{line.line_uid}": "not-a-number", "reason": "خطأ"},
        )
        assert response.status_code == 302
        issued.refresh_from_db()
        assert issued.version == 1

    def test_a_storekeeper_cannot_revise_through_the_route(
        self, client_for: Callable[[User], Client], keeper: User, issued: PurchaseOrder
    ) -> None:
        response = client_for(keeper).post(
            reverse("procurement:purchase_order_revise", args=[issued.pk]),
            {"reason": "أريد"},
        )
        assert response.status_code in {302, 403}
        issued.refresh_from_db()
        assert issued.version == 1
