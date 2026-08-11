"""
Supplier quotations: evidence, not commitment.

The arithmetic tests matter most here. A quotation is the first procurement
document that carries money, and every later comparison, order and invoice
inherits the precision decisions made in `add_quotation_line` — so the line
total, the base unit price and the document total are each asserted against a
figure worked out by hand rather than against the code's own output.
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
    ItemType,
    PackageUnit,
    StockMovement,
    Warehouse,
)
from apps.organizations.authorization import OutOfScope
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_branch_access
from apps.procurement.models import (
    SupplierQuotation,
    SupplierQuotationLine,
    SupplierQuotationStatus,
)
from apps.procurement.permissions import (
    AWARD_QUOTATION,
    MANAGE_QUOTATIONS,
    VIEW_QUOTATION,
    permissions_for_role,
)
from apps.procurement.selectors import resolve_quotation, visible_quotations
from apps.procurement.services import (
    add_quotation_line,
    add_request_line,
    approve_purchase_request,
    create_purchase_request,
    create_supplier,
    create_supplier_quotation,
    decline_supplier_quotation,
    remove_quotation_line,
    submit_purchase_request,
    submit_supplier_quotation,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

HX = {"hx-request": "true"}
QUOTED = datetime.date(2026, 2, 1)


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
def grocery(organization: Organization) -> object:
    return create_supplier(organization=organization, code="GROC-01", name_ar="مورد المواد")


@pytest.fixture
def buyer(branch: Branch) -> User:
    user = User.objects.create_user(username="buyer", password="pw-not-real-1234")
    grant_branch_access(user=user, branch=branch, role=Role.PURCHASING)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def draft(grocery: object, buyer: User) -> SupplierQuotation:
    return create_supplier_quotation(
        supplier=grocery,  # type: ignore[arg-type]
        recorded_by=buyer,
        quoted_at=QUOTED,
        valid_until=QUOTED + datetime.timedelta(days=30),
        supplier_reference="Q-2026-88",
        freight_amount=Decimal("15000.000"),
        evidence_reference="بريد المورد",
    )


@pytest.fixture
def priced(draft: SupplierQuotation, rice: InventoryItem, sack: PackageUnit) -> SupplierQuotation:
    add_quotation_line(
        quotation=draft,
        item=rice,
        package_unit=sack,
        quantity=Decimal("4.000"),
        unit_price=Decimal("42000.000000"),
    )
    return draft


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


class TestTheArithmetic:
    def test_the_line_total_is_quantity_times_price(self, priced: SupplierQuotation) -> None:
        """4 sacks × 42,000 = 168,000, worked out by hand."""
        line = priced.lines.get()
        assert line.line_total == Decimal("168000.000")
        assert line.base_quantity == Decimal("120.000")

    def test_the_base_unit_price_is_derived_and_never_stored(
        self, priced: SupplierQuotation
    ) -> None:
        """
        168,000 over 120 kg is 1,400 per kg — the only figure two suppliers
        quoting different package sizes can honestly be compared on.
        """
        line = priced.lines.get()
        assert line.base_unit_price == Decimal("1400.000000")
        assert "base_unit_price" not in {
            field.name for field in SupplierQuotationLine._meta.get_fields()
        }

    def test_the_document_total_is_the_sum_of_lines_plus_charges(
        self, priced: SupplierQuotation, rice: InventoryItem
    ) -> None:
        add_quotation_line(
            quotation=priced,
            item=rice,
            quantity=Decimal("10.000"),
            unit_price=Decimal("1500.000000"),
        )
        assert priced.line_total == Decimal("183000.000")
        assert priced.total_amount == Decimal("198000.000")

    def test_no_total_column_exists_to_disagree_with_the_lines(self) -> None:
        names = {field.name for field in SupplierQuotation._meta.get_fields()}
        assert "total_amount" not in names
        assert "line_total" not in names

    def test_a_fractional_price_rounds_once_at_the_boundary(
        self, draft: SupplierQuotation, rice: InventoryItem
    ) -> None:
        """
        3 × 1,333.333333 is 3,999.999999, which stores as 4,000.000 at three
        places. Quantizing the price first and multiplying afterwards would
        give the same answer here; quantizing the product twice would not, and
        this is the boundary the rule is about.
        """
        line = add_quotation_line(
            quotation=draft,
            item=rice,
            quantity=Decimal("3.000"),
            unit_price=Decimal("1333.333333"),
        )
        assert line.line_total == Decimal("4000.000")

    def test_a_zero_price_is_a_legitimate_quote(
        self, draft: SupplierQuotation, rice: InventoryItem
    ) -> None:
        """A free sample is a real offer; a negative price is not."""
        line = add_quotation_line(
            quotation=draft,
            item=rice,
            quantity=Decimal("1.000"),
            unit_price=Decimal("0.000000"),
        )
        assert line.line_total == Decimal("0.000")

    def test_a_negative_price_is_refused(
        self, draft: SupplierQuotation, rice: InventoryItem
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            add_quotation_line(
                quotation=draft,
                item=rice,
                quantity=Decimal("1.000"),
                unit_price=Decimal("-1.000000"),
            )
        assert refused.value.code == "price_negative"

    def test_the_database_refuses_a_negative_price_too(self, priced: SupplierQuotation) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierQuotationLine.objects.filter(quotation=priced).update(
                unit_price=Decimal("-1.000000")
            )


# ---------------------------------------------------------------------------
# Nothing moves
# ---------------------------------------------------------------------------


class TestNoLedgerEffect:
    def test_no_stock_and_no_journal_in_any_status(
        self, priced: SupplierQuotation, buyer: User
    ) -> None:
        submit_supplier_quotation(quotation=priced, actor=buyer)
        assert StockMovement.objects.count() == 0
        assert JournalEntry.objects.count() == 0

        decline_supplier_quotation(quotation=priced, actor=buyer, reason="أغلى")
        assert StockMovement.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_the_model_has_no_posting_columns(self) -> None:
        names = {field.name for field in SupplierQuotation._meta.get_fields()}
        assert not {"stock_entry", "journal_entry", "posted_at"} & names


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_submission_draws_a_number(self, priced: SupplierQuotation, buyer: User) -> None:
        assert priced.number == ""
        submitted = submit_supplier_quotation(quotation=priced, actor=buyer)
        assert submitted.number == "QT-2026-000001"
        assert submitted.status == SupplierQuotationStatus.SUBMITTED

    def test_an_empty_quotation_cannot_be_submitted(
        self, draft: SupplierQuotation, buyer: User
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            submit_supplier_quotation(quotation=draft, actor=buyer)
        assert refused.value.code == "quotation_has_no_lines"

    def test_evidence_is_required_before_submission(
        self, grocery: object, buyer: User, rice: InventoryItem
    ) -> None:
        """A price nobody can trace back to what the supplier sent is a rumour."""
        bare = create_supplier_quotation(
            supplier=grocery,  # type: ignore[arg-type]
            recorded_by=buyer,
            quoted_at=QUOTED,
        )
        add_quotation_line(
            quotation=bare,
            item=rice,
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.000000"),
        )
        with pytest.raises(ValidationError) as refused:
            submit_supplier_quotation(quotation=bare, actor=buyer)
        assert refused.value.code == "evidence_required"

    def test_a_submitted_quotation_is_frozen(
        self, priced: SupplierQuotation, buyer: User, rice: InventoryItem
    ) -> None:
        submit_supplier_quotation(quotation=priced, actor=buyer)
        with pytest.raises(ValidationError) as refused:
            add_quotation_line(
                quotation=priced,
                item=rice,
                quantity=Decimal("1.000"),
                unit_price=Decimal("1.000000"),
            )
        assert refused.value.code == "quotation_not_editable"

    def test_a_line_cannot_be_removed_after_submission(
        self, priced: SupplierQuotation, buyer: User
    ) -> None:
        line = priced.lines.get()
        submit_supplier_quotation(quotation=priced, actor=buyer)
        with pytest.raises(ValidationError):
            remove_quotation_line(line=line)

    def test_a_declined_quotation_is_kept_not_deleted(
        self, priced: SupplierQuotation, buyer: User
    ) -> None:
        """An award means nothing without the offers it was chosen over."""
        submit_supplier_quotation(quotation=priced, actor=buyer)
        declined = decline_supplier_quotation(
            quotation=priced, actor=buyer, reason="سعر أعلى بعد النقل"
        )
        assert declined.status == SupplierQuotationStatus.DECLINED
        assert SupplierQuotation.objects.filter(pk=priced.pk).exists()
        assert declined.lines.count() == 1

    def test_declining_needs_a_reason(self, priced: SupplierQuotation, buyer: User) -> None:
        submit_supplier_quotation(quotation=priced, actor=buyer)
        with pytest.raises(ValidationError) as refused:
            decline_supplier_quotation(quotation=priced, actor=buyer, reason="  ")
        assert refused.value.code == "reason_required"

    def test_a_declined_quotation_cannot_be_declined_again(
        self, priced: SupplierQuotation, buyer: User
    ) -> None:
        submit_supplier_quotation(quotation=priced, actor=buyer)
        decline_supplier_quotation(quotation=priced, actor=buyer, reason="لا")
        with pytest.raises(ValidationError) as refused:
            decline_supplier_quotation(quotation=priced, actor=buyer, reason="لا")
        assert refused.value.code == "illegal_transition"

    def test_every_transition_is_audited(self, priced: SupplierQuotation, buyer: User) -> None:
        submit_supplier_quotation(quotation=priced, actor=buyer)
        actions = list(
            AuditEvent.objects.filter(
                target_type="procurement.SupplierQuotation", target_id=str(priced.pk)
            )
            .order_by("id")
            .values_list("action", flat=True)
        )
        assert actions == ["CREATED", "SUBMITTED"]


# ---------------------------------------------------------------------------
# Validity, duplicates and scope
# ---------------------------------------------------------------------------


class TestValidityAndDuplicates:
    def test_expiring_before_it_was_given_is_refused(self, grocery: object, buyer: User) -> None:
        with pytest.raises(ValidationError) as refused:
            create_supplier_quotation(
                supplier=grocery,  # type: ignore[arg-type]
                recorded_by=buyer,
                quoted_at=QUOTED,
                valid_until=QUOTED - datetime.timedelta(days=1),
            )
        assert refused.value.code == "validity_reversed"

    def test_the_same_supplier_reference_cannot_be_entered_twice(
        self, draft: SupplierQuotation, grocery: object, buyer: User
    ) -> None:
        """The cheapest protection against comparing a supplier with themselves."""
        with pytest.raises(ValidationError):
            create_supplier_quotation(
                supplier=grocery,  # type: ignore[arg-type]
                recorded_by=buyer,
                quoted_at=QUOTED,
                supplier_reference="Q-2026-88",
            )

    def test_another_supplier_may_reuse_the_reference(
        self, draft: SupplierQuotation, organization: Organization, buyer: User
    ) -> None:
        other = create_supplier(organization=organization, code="MEAT-01", name_ar="مورد اللحوم")
        twin = create_supplier_quotation(
            supplier=other,
            recorded_by=buyer,
            quoted_at=QUOTED,
            supplier_reference="Q-2026-88",
        )
        assert twin.pk != draft.pk

    def test_only_approved_requests_may_be_quoted_against(
        self,
        branch: Branch,
        keeper: User,
        buyer: User,
        grocery: object,
        rice: InventoryItem,
    ) -> None:
        """
        The service accepts any request in the organization; the **form** is
        what restricts the choice to approved ones. Asserted here so the
        difference is deliberate rather than accidental — a quotation recorded
        against a draft request is data entry running ahead of a decision, and
        it is recoverable; a service that refused it would lose the price.
        """
        from apps.inventory.services import create_warehouse
        from apps.procurement.forms import SupplierQuotationForm

        store: Warehouse = create_warehouse(branch=branch, code="MAIN", name_ar="مخزن")
        drafted = create_purchase_request(
            branch=branch,
            requested_by=keeper,
            warehouse=store,
            required_date=QUOTED + datetime.timedelta(days=30),
            purpose="مسودة",
        )
        add_request_line(request=drafted, item=rice, entered_quantity=Decimal("1.000"))

        offered = SupplierQuotationForm(actor=buyer).fields["request"].queryset  # type: ignore[attr-defined]
        assert drafted.pk not in set(offered.values_list("pk", flat=True))

        submitted = submit_purchase_request(request=drafted, actor=keeper)
        approver = User.objects.create_user(username="ap", password="pw-not-real-1234")
        grant_branch_access(user=approver, branch=branch, role=Role.ACCOUNTING_MANAGER)
        approve_purchase_request(
            request=submitted, actor=User.objects.get(pk=approver.pk), reason="ok"
        )

        offered_after = SupplierQuotationForm(actor=buyer).fields["request"].queryset  # type: ignore[attr-defined]
        assert drafted.pk in set(offered_after.values_list("pk", flat=True))

    def test_a_request_from_another_organization_is_refused(
        self, grocery: object, buyer: User, other_organization: Organization
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
        their_request = create_purchase_request(
            branch=their_branch,
            requested_by=buyer,
            warehouse=create_warehouse(branch=their_branch, code="W", name_ar="م"),
            required_date=QUOTED,
            purpose="طلبهم",
        )
        with pytest.raises(ValidationError) as refused:
            create_supplier_quotation(
                supplier=grocery,  # type: ignore[arg-type]
                recorded_by=buyer,
                quoted_at=QUOTED,
                request=their_request,
            )
        assert refused.value.code == "organization_mismatch"

    def test_another_organizations_quotation_is_out_of_scope(
        self, manager: User, other_organization: Organization, buyer: User
    ) -> None:
        theirs = create_supplier_quotation(
            supplier=create_supplier(
                organization=other_organization, code="RIVAL-01", name_ar="منافس"
            ),
            recorded_by=buyer,
            quoted_at=QUOTED,
        )
        assert theirs.pk not in set(visible_quotations(manager).values_list("pk", flat=True))
        with pytest.raises(OutOfScope):
            resolve_quotation(manager, theirs.pk)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_purchasing_records_and_awards(self) -> None:
        held = permissions_for_role(Role.PURCHASING)
        assert {VIEW_QUOTATION, MANAGE_QUOTATIONS, AWARD_QUOTATION} <= held

    def test_an_accounting_manager_reads_and_does_not_record(self) -> None:
        held = permissions_for_role(Role.ACCOUNTING_MANAGER)
        assert VIEW_QUOTATION in held
        assert MANAGE_QUOTATIONS not in held
        assert AWARD_QUOTATION not in held

    def test_a_storekeeper_sees_no_quotations_at_all(self) -> None:
        """Prices are none of their business, and neither is who offered them."""
        assert VIEW_QUOTATION not in permissions_for_role(Role.STOREKEEPER)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class TestScreens:
    def test_the_list_renders_and_filters_by_status(
        self, client_for: Callable[[User], Client], buyer: User, priced: SupplierQuotation
    ) -> None:
        client = client_for(buyer)
        body = client.get(reverse("procurement:quotation_list")).content.decode()
        assert "GROC-01" in body

        submit_supplier_quotation(quotation=priced, actor=buyer)
        drafts = client.get(
            reverse("procurement:quotation_list"), {"status": "DRAFT"}, headers=HX
        ).content.decode()
        assert "GROC-01" not in drafts

    def test_an_hx_request_returns_only_the_results(
        self, client_for: Callable[[User], Client], buyer: User, priced: SupplierQuotation
    ) -> None:
        body = (
            client_for(buyer)
            .get(reverse("procurement:quotation_list"), headers=HX)
            .content.decode()
        )
        assert "<html" not in body.lower()
        assert "GROC-01" in body

    def test_the_detail_shows_the_derived_base_unit_price(
        self, client_for: Callable[[User], Client], buyer: User, priced: SupplierQuotation
    ) -> None:
        body = (
            client_for(buyer)
            .get(reverse("procurement:quotation_detail", args=[priced.pk]))
            .content.decode()
        )
        assert "168000.000" in body
        assert "1400.000000" in body

    def test_a_caller_without_cost_sees_no_amounts(
        self,
        client_for: Callable[[User], Client],
        accounting_manager: User,
        priced: SupplierQuotation,
        organization: Organization,
    ) -> None:
        """
        The accounting manager holds `view_supplier_cost`, so this asserts the
        opposite case through a role that does not: a viewer.
        """
        viewer = User.objects.create_user(username="viewer", password="pw-not-real-1234")
        from apps.organizations.services import grant_organization_access

        grant_organization_access(user=viewer, organization=organization, role=Role.VIEWER)
        response = client_for(User.objects.get(pk=viewer.pk)).get(
            reverse("procurement:quotation_list")
        )
        assert response.status_code in {302, 403}

    def test_submitting_through_the_screen_draws_a_number(
        self, client_for: Callable[[User], Client], buyer: User, priced: SupplierQuotation
    ) -> None:
        response = client_for(buyer).post(reverse("procurement:quotation_submit", args=[priced.pk]))
        assert response.status_code == 302
        priced.refresh_from_db()
        assert priced.number.startswith("QT-2026-")

    def test_line_delete_is_post_only(
        self, client_for: Callable[[User], Client], buyer: User, priced: SupplierQuotation
    ) -> None:
        line = priced.lines.get()
        url = reverse("procurement:quotation_line_delete", args=[priced.pk, line.pk])
        client = client_for(buyer)
        assert client.get(url).status_code == 405
        assert client.post(url).status_code == 302
        assert priced.lines.count() == 0

    def test_a_line_from_another_quotation_is_a_404_on_this_route(
        self,
        client_for: Callable[[User], Client],
        buyer: User,
        grocery: object,
        priced: SupplierQuotation,
        rice: InventoryItem,
    ) -> None:
        other = create_supplier_quotation(
            supplier=grocery,  # type: ignore[arg-type]
            recorded_by=buyer,
            quoted_at=QUOTED,
            supplier_reference="Q-OTHER",
        )
        stray = add_quotation_line(
            quotation=other,
            item=rice,
            quantity=Decimal("1.000"),
            unit_price=Decimal("5.000000"),
        )
        response = client_for(buyer).post(
            reverse("procurement:quotation_line_delete", args=[priced.pk, stray.pk])
        )
        assert response.status_code == 404
        assert SupplierQuotationLine.objects.filter(pk=stray.pk).exists()
