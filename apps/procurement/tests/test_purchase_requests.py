"""
Purchase requests: a statement of need that moves nothing.

The two claims worth proving hardest are the ones a reader would otherwise
have to take on trust. `TestNoLedgerEffect` asserts that no stock movement and
no journal entry exists after every transition, in every terminal state — not
because it is likely to break, but because the whole reason this document is
separate from the order is that it commits nobody to anything. And
`test_the_database_refuses_a_self_approval` goes past the service, because
maker-checker enforced only in Python is a promise rather than a rule.
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
    ItemCategory,
    ItemType,
    PackageUnit,
    StockMovement,
    Warehouse,
)
from apps.organizations.authorization import OutOfScope
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.procurement.models import (
    ProcurementDocumentSequence,
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
)
from apps.procurement.permissions import (
    APPROVE_PURCHASE_REQUEST,
    CREATE_PURCHASE_REQUEST,
    VIEW_PURCHASE_REQUEST,
    permissions_for_role,
)
from apps.procurement.selectors import resolve_purchase_request, visible_purchase_requests
from apps.procurement.services import (
    add_request_line,
    approve_purchase_request,
    cancel_purchase_request,
    create_purchase_request,
    reject_purchase_request,
    remove_request_line,
    submit_purchase_request,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

HX = {"hx-request": "true"}
REQUIRED = datetime.date(2026, 3, 15)


@pytest.fixture
def units() -> None:
    from django.core.management import call_command

    call_command("seed_units", verbosity=0)


@pytest.fixture
def kilogram(units: None) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="KG")


@pytest.fixture
def store(branch: Branch) -> Warehouse:
    from apps.inventory.services import create_warehouse

    return create_warehouse(branch=branch, code="MAIN", name="المخزن الرئيسي")


@pytest.fixture
def rice(organization: Organization, kilogram: UnitOfMeasure) -> InventoryItem:
    from apps.inventory.services import create_item, create_item_category

    category: ItemCategory = create_item_category(
        organization=organization, code="GRAINS", name="حبوب"
    )
    return create_item(
        organization=organization,
        code="RICE",
        name="رز",
        category=category,
        item_type=ItemType.RAW_MATERIAL,
        base_unit=kilogram,
    )


@pytest.fixture
def sack(organization: Organization, rice: InventoryItem) -> PackageUnit:
    from apps.inventory.services import create_item_conversion, create_package_unit

    package = create_package_unit(organization=organization, code="SACK", name="كيس")
    create_item_conversion(
        item=rice,
        package_unit=package,
        factor_to_base=Decimal("30.000000000000"),
        conversion_type=ConversionType.FIXED,
        effective_from=datetime.date(2026, 1, 1),
    )
    return package


@pytest.fixture
def approver(branch: Branch) -> User:
    """A second person, because one cannot both submit and decide."""
    user = User.objects.create_user(username="approver", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def draft(branch: Branch, keeper: User, store: Warehouse) -> PurchaseRequest:
    return create_purchase_request(
        branch=branch,
        requested_by=keeper,
        warehouse=store,
        required_date=REQUIRED,
        purpose="مخزون الأسبوع",
    )


@pytest.fixture
def stocked_draft(
    draft: PurchaseRequest, rice: InventoryItem, sack: PackageUnit
) -> PurchaseRequest:
    add_request_line(request=draft, item=rice, package_unit=sack, entered_quantity=Decimal("4.000"))
    return draft


# ---------------------------------------------------------------------------
# Nothing moves
# ---------------------------------------------------------------------------


class TestNoLedgerEffect:
    def test_no_stock_and_no_journal_in_any_status(
        self, stocked_draft: PurchaseRequest, keeper: User, approver: User
    ) -> None:
        """
        The claim the whole document rests on, asserted at every step rather
        than once at the end — a leak introduced at submission would otherwise
        be masked by a clean check after approval.
        """
        for step in (
            lambda: None,
            lambda: submit_purchase_request(request=stocked_draft, actor=keeper),
            lambda: approve_purchase_request(request=stocked_draft, actor=approver, reason="ok"),
        ):
            step()
            assert StockMovement.objects.count() == 0
            assert JournalEntry.objects.count() == 0

    def test_a_rejected_request_also_moves_nothing(
        self, stocked_draft: PurchaseRequest, keeper: User, approver: User
    ) -> None:
        submit_purchase_request(request=stocked_draft, actor=keeper)
        reject_purchase_request(request=stocked_draft, actor=approver, reason="كافٍ")
        assert StockMovement.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_the_model_has_no_posting_columns(self) -> None:
        names = {field.name for field in PurchaseRequest._meta.get_fields()}
        assert not {"stock_entry", "journal_entry", "posted_at", "posted_by"} & names


# ---------------------------------------------------------------------------
# Maker-checker
# ---------------------------------------------------------------------------


class TestMakerChecker:
    def test_the_submitter_cannot_approve(
        self, stocked_draft: PurchaseRequest, keeper: User
    ) -> None:
        submit_purchase_request(request=stocked_draft, actor=keeper)
        with pytest.raises(ValidationError) as refused:
            approve_purchase_request(request=stocked_draft, actor=keeper, reason="")
        assert refused.value.code == "maker_is_not_checker"

    def test_the_submitter_cannot_reject_either(
        self, stocked_draft: PurchaseRequest, keeper: User
    ) -> None:
        submit_purchase_request(request=stocked_draft, actor=keeper)
        with pytest.raises(ValidationError):
            reject_purchase_request(request=stocked_draft, actor=keeper, reason="لا")

    def test_the_database_refuses_a_self_approval(
        self, stocked_draft: PurchaseRequest, keeper: User
    ) -> None:
        """
        Past the service, straight at the table.

        A rule enforced only in Python is one management command away from not
        being enforced at all.
        """
        submitted = submit_purchase_request(request=stocked_draft, actor=keeper)
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseRequest.objects.filter(pk=submitted.pk).update(
                decided_by=keeper, decided_at=timezone.now()
            )

    def test_a_decision_without_a_timestamp_is_refused(
        self, stocked_draft: PurchaseRequest, keeper: User, approver: User
    ) -> None:
        submitted = submit_purchase_request(request=stocked_draft, actor=keeper)
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseRequest.objects.filter(pk=submitted.pk).update(decided_by=approver)

    def test_a_refusal_must_state_a_reason(
        self, stocked_draft: PurchaseRequest, keeper: User, approver: User
    ) -> None:
        submit_purchase_request(request=stocked_draft, actor=keeper)
        with pytest.raises(ValidationError) as refused:
            reject_purchase_request(request=stocked_draft, actor=approver, reason="   ")
        assert refused.value.code == "reason_required"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_a_draft_carries_no_number_until_submission(
        self, stocked_draft: PurchaseRequest, keeper: User
    ) -> None:
        """A draft that is abandoned must not burn a number out of the sequence."""
        assert stocked_draft.number == ""
        submitted = submit_purchase_request(request=stocked_draft, actor=keeper)
        assert submitted.number == "PR-2026-000001"

    def test_numbers_are_gapless_and_per_organization_and_year(
        self,
        branch: Branch,
        keeper: User,
        store: Warehouse,
        rice: InventoryItem,
    ) -> None:
        numbers = []
        for index in range(3):
            document = create_purchase_request(
                branch=branch,
                requested_by=keeper,
                warehouse=store,
                required_date=REQUIRED,
                purpose=f"طلب {index}",
            )
            add_request_line(request=document, item=rice, entered_quantity=Decimal("1.000"))
            numbers.append(submit_purchase_request(request=document, actor=keeper).number)
        assert numbers == ["PR-2026-000001", "PR-2026-000002", "PR-2026-000003"]
        sequence = ProcurementDocumentSequence.objects.get(
            organization=branch.organization, document_type="PURCHASE_REQUEST", year=2026
        )
        assert sequence.last_number == 3

    def test_an_empty_request_cannot_be_submitted(
        self, draft: PurchaseRequest, keeper: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            submit_purchase_request(request=draft, actor=keeper)
        assert refused.value.code == "request_has_no_lines"

    def test_a_submitted_request_is_frozen(
        self, stocked_draft: PurchaseRequest, keeper: User, rice: InventoryItem
    ) -> None:
        submit_purchase_request(request=stocked_draft, actor=keeper)
        with pytest.raises(ValidationError) as refused:
            add_request_line(request=stocked_draft, item=rice, entered_quantity=Decimal("1.000"))
        assert refused.value.code == "request_not_editable"

    def test_a_line_cannot_be_removed_after_submission(
        self, stocked_draft: PurchaseRequest, keeper: User
    ) -> None:
        line = stocked_draft.lines.get()
        submit_purchase_request(request=stocked_draft, actor=keeper)
        with pytest.raises(ValidationError):
            remove_request_line(line=line)

    def test_an_approved_request_cannot_be_approved_twice(
        self, stocked_draft: PurchaseRequest, keeper: User, approver: User
    ) -> None:
        submit_purchase_request(request=stocked_draft, actor=keeper)
        approve_purchase_request(request=stocked_draft, actor=approver, reason="ok")
        with pytest.raises(ValidationError) as refused:
            approve_purchase_request(request=stocked_draft, actor=approver, reason="ok")
        assert refused.value.code == "illegal_transition"

    def test_a_draft_may_be_cancelled_by_its_own_requester(
        self, stocked_draft: PurchaseRequest, keeper: User
    ) -> None:
        """
        The one place maker-checker does not apply: nobody submitted it, so
        there is no checker to be.
        """
        cancelled = cancel_purchase_request(
            request=stocked_draft, actor=keeper, reason="لم تعد مطلوبة"
        )
        assert cancelled.status == PurchaseRequestStatus.CANCELLED
        assert cancelled.decided_by is None

    def test_an_approved_request_may_still_be_cancelled(
        self, stocked_draft: PurchaseRequest, keeper: User, approver: User
    ) -> None:
        submit_purchase_request(request=stocked_draft, actor=keeper)
        approve_purchase_request(request=stocked_draft, actor=approver, reason="ok")
        cancelled = cancel_purchase_request(
            request=stocked_draft, actor=approver, reason="المورد اعتذر"
        )
        assert cancelled.status == PurchaseRequestStatus.CANCELLED

    def test_a_cancelled_request_is_terminal(
        self, stocked_draft: PurchaseRequest, keeper: User, approver: User
    ) -> None:
        submit_purchase_request(request=stocked_draft, actor=keeper)
        cancel_purchase_request(request=stocked_draft, actor=approver, reason="لا")
        with pytest.raises(ValidationError):
            approve_purchase_request(request=stocked_draft, actor=approver, reason="ok")

    def test_every_transition_is_audited(
        self, stocked_draft: PurchaseRequest, keeper: User, approver: User
    ) -> None:
        submit_purchase_request(request=stocked_draft, actor=keeper)
        approve_purchase_request(request=stocked_draft, actor=approver, reason="ok")
        actions = list(
            AuditEvent.objects.filter(
                target_type="procurement.PurchaseRequest",
                target_id=str(stocked_draft.pk),
            )
            .order_by("id")
            .values_list("action", flat=True)
        )
        assert actions == ["CREATED", "SUBMITTED", "APPROVED"]


# ---------------------------------------------------------------------------
# Lines and the conversion snapshot
# ---------------------------------------------------------------------------


class TestLines:
    def test_a_package_line_snapshots_its_conversion(self, stocked_draft: PurchaseRequest) -> None:
        line = stocked_draft.lines.get()
        assert line.conversion is not None
        assert line.conversion_version == line.conversion.version
        assert line.conversion_factor == Decimal("30.000000000000")
        assert line.base_quantity == Decimal("120.000")

    def test_a_base_unit_line_carries_no_conversion(
        self, draft: PurchaseRequest, rice: InventoryItem
    ) -> None:
        line = add_request_line(request=draft, item=rice, entered_quantity=Decimal("25.000"))
        assert line.conversion is None
        assert line.conversion_factor is None
        assert line.base_quantity == Decimal("25.000")

    def test_a_package_the_item_cannot_convert_is_refused(
        self, draft: PurchaseRequest, rice: InventoryItem, organization: Organization
    ) -> None:
        from apps.inventory.services import create_package_unit

        box = create_package_unit(organization=organization, code="BOX", name="علبة")
        with pytest.raises(ValidationError) as refused:
            add_request_line(
                request=draft, item=rice, package_unit=box, entered_quantity=Decimal("1.000")
            )
        assert refused.value.code == "no_conversion_for_package"

    def test_a_zero_quantity_is_refused(self, draft: PurchaseRequest, rice: InventoryItem) -> None:
        with pytest.raises(ValidationError):
            add_request_line(request=draft, item=rice, entered_quantity=Decimal("0.000"))

    def test_the_database_refuses_a_non_positive_quantity_too(
        self, stocked_draft: PurchaseRequest
    ) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            PurchaseRequestLine.objects.filter(request=stocked_draft).update(
                entered_quantity=Decimal("0.000")
            )

    def test_one_item_and_package_appears_once_per_request(
        self, stocked_draft: PurchaseRequest, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        with pytest.raises(ValidationError):
            add_request_line(
                request=stocked_draft,
                item=rice,
                package_unit=sack,
                entered_quantity=Decimal("2.000"),
            )

    def test_the_same_item_in_a_different_package_is_a_separate_line(
        self, stocked_draft: PurchaseRequest, rice: InventoryItem
    ) -> None:
        line = add_request_line(request=stocked_draft, item=rice, entered_quantity=Decimal("5.000"))
        assert line.package_unit is None
        assert stocked_draft.lines.count() == 2

    def test_an_item_from_another_organization_is_refused(
        self, draft: PurchaseRequest, other_organization: Organization, kilogram: UnitOfMeasure
    ) -> None:
        from apps.inventory.services import create_item, create_item_category

        theirs = create_item(
            organization=other_organization,
            code="THEIRS",
            name="صنف",
            category=create_item_category(organization=other_organization, code="X", name="س"),
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        with pytest.raises(ValidationError) as refused:
            add_request_line(request=draft, item=theirs, entered_quantity=Decimal("1.000"))
        assert refused.value.code == "organization_mismatch"

    def test_a_warehouse_from_another_branch_is_refused(
        self, organization: Organization, branch: Branch, keeper: User
    ) -> None:
        from apps.inventory.services import create_warehouse
        from apps.organizations.services import create_branch

        other = create_branch(
            organization=organization,
            code="KARRADA",
            name="الكرادة",
            business_day_start_time=datetime.time(9, 0),
        )
        elsewhere = create_warehouse(branch=other, code="FAR", name="بعيد")
        with pytest.raises(ValidationError) as refused:
            create_purchase_request(
                branch=branch,
                requested_by=keeper,
                warehouse=elsewhere,
                required_date=REQUIRED,
                purpose="خطأ",
            )
        assert refused.value.code == "warehouse_branch_mismatch"


# ---------------------------------------------------------------------------
# Scope and permissions
# ---------------------------------------------------------------------------


class TestScopeAndPermissions:
    def test_another_branchs_request_is_out_of_scope(
        self, manager: User, organization: Organization, keeper: User
    ) -> None:
        from apps.inventory.services import create_warehouse
        from apps.organizations.services import create_branch

        other = create_branch(
            organization=organization,
            code="FARBR",
            name="فرع بعيد",
            business_day_start_time=datetime.time(9, 0),
        )
        far_user = User.objects.create_user(username="far", password="pw-not-real-1234")
        grant_branch_access(user=far_user, branch=other, role=Role.MANAGER)
        theirs = create_purchase_request(
            branch=other,
            requested_by=far_user,
            warehouse=create_warehouse(branch=other, code="FARW", name="مخزن"),
            required_date=REQUIRED,
            purpose="طلبهم",
        )
        assert theirs.pk not in set(visible_purchase_requests(manager).values_list("pk", flat=True))
        with pytest.raises(OutOfScope):
            resolve_purchase_request(manager, theirs.pk)

    def test_a_storekeeper_asks_and_does_not_approve(self) -> None:
        held = permissions_for_role(Role.STOREKEEPER)
        assert CREATE_PURCHASE_REQUEST in held
        assert APPROVE_PURCHASE_REQUEST not in held

    def test_an_accounting_manager_approves_and_does_not_ask(self) -> None:
        held = permissions_for_role(Role.ACCOUNTING_MANAGER)
        assert APPROVE_PURCHASE_REQUEST in held
        assert CREATE_PURCHASE_REQUEST not in held

    def test_a_viewer_reads_and_does_nothing_else(self) -> None:
        held = permissions_for_role(Role.VIEWER)
        assert VIEW_PURCHASE_REQUEST in held
        assert CREATE_PURCHASE_REQUEST not in held


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class TestScreens:
    def test_the_list_renders_and_filters_by_status(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        stocked_draft: PurchaseRequest,
    ) -> None:
        client = client_for(keeper)
        body = client.get(reverse("procurement:purchase_request_list")).content.decode()
        assert "مخزون الأسبوع" in body

        submit_purchase_request(request=stocked_draft, actor=keeper)
        filtered = client.get(
            reverse("procurement:purchase_request_list"), {"status": "DRAFT"}, headers=HX
        ).content.decode()
        assert "مخزون الأسبوع" not in filtered

    def test_an_hx_request_returns_only_the_results(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        stocked_draft: PurchaseRequest,
    ) -> None:
        body = (
            client_for(keeper)
            .get(reverse("procurement:purchase_request_list"), headers=HX)
            .content.decode()
        )
        assert "<html" not in body.lower()
        assert "مخزون الأسبوع" in body

    def test_the_detail_screen_shows_the_lines(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        stocked_draft: PurchaseRequest,
    ) -> None:
        body = (
            client_for(keeper)
            .get(reverse("procurement:purchase_request_detail", args=[stocked_draft.pk]))
            .content.decode()
        )
        assert "RICE" in body
        assert "120.000" in body

    def test_submitting_through_the_screen_draws_a_number(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        stocked_draft: PurchaseRequest,
    ) -> None:
        response = client_for(keeper).post(
            reverse("procurement:purchase_request_submit", args=[stocked_draft.pk])
        )
        assert response.status_code == 302
        stocked_draft.refresh_from_db()
        assert stocked_draft.status == PurchaseRequestStatus.SUBMITTED
        assert stocked_draft.number.startswith("PR-2026-")

    def test_a_storekeeper_cannot_approve_through_the_route(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        stocked_draft: PurchaseRequest,
    ) -> None:
        """Hiding the button is presentation; this is the protection."""
        submit_purchase_request(request=stocked_draft, actor=keeper)
        response = client_for(keeper).post(
            reverse("procurement:purchase_request_approve", args=[stocked_draft.pk])
        )
        assert response.status_code in {302, 403}
        stocked_draft.refresh_from_db()
        assert stocked_draft.status == PurchaseRequestStatus.SUBMITTED

    def test_an_approver_approves_through_the_route(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        approver: User,
        stocked_draft: PurchaseRequest,
    ) -> None:
        submit_purchase_request(request=stocked_draft, actor=keeper)
        response = client_for(approver).post(
            reverse("procurement:purchase_request_approve", args=[stocked_draft.pk])
        )
        assert response.status_code == 302
        stocked_draft.refresh_from_db()
        assert stocked_draft.status == PurchaseRequestStatus.APPROVED

    def test_line_delete_is_post_only(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        stocked_draft: PurchaseRequest,
    ) -> None:
        line = stocked_draft.lines.get()
        url = reverse("procurement:purchase_request_line_delete", args=[stocked_draft.pk, line.pk])
        client = client_for(keeper)
        assert client.get(url).status_code == 405
        assert client.post(url).status_code == 302
        assert stocked_draft.lines.count() == 0

    def test_a_line_from_another_request_is_a_404_on_this_route(
        self,
        client_for: Callable[[User], Client],
        keeper: User,
        branch: Branch,
        store: Warehouse,
        stocked_draft: PurchaseRequest,
        rice: InventoryItem,
    ) -> None:
        """
        The parent is part of the lookup. Without it a line id belonging to a
        different document would resolve and this route would act on it.
        """
        other = create_purchase_request(
            branch=branch,
            requested_by=keeper,
            warehouse=store,
            required_date=REQUIRED,
            purpose="طلب آخر",
        )
        stray = add_request_line(request=other, item=rice, entered_quantity=Decimal("1.000"))
        response = client_for(keeper).post(
            reverse(
                "procurement:purchase_request_line_delete",
                args=[stocked_draft.pk, stray.pk],
            )
        )
        assert response.status_code == 404
        assert PurchaseRequestLine.objects.filter(pk=stray.pk).exists()
