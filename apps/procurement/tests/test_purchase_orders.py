"""
Purchase orders: a commitment that owes nothing.

`TestNoLedgerEffect` is the load-bearing class. An order is the first
procurement document that names a price somebody has agreed to pay, and the
temptation to accrue it is exactly why the absence is asserted at every status
including ISSUED — nothing is owed until goods arrive and an invoice states an
amount.
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
from django.utils import timezone

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
from apps.organizations.authorization import OutOfScope
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.procurement.models import (
    ProcurementDocumentSequence,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    Supplier,
)
from apps.procurement.permissions import (
    APPROVE_PURCHASE_ORDER,
    CREATE_PURCHASE_ORDER,
    ISSUE_PURCHASE_ORDER,
    VIEW_PURCHASE_ORDER,
    permissions_for_role,
)
from apps.procurement.selectors import resolve_purchase_order, visible_purchase_orders
from apps.procurement.services import (
    add_order_line,
    approve_purchase_order,
    cancel_purchase_order,
    create_purchase_order,
    create_supplier,
    issue_purchase_order,
    remove_order_line,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

HX = {"hx-request": "true"}
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
    return create_supplier(
        organization=organization,
        code="GROC-01",
        name_ar="مورد المواد",
        payment_terms_days=30,
    )


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
def draft(grocery: Supplier, branch: Branch, store: Warehouse, buyer: User) -> PurchaseOrder:
    return create_purchase_order(
        supplier=grocery,
        branch=branch,
        warehouse=store,
        created_by=buyer,
        ordered_on=ORDERED,
        expected_on=ORDERED + datetime.timedelta(days=7),
    )


@pytest.fixture
def stocked(draft: PurchaseOrder, rice: InventoryItem, sack: PackageUnit) -> PurchaseOrder:
    add_order_line(
        order=draft,
        item=rice,
        package_unit=sack,
        ordered_quantity=Decimal("4.000"),
        unit_price=Decimal("42000.000000"),
    )
    return draft


# ---------------------------------------------------------------------------
# Nothing moves and nothing is owed
# ---------------------------------------------------------------------------


class TestNoLedgerEffect:
    def test_no_stock_and_no_journal_at_any_status(
        self, stocked: PurchaseOrder, buyer: User, approver: User
    ) -> None:
        for step in (
            lambda: None,
            lambda: approve_purchase_order(order=stocked, actor=approver),
            lambda: issue_purchase_order(order=stocked, actor=buyer),
        ):
            step()
            assert StockMovement.objects.count() == 0
            assert StockLedgerEntry.objects.count() == 0
            assert JournalEntry.objects.count() == 0

    def test_a_cancelled_order_posts_nothing_either(
        self, stocked: PurchaseOrder, approver: User
    ) -> None:
        cancel_purchase_order(order=stocked, actor=approver, reason="المورد اعتذر")
        assert StockMovement.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_the_model_has_no_posting_columns(self) -> None:
        names = {field.name for field in PurchaseOrder._meta.get_fields()}
        assert not {"stock_entry", "journal_entry", "posted_at", "posted_by"} & names


# ---------------------------------------------------------------------------
# Arithmetic and snapshots
# ---------------------------------------------------------------------------


class TestArithmeticAndSnapshots:
    def test_the_line_total_is_quantity_times_price(self, stocked: PurchaseOrder) -> None:
        """4 sacks × 42,000 = 168,000, worked out by hand."""
        line = stocked.lines.get()
        assert line.line_total == Decimal("168000.000")
        assert line.ordered_base_quantity == Decimal("120.000")

    def test_the_order_total_is_the_sum_of_its_lines(
        self, stocked: PurchaseOrder, rice: InventoryItem
    ) -> None:
        add_order_line(
            order=stocked,
            item=rice,
            ordered_quantity=Decimal("10.000"),
            unit_price=Decimal("1500.000000"),
        )
        assert stocked.total_amount == Decimal("183000.000")

    def test_no_total_column_exists_to_disagree_with_the_lines(self) -> None:
        assert "total_amount" not in {field.name for field in PurchaseOrder._meta.get_fields()}

    def test_payment_terms_are_snapshotted_from_the_supplier(
        self, draft: PurchaseOrder, grocery: Supplier
    ) -> None:
        """
        Copied at creation, never read live afterwards. An order placed in
        January must keep January's terms when March renegotiates them.
        """
        assert draft.payment_terms_days == 30

        from apps.procurement.services import update_supplier

        update_supplier(supplier=grocery, name_ar=grocery.name_ar, payment_terms_days=60)
        draft.refresh_from_db()
        assert draft.payment_terms_days == 30

    def test_a_package_line_snapshots_its_conversion(self, stocked: PurchaseOrder) -> None:
        line = stocked.lines.get()
        assert line.conversion is not None
        assert line.conversion_version == line.conversion.version
        assert line.conversion_factor == Decimal("30.000000000000")

    def test_a_package_the_item_cannot_convert_is_refused(
        self, draft: PurchaseOrder, rice: InventoryItem, organization: Organization
    ) -> None:
        from apps.inventory.services import create_package_unit

        box = create_package_unit(organization=organization, code="BOX", name_ar="علبة")
        with pytest.raises(ValidationError) as refused:
            add_order_line(
                order=draft,
                item=rice,
                package_unit=box,
                ordered_quantity=Decimal("1.000"),
                unit_price=Decimal("1.000000"),
            )
        assert refused.value.code == "no_conversion_for_package"

    def test_the_database_refuses_a_non_positive_quantity(self, stocked: PurchaseOrder) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseOrderLine.objects.filter(order=stocked).update(
                ordered_quantity=Decimal("0.000")
            )


# ---------------------------------------------------------------------------
# Lifecycle and maker-checker
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_a_draft_carries_no_number_until_approval(
        self, stocked: PurchaseOrder, approver: User
    ) -> None:
        assert stocked.number == ""
        approved = approve_purchase_order(order=stocked, actor=approver)
        assert approved.number == "PO-2026-000001"

    def test_numbers_are_gapless_per_organization_and_year(
        self,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        buyer: User,
        approver: User,
        rice: InventoryItem,
    ) -> None:
        numbers = []
        for _ in range(3):
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
                ordered_quantity=Decimal("1.000"),
                unit_price=Decimal("1.000000"),
            )
            numbers.append(approve_purchase_order(order=order, actor=approver).number)
        assert numbers == ["PO-2026-000001", "PO-2026-000002", "PO-2026-000003"]
        assert (
            ProcurementDocumentSequence.objects.get(
                organization=branch.organization,
                document_type="PURCHASE_ORDER",
                year=2026,
            ).last_number
            == 3
        )

    def test_an_empty_order_cannot_be_approved(self, draft: PurchaseOrder, approver: User) -> None:
        with pytest.raises(ValidationError) as refused:
            approve_purchase_order(order=draft, actor=approver)
        assert refused.value.code == "order_has_no_lines"

    def test_the_preparer_cannot_approve_their_own_order(
        self, stocked: PurchaseOrder, buyer: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            approve_purchase_order(order=stocked, actor=buyer)
        assert refused.value.code == "maker_is_not_checker"

    def test_the_database_refuses_a_self_approval(
        self, stocked: PurchaseOrder, buyer: User
    ) -> None:
        """A spending commitment is exactly what somebody routes around."""
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseOrder.objects.filter(pk=stocked.pk).update(
                approved_by=buyer, approved_at=timezone.now()
            )

    def test_an_approved_order_is_frozen(
        self, stocked: PurchaseOrder, approver: User, rice: InventoryItem
    ) -> None:
        approve_purchase_order(order=stocked, actor=approver)
        with pytest.raises(ValidationError) as refused:
            add_order_line(
                order=stocked,
                item=rice,
                ordered_quantity=Decimal("1.000"),
                unit_price=Decimal("1.000000"),
            )
        assert refused.value.code == "order_not_editable"

    def test_a_line_cannot_be_removed_after_approval(
        self, stocked: PurchaseOrder, approver: User
    ) -> None:
        line = stocked.lines.get()
        approve_purchase_order(order=stocked, actor=approver)
        with pytest.raises(ValidationError):
            remove_order_line(line=line)

    def test_only_an_approved_order_may_be_issued(
        self, stocked: PurchaseOrder, buyer: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            issue_purchase_order(order=stocked, actor=buyer)
        assert refused.value.code == "illegal_transition"

    def test_issuing_records_who_sent_it_and_when(
        self, stocked: PurchaseOrder, approver: User, buyer: User
    ) -> None:
        approve_purchase_order(order=stocked, actor=approver)
        issued = issue_purchase_order(order=stocked, actor=buyer)
        assert issued.status == PurchaseOrderStatus.ISSUED
        assert issued.issued_by_id == buyer.pk
        assert issued.issued_at is not None

    def test_cancelling_needs_a_reason_and_is_terminal(
        self, stocked: PurchaseOrder, approver: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            cancel_purchase_order(order=stocked, actor=approver, reason="  ")
        assert refused.value.code == "reason_required"

        cancel_purchase_order(order=stocked, actor=approver, reason="لم نعد بحاجة")
        with pytest.raises(ValidationError) as again:
            cancel_purchase_order(order=stocked, actor=approver, reason="مرة أخرى")
        assert again.value.code == "already_cancelled"

    def test_the_database_refuses_a_cancellation_with_no_reason(
        self, stocked: PurchaseOrder
    ) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseOrder.objects.filter(pk=stocked.pk).update(status="CANCELLED")

    def test_every_transition_is_audited(
        self, stocked: PurchaseOrder, approver: User, buyer: User
    ) -> None:
        approve_purchase_order(order=stocked, actor=approver)
        issue_purchase_order(order=stocked, actor=buyer)
        actions = list(
            AuditEvent.objects.filter(
                target_type="procurement.PurchaseOrder", target_id=str(stocked.pk)
            )
            .order_by("id")
            .values_list("action", flat=True)
        )
        assert actions == ["CREATED", "APPROVED", "SUBMITTED"]


# ---------------------------------------------------------------------------
# Scope and source documents
# ---------------------------------------------------------------------------


class TestScopeAndSources:
    def test_a_warehouse_from_another_branch_is_refused(
        self, grocery: Supplier, branch: Branch, buyer: User, organization: Organization
    ) -> None:
        from apps.inventory.services import create_warehouse
        from apps.organizations.services import create_branch

        other = create_branch(
            organization=organization,
            code="FARBR",
            name_ar="فرع بعيد",
            name_en="Far",
            business_day_start_time=datetime.time(9, 0),
        )
        elsewhere = create_warehouse(branch=other, code="FARW", name_ar="بعيد")
        with pytest.raises(ValidationError) as refused:
            create_purchase_order(
                supplier=grocery,
                branch=branch,
                warehouse=elsewhere,
                created_by=buyer,
                ordered_on=ORDERED,
            )
        assert refused.value.code == "warehouse_branch_mismatch"

    def test_a_supplier_from_another_organization_is_refused(
        self,
        branch: Branch,
        store: Warehouse,
        buyer: User,
        other_organization: Organization,
    ) -> None:
        theirs = create_supplier(organization=other_organization, code="RIVAL-01", name_ar="منافس")
        with pytest.raises(ValidationError) as refused:
            create_purchase_order(
                supplier=theirs,
                branch=branch,
                warehouse=store,
                created_by=buyer,
                ordered_on=ORDERED,
            )
        assert refused.value.code == "organization_mismatch"

    def test_a_delivery_expected_before_the_order_is_refused(
        self, grocery: Supplier, branch: Branch, store: Warehouse, buyer: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            create_purchase_order(
                supplier=grocery,
                branch=branch,
                warehouse=store,
                created_by=buyer,
                ordered_on=ORDERED,
                expected_on=ORDERED - datetime.timedelta(days=1),
            )
        assert refused.value.code == "expected_before_ordered"

    def test_another_branchs_order_is_out_of_scope(
        self, manager: User, organization: Organization, buyer: User
    ) -> None:
        from apps.inventory.services import create_warehouse
        from apps.organizations.services import create_branch

        other = create_branch(
            organization=organization,
            code="OTHERBR",
            name_ar="فرع",
            name_en="Other",
            business_day_start_time=datetime.time(9, 0),
        )
        theirs = create_purchase_order(
            supplier=create_supplier(organization=organization, code="X-01", name_ar="مورد"),
            branch=other,
            warehouse=create_warehouse(branch=other, code="W", name_ar="م"),
            created_by=buyer,
            ordered_on=ORDERED,
        )
        assert theirs.pk not in set(visible_purchase_orders(manager).values_list("pk", flat=True))
        with pytest.raises(OutOfScope):
            resolve_purchase_order(manager, theirs.pk)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_purchasing_prepares_and_issues_but_never_approves(self) -> None:
        """Whoever chose the supplier does not also authorise the spend."""
        held = permissions_for_role(Role.PURCHASING)
        assert CREATE_PURCHASE_ORDER in held
        assert ISSUE_PURCHASE_ORDER in held
        assert APPROVE_PURCHASE_ORDER not in held

    def test_an_accounting_manager_approves_but_never_raises(self) -> None:
        held = permissions_for_role(Role.ACCOUNTING_MANAGER)
        assert APPROVE_PURCHASE_ORDER in held
        assert CREATE_PURCHASE_ORDER not in held

    def test_a_storekeeper_sees_orders_and_changes_nothing(self) -> None:
        held = permissions_for_role(Role.STOREKEEPER)
        assert VIEW_PURCHASE_ORDER in held
        assert CREATE_PURCHASE_ORDER not in held
        assert APPROVE_PURCHASE_ORDER not in held


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class TestScreens:
    def test_the_list_renders_and_filters_by_status(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        approver: User,
        stocked: PurchaseOrder,
    ) -> None:
        client = client_for(buyer)
        body = client.get(reverse("procurement:purchase_order_list")).content.decode()
        assert "GROC-01" in body

        approve_purchase_order(order=stocked, actor=approver)
        drafts = client.get(
            reverse("procurement:purchase_order_list"), {"status": "DRAFT"}, headers=HX
        ).content.decode()
        assert "GROC-01" not in drafts

    def test_an_hx_request_returns_only_the_results(
        self, client_for: Callable[[User], Client], buyer: User, stocked: PurchaseOrder
    ) -> None:
        body = (
            client_for(buyer)
            .get(reverse("procurement:purchase_order_list"), headers=HX)
            .content.decode()
        )
        assert "<html" not in body.lower()
        assert "GROC-01" in body

    def test_the_detail_shows_the_lines_and_the_total(
        self, client_for: Callable[[User], Client], buyer: User, stocked: PurchaseOrder
    ) -> None:
        body = (
            client_for(buyer)
            .get(reverse("procurement:purchase_order_detail", args=[stocked.pk]))
            .content.decode()
        )
        assert "RICE" in body
        assert "168000.000" in body
        assert "120.000" in body

    def test_a_buyer_cannot_approve_through_the_route(
        self, client_for: Callable[[User], Client], buyer: User, stocked: PurchaseOrder
    ) -> None:
        """Hiding the button is presentation; this is the protection."""
        response = client_for(buyer).post(
            reverse("procurement:purchase_order_approve", args=[stocked.pk])
        )
        assert response.status_code in {302, 403}
        stocked.refresh_from_db()
        assert stocked.status == PurchaseOrderStatus.DRAFT

    def test_an_approver_approves_and_a_buyer_issues(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        approver: User,
        stocked: PurchaseOrder,
    ) -> None:
        assert (
            client_for(approver)
            .post(reverse("procurement:purchase_order_approve", args=[stocked.pk]))
            .status_code
            == 302
        )
        stocked.refresh_from_db()
        assert stocked.status == PurchaseOrderStatus.APPROVED

        assert (
            client_for(buyer)
            .post(reverse("procurement:purchase_order_issue", args=[stocked.pk]))
            .status_code
            == 302
        )
        stocked.refresh_from_db()
        assert stocked.status == PurchaseOrderStatus.ISSUED

    def test_line_delete_is_post_only(
        self, client_for: Callable[[User], Client], buyer: User, stocked: PurchaseOrder
    ) -> None:
        line = stocked.lines.get()
        url = reverse("procurement:purchase_order_line_delete", args=[stocked.pk, line.pk])
        client = client_for(buyer)
        assert client.get(url).status_code == 405
        assert client.post(url).status_code == 302
        assert stocked.lines.count() == 0

    def test_a_line_from_another_order_is_a_404_on_this_route(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        grocery: Supplier,
        branch: Branch,
        store: Warehouse,
        stocked: PurchaseOrder,
        rice: InventoryItem,
    ) -> None:
        other = create_purchase_order(
            supplier=grocery,
            branch=branch,
            warehouse=store,
            created_by=buyer,
            ordered_on=ORDERED,
        )
        stray = add_order_line(
            order=other,
            item=rice,
            ordered_quantity=Decimal("1.000"),
            unit_price=Decimal("1.000000"),
        )
        response = client_for(buyer).post(
            reverse("procurement:purchase_order_line_delete", args=[stocked.pk, stray.pk])
        )
        assert response.status_code == 404
        assert PurchaseOrderLine.objects.filter(pk=stray.pk).exists()
