"""
The supplier master: its code, its scope, and the balance it does not keep.

The sharpest tests here are the absence ones. A supplier carries no balance
field and no account foreign key, and both would be easy to add later by
somebody who did not know why they were left out — so their absence is
asserted rather than assumed.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from apps.core.models import AuditEvent
from apps.organizations.authorization import OutOfScope
from apps.organizations.models import Organization, Role
from apps.organizations.services import grant_organization_access
from apps.procurement.models import Supplier, SupplierCreditTerm
from apps.procurement.permissions import (
    ALL_PERMISSIONS,
    MANAGE_SUPPLIERS,
    PERMISSION_SCOPE,
    ROLE_PERMISSIONS,
    VIEW_SUPPLIER,
    VIEW_SUPPLIER_COST,
    permissions_for_role,
)
from apps.procurement.selectors import resolve_supplier, visible_suppliers
from apps.procurement.services import create_supplier, update_supplier
from apps.users.models import User

pytestmark = pytest.mark.django_db

HX = {"hx-request": "true"}


@pytest.fixture
def meat(organization: Organization) -> Supplier:
    return create_supplier(
        organization=organization,
        code="meat-01",
        name_ar="مورد اللحوم",
        name_en="Meat Supplier",
        contact_name="أبو علي",
        phone="07701234567",
        payment_terms_days=30,
    )


# ---------------------------------------------------------------------------
# The code
# ---------------------------------------------------------------------------


class TestSupplierCode:
    def test_the_code_is_canonicalised(self, meat: Supplier) -> None:
        assert meat.code == "MEAT-01"

    def test_case_and_padding_cannot_smuggle_a_duplicate(
        self, organization: Organization, meat: Supplier
    ) -> None:
        with pytest.raises(ValidationError):
            create_supplier(organization=organization, code="  meat-01 ", name_ar="آخر")

    def test_the_database_refuses_a_duplicate_too(
        self, organization: Organization, meat: Supplier
    ) -> None:
        """The service check is a courtesy; the constraint is the guarantee."""
        with pytest.raises(IntegrityError), transaction.atomic():
            Supplier.objects.create(organization=organization, code="MEAT-01", name_ar="تكرار")

    def test_the_same_code_is_allowed_in_another_organization(
        self, other_organization: Organization, meat: Supplier
    ) -> None:
        twin = create_supplier(organization=other_organization, code="MEAT-01", name_ar="مورد آخر")
        assert twin.pk != meat.pk

    def test_an_archived_code_stays_reserved(
        self, organization: Organization, meat: Supplier
    ) -> None:
        update_supplier(supplier=meat, name_ar=meat.name_ar, is_active=False)
        with pytest.raises(ValidationError):
            create_supplier(organization=organization, code="MEAT-01", name_ar="بديل")

    def test_a_malformed_code_is_refused(self, organization: Organization) -> None:
        with pytest.raises(ValidationError):
            create_supplier(organization=organization, code="مورد ١", name_ar="خطأ")

    def test_an_empty_code_is_refused(self, organization: Organization) -> None:
        with pytest.raises(ValidationError):
            create_supplier(organization=organization, code="   ", name_ar="خطأ")


# ---------------------------------------------------------------------------
# What a supplier deliberately does not carry
# ---------------------------------------------------------------------------


class TestTheAbsentFields:
    def test_there_is_no_balance_field(self) -> None:
        """
        What is owed is derived from posted documents, every time.

        A cached balance is a second source of truth, and the cached one is
        always the one that drifts. Asserted rather than assumed, because
        adding the field later would look like an optimisation.
        """
        names = {field.name for field in Supplier._meta.get_fields()}
        assert not {"balance", "current_balance", "outstanding", "owed"} & names

    def test_there_is_no_account_field(self) -> None:
        """Account resolution is an `AccountRole` mapping and nothing else."""
        names = {field.name for field in Supplier._meta.get_fields()}
        assert not {"account", "payable_account", "account_id"} & names

    def test_a_new_supplier_owes_nothing_because_nothing_was_posted(self, meat: Supplier) -> None:
        assert not hasattr(meat, "balance")


# ---------------------------------------------------------------------------
# Ownership, archiving and audit
# ---------------------------------------------------------------------------


class TestOwnershipAndLifecycle:
    def test_a_supplier_belongs_to_one_organization(
        self, organization: Organization, meat: Supplier
    ) -> None:
        assert meat.organization_id == organization.pk

    def test_the_phone_is_canonicalised(self, meat: Supplier) -> None:
        assert meat.phone == "+9647701234567"

    def test_a_malformed_phone_is_refused(self, organization: Organization) -> None:
        with pytest.raises(ValidationError):
            create_supplier(organization=organization, code="BAD", name_ar="خطأ", phone="12345")

    def test_a_negative_credit_limit_is_refused_by_the_database(
        self, organization: Organization
    ) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Supplier.objects.create(
                organization=organization,
                code="NEG",
                name_ar="سالب",
                credit_limit=Decimal("-1.000"),
            )

    def test_archive_and_reactivate_keep_the_row(self, meat: Supplier) -> None:
        update_supplier(supplier=meat, name_ar=meat.name_ar, is_active=False)
        assert Supplier.objects.get(pk=meat.pk).is_active is False

        update_supplier(supplier=meat, name_ar=meat.name_ar, is_active=True)
        assert Supplier.objects.get(pk=meat.pk).is_active is True

    def test_creating_and_editing_are_audited_with_a_real_before(self, meat: Supplier) -> None:
        update_supplier(supplier=meat, name_ar="مورد اللحوم المحدث")
        events = AuditEvent.objects.filter(
            target_type="procurement.Supplier", target_id=str(meat.pk)
        ).order_by("id")
        assert [event.action for event in events] == ["CREATED", "UPDATED"]

        edit = events[1]
        assert edit.previous_state is not None and edit.new_state is not None
        assert edit.previous_state["name_ar"] == "مورد اللحوم"
        assert edit.new_state["name_ar"] == "مورد اللحوم المحدث"
        assert edit.previous_state["payment_terms_days"] == 30
        assert edit.new_state["payment_terms_days"] == 30

    def test_payment_terms_are_a_projection_not_an_editable_supplier_field(
        self, meat: Supplier
    ) -> None:
        assert meat.payment_terms_days == 30
        with pytest.raises(ValidationError) as refusal:
            update_supplier(supplier=meat, name_ar=meat.name_ar, payment_terms_days=45)
        assert refusal.value.code == "supplier_credit_terms_are_versioned"


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestScope:
    def test_another_organizations_supplier_is_invisible(
        self, other_organization: Organization, manager: User, meat: Supplier
    ) -> None:
        theirs = create_supplier(organization=other_organization, code="RIVAL-01", name_ar="منافس")
        codes = set(visible_suppliers(manager).values_list("code", flat=True))
        assert "MEAT-01" in codes
        assert theirs.code not in codes

    def test_a_foreign_supplier_id_is_out_of_scope(
        self, other_organization: Organization, manager: User
    ) -> None:
        theirs = create_supplier(organization=other_organization, code="RIVAL-02", name_ar="منافس")
        with pytest.raises(OutOfScope):
            resolve_supplier(manager, theirs.pk)

    def test_an_archived_supplier_stays_visible_to_management(
        self, manager: User, meat: Supplier
    ) -> None:
        update_supplier(supplier=meat, name_ar=meat.name_ar, is_active=False)
        assert meat.pk in set(visible_suppliers(manager).values_list("pk", flat=True))

    def test_a_branch_membership_reaches_the_organizations_suppliers(
        self, keeper: User, meat: Supplier
    ) -> None:
        """A storekeeper holds no organization membership and still sees them."""
        assert meat.pk in set(visible_suppliers(keeper).values_list("pk", flat=True))


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_every_permission_is_migrated(self) -> None:
        from django.contrib.auth.models import Permission

        codenames = set(
            Permission.objects.filter(content_type__app_label="procurement").values_list(
                "codename", flat=True
            )
        )
        for permission in ALL_PERMISSIONS:
            assert permission.split(".", 1)[1] in codenames, permission

    def test_every_permission_declares_a_scope(self) -> None:
        assert set(PERMISSION_SCOPE) == set(ALL_PERMISSIONS)

    def test_every_role_is_mapped(self) -> None:
        assert set(ROLE_PERMISSIONS) == {role.value for role in Role}

    def test_a_storekeeper_sees_suppliers_and_never_their_prices(self) -> None:
        held = permissions_for_role(Role.STOREKEEPER)
        assert VIEW_SUPPLIER in held
        assert VIEW_SUPPLIER_COST not in held
        assert MANAGE_SUPPLIERS not in held

    def test_purchasing_maintains_the_master_and_sees_cost(self) -> None:
        held = permissions_for_role(Role.PURCHASING)
        assert {VIEW_SUPPLIER, MANAGE_SUPPLIERS, VIEW_SUPPLIER_COST} <= held

    def test_an_accounting_manager_reads_but_does_not_invent_a_supplier(self) -> None:
        held = permissions_for_role(Role.ACCOUNTING_MANAGER)
        assert VIEW_SUPPLIER_COST in held
        assert MANAGE_SUPPLIERS not in held

    def test_a_cashier_holds_nothing(self) -> None:
        assert permissions_for_role(Role.CASHIER) == frozenset()

    def test_the_groups_carry_the_permissions_after_a_grant(
        self, organization: Organization
    ) -> None:
        user = User.objects.create_user(username="buyer", password="pw-not-real-1234")
        grant_organization_access(user=user, organization=organization, role=Role.PURCHASING)
        user = User.objects.get(pk=user.pk)
        assert user.has_perm(MANAGE_SUPPLIERS)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class TestScreens:
    def test_the_list_renders_the_supplier(
        self, client_for: Callable[[User], Client], manager: User, meat: Supplier
    ) -> None:
        response = client_for(manager).get(reverse("procurement:supplier_list"))
        assert response.status_code == 200
        body = response.content.decode()
        assert "MEAT-01" in body
        assert "مورد اللحوم" in body

    def test_an_hx_request_returns_only_the_results(
        self, client_for: Callable[[User], Client], manager: User, meat: Supplier
    ) -> None:
        response = client_for(manager).get(reverse("procurement:supplier_list"), headers=HX)
        assert response.status_code == 200
        body = response.content.decode()
        assert "MEAT-01" in body
        assert "<html" not in body.lower()

    def test_the_search_narrows_the_table(
        self, client_for: Callable[[User], Client], manager: User, meat: Supplier
    ) -> None:
        client = client_for(manager)
        create_supplier(organization=meat.organization, code="VEG-01", name_ar="مورد الخضار")
        body = client.get(
            reverse("procurement:supplier_list"), {"q": "MEAT"}, headers=HX
        ).content.decode()
        assert "MEAT-01" in body
        assert "VEG-01" not in body

    def test_a_storekeeper_may_read_the_list_and_is_offered_no_create(
        self, client_for: Callable[[User], Client], keeper: User, meat: Supplier
    ) -> None:
        response = client_for(keeper).get(reverse("procurement:supplier_list"))
        assert response.status_code == 200
        assert response.context["create_url"] is None

    def test_a_cashier_is_refused_the_list(
        self, client_for: Callable[[User], Client], cashier: User
    ) -> None:
        response = client_for(cashier).get(reverse("procurement:supplier_list"))
        assert response.status_code in {302, 403}

    def test_creating_through_the_screen_goes_through_the_service(
        self, client_for: Callable[[User], Client], manager: User, organization: Organization
    ) -> None:
        response = client_for(manager).post(
            reverse("procurement:supplier_create"),
            {
                "organization": str(organization.pk),
                "code": " chicken-01 ",
                "name_ar": "مورد الدجاج",
                "payment_terms_days": "14",
            },
        )
        assert response.status_code == 302
        supplier = Supplier.objects.get(organization=organization, code="CHICKEN-01")
        assert supplier.payment_terms_days == 14
        assert AuditEvent.objects.filter(
            target_type="procurement.Supplier", target_id=str(supplier.pk), action="CREATED"
        ).exists()

    def test_a_foreign_supplier_is_a_404_on_the_edit_route(
        self,
        client_for: Callable[[User], Client],
        manager: User,
        other_organization: Organization,
    ) -> None:
        theirs = create_supplier(organization=other_organization, code="RIVAL-03", name_ar="منافس")
        response = client_for(manager).get(reverse("procurement:supplier_update", args=[theirs.pk]))
        assert response.status_code == 404

    def test_archiving_is_post_only(
        self, client_for: Callable[[User], Client], manager: User, meat: Supplier
    ) -> None:
        client = client_for(manager)
        assert (
            client.get(reverse("procurement:supplier_archive", args=[meat.pk])).status_code == 405
        )
        assert (
            client.post(reverse("procurement:supplier_archive", args=[meat.pk])).status_code == 302
        )
        assert Supplier.objects.get(pk=meat.pk).is_active is False


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class TestAdminIsReadOnly:
    def test_the_supplier_is_registered_read_only(self) -> None:
        from django.contrib import admin

        from apps.procurement.admin import SupplierAdmin

        registered = admin.site._registry[Supplier]
        assert isinstance(registered, SupplierAdmin)
        assert not registered.has_add_permission(None)  # type: ignore[arg-type]
        assert not registered.has_change_permission(None)  # type: ignore[arg-type]
        assert not registered.has_delete_permission(None)  # type: ignore[arg-type]

    def test_credit_terms_are_registered_read_only(self) -> None:
        from django.contrib import admin

        from apps.procurement.admin import SupplierCreditTermAdmin

        registered = admin.site._registry[SupplierCreditTerm]
        assert isinstance(registered, SupplierCreditTermAdmin)
        assert not registered.has_add_permission(None)  # type: ignore[arg-type]
        assert not registered.has_change_permission(None)  # type: ignore[arg-type]
        assert not registered.has_delete_permission(None)  # type: ignore[arg-type]

    def test_a_superuser_cannot_edit_a_supplier_through_the_admin(
        self, client_for: Callable[[User], Client], superuser: User, meat: Supplier
    ) -> None:
        response = client_for(superuser).post(
            f"/admin/procurement/supplier/{meat.pk}/change/",
            {"name_ar": "مغيَّر"},
        )
        assert response.status_code in {302, 403}
        meat.refresh_from_db()
        assert meat.name_ar == "مورد اللحوم"


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------


class TestDemoSuppliers:
    def test_the_seed_creates_exactly_three_and_is_idempotent(
        self, organization: Organization
    ) -> None:
        from apps.procurement.demo import DEMO_SUPPLIERS, seed_demo_suppliers

        first = seed_demo_suppliers(organization=organization)
        assert len(first) == len(DEMO_SUPPLIERS) == 3

        again = seed_demo_suppliers(organization=organization)
        assert {supplier.pk for supplier in again} == {supplier.pk for supplier in first}
        assert Supplier.objects.filter(organization=organization).count() == 3
        terms = SupplierCreditTerm.objects.filter(organization=organization)
        assert terms.count() == 3
        assert set(terms.values_list("net_days", flat=True)) == {0, 14, 30}

    def test_every_demo_supplier_is_bilingual_and_named_in_arabic(
        self, organization: Organization
    ) -> None:
        from apps.procurement.demo import seed_demo_suppliers

        for supplier in seed_demo_suppliers(organization=organization):
            assert supplier.name_ar
            assert supplier.code.startswith("DEMO-")
