"""
The native Phase 0 settings screens.

They must live inside the shell, be closed to non-staff, and route every
mutation through the services so nothing is saved without an audit event.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.core.models import AuditAction, AuditEvent
from apps.organizations.models import Branch, BranchMembership, Organization, Role
from apps.organizations.services import create_branch, create_organization, grant_branch_access
from apps.units.models import Dimension, UnitOfMeasure
from apps.units.services import create_unit
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"

FOUNDATION_LIST_URLS = [
    "organizations:organization_list",
    "organizations:branch_list",
    "organizations:access_list",
    "users:user_list",
    "units:unit_list",
    "core:audit_list",
]


@pytest.fixture
def staff() -> User:
    return User.objects.create_user(username="admin.user", password=PASSWORD, is_staff=True)


@pytest.fixture
def plain_user() -> User:
    return User.objects.create_user(username="cashier", password=PASSWORD)


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9, 0),
    )


class TestAccessControl:
    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_anonymous_is_redirected_to_login(self, client: Client, url_name: str) -> None:
        response = client.get(reverse(url_name))
        assert response.status_code == 302
        assert reverse("users:login") in response["Location"]

    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_non_staff_is_refused(self, client: Client, plain_user: User, url_name: str) -> None:
        """
        These screens create users, branches, and the units every quantity is
        measured in. A signed-in cashier must not reach them.
        """
        client.force_login(plain_user)
        assert client.get(reverse(url_name)).status_code == 403

    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_staff_may_enter(self, client: Client, staff: User, url_name: str) -> None:
        client.force_login(staff)
        assert client.get(reverse(url_name)).status_code == 200


class TestScreensLiveInTheShell:
    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_every_screen_renders_inside_the_shell(
        self, client: Client, staff: User, url_name: str
    ) -> None:
        """Not Django admin: the same rail, sidebar, and top bar as the rest."""
        client.force_login(staff)
        body = client.get(reverse(url_name)).content.decode()
        assert 'class="shell"' in body
        assert 'class="rail"' in body
        assert 'class="topbar"' in body

    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_every_screen_is_right_to_left_in_arabic(
        self, client: Client, staff: User, url_name: str
    ) -> None:
        # Test settings default to English for deterministic assertions, so
        # Arabic is selected explicitly here as a user would.
        client.force_login(staff)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
        assert 'dir="rtl"' in client.get(reverse(url_name)).content.decode()

    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_settings_module_is_highlighted(
        self, client: Client, staff: User, url_name: str
    ) -> None:
        client.force_login(staff)
        response = client.get(reverse(url_name))
        assert response.context["active_module"].key == "settings"


class TestOrganizationScreens:
    def test_list_shows_organizations(
        self, client: Client, staff: User, organization: Organization
    ) -> None:
        client.force_login(staff)
        assert "خان مندي" in client.get(reverse("organizations:organization_list")).content.decode()

    def test_create_goes_through_the_service_and_audits(self, client: Client, staff: User) -> None:
        client.force_login(staff)
        response = client.post(
            reverse("organizations:organization_create"),
            {"code": "NEWORG", "name_ar": "جديدة", "name_en": "New Org"},
        )
        assert response.status_code == 302
        created = Organization.objects.get(code="NEWORG")
        assert AuditEvent.objects.filter(
            target_type="organizations.Organization",
            target_id=str(created.pk),
            action=AuditAction.CREATED,
        ).exists()

    def test_the_audit_event_names_the_acting_user(self, client: Client, staff: User) -> None:
        client.force_login(staff)
        client.post(
            reverse("organizations:organization_create"),
            {"code": "AUDITED", "name_ar": "مدققة", "name_en": "Audited"},
        )
        event = AuditEvent.objects.get(target_id=str(Organization.objects.get(code="AUDITED").pk))
        assert event.actor == staff
        assert event.actor_label == str(staff)

    def test_code_cannot_be_edited(
        self, client: Client, staff: User, organization: Organization
    ) -> None:
        """It appears in document numbering; editing it rewrites history."""
        client.force_login(staff)
        response = client.get(reverse("organizations:organization_update", args=[organization.pk]))
        assert "code" not in response.context["form"].fields

    def test_invalid_code_is_shown_as_a_field_error(self, client: Client, staff: User) -> None:
        client.force_login(staff)
        response = client.post(
            reverse("organizations:organization_create"),
            {"code": "bad code", "name_ar": "س", "name_en": "S"},
        )
        assert response.status_code == 200
        assert response.context["form"].errors


class TestBranchScreens:
    def test_list_shows_the_operating_day(
        self, client: Client, staff: User, branch: Branch
    ) -> None:
        client.force_login(staff)
        body = client.get(reverse("organizations:branch_list")).content.decode()
        assert "البنوك" in body
        assert "Asia/Baghdad" in body

    def test_create_requires_the_operating_day_cutoff(
        self, client: Client, staff: User, organization: Organization
    ) -> None:
        """No default: the cutoff is a business decision, not a form default."""
        client.force_login(staff)
        response = client.post(
            reverse("organizations:branch_create"),
            {
                "organization": organization.pk,
                "code": "NOCUT",
                "name_ar": "بلا",
                "name_en": "No cutoff",
                "timezone": "Asia/Baghdad",
            },
        )
        assert response.status_code == 200
        assert "business_day_start_time" in response.context["form"].errors

    def test_changing_the_cutoff_is_recorded_with_a_reason(
        self, client: Client, staff: User, branch: Branch
    ) -> None:
        client.force_login(staff)
        client.post(
            reverse("organizations:branch_update", args=[branch.pk]),
            {
                "name_ar": branch.name_ar,
                "name_en": branch.name_en,
                "timezone": "Asia/Baghdad",
                "business_day_start_time": "10:00",
                "is_active": "on",
            },
        )
        event = AuditEvent.objects.filter(
            target_type="organizations.Branch", action=AuditAction.UPDATED
        ).latest("occurred_at")
        assert event.reason == "operating day changed"
        assert event.previous_state is not None
        assert event.previous_state["business_day_start_time"].startswith("09:00")


class TestUnitScreens:
    def test_list_shows_full_factor_precision(self, client: Client, staff: User) -> None:
        """A truncated factor on screen invites someone to retype it wrong."""
        client.force_login(staff)
        create_unit(
            code="OUNCE",
            name_ar="أونصة",
            name_en="Ounce",
            dimension=Dimension.MASS,
            factor_to_base=Decimal("0.028349523125"),
        )
        body = client.get(reverse("units:unit_list")).content.decode()
        assert "0.028349523125" in body

    def test_the_factor_uses_a_period_not_a_locale_separator(
        self, client: Client, staff: User
    ) -> None:
        """
        Django localises Decimals, so under Arabic the factor would render as
        `0,028349523125`. A factor is a technical identity; a comma there is
        ambiguous and invites a mis-typed re-entry.
        """
        client.force_login(staff)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
        create_unit(
            code="OUNCE2",
            name_ar="أونصة",
            name_en="Ounce",
            dimension=Dimension.MASS,
            factor_to_base=Decimal("0.028349523125"),
        )
        body = client.get(reverse("units:unit_list")).content.decode()
        assert "0.028349523125" in body
        assert "0,028349523125" not in body

    def test_create_audits(self, client: Client, staff: User) -> None:
        client.force_login(staff)
        client.post(
            reverse("units:unit_create"),
            {
                "code": "SPOON",
                "name_ar": "ملعقة",
                "name_en": "Spoon",
                "dimension": Dimension.VOLUME,
                "factor_to_base": "0.015",
            },
        )
        unit = UnitOfMeasure.objects.get(code="SPOON")
        assert AuditEvent.objects.filter(
            target_type="units.UnitOfMeasure", target_id=str(unit.pk)
        ).exists()

    def test_a_base_unit_factor_cannot_be_edited(self, client: Client, staff: User) -> None:
        """The database pins it to 1; offering the field would only error."""
        client.force_login(staff)
        base = create_unit(
            code="BASEKG",
            name_ar="كغم",
            name_en="Kilogram",
            dimension=Dimension.MASS,
            factor_to_base=Decimal("1"),
            is_base=True,
        )
        response = client.get(reverse("units:unit_update", args=[base.pk]))
        assert response.context["form"].fields["factor_to_base"].disabled


class TestUserScreens:
    def test_create_account(self, client: Client, staff: User) -> None:
        client.force_login(staff)
        response = client.post(
            reverse("users:user_create"),
            {
                "username": "newstore",
                "phone": "07701234567",
                "first_name": "أمين",
                "last_name": "المخزن",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        assert response.status_code == 302
        created = User.objects.get(username="newstore")
        assert created.phone == "+9647701234567"

    def test_mismatched_passwords_are_refused(self, client: Client, staff: User) -> None:
        client.force_login(staff)
        response = client.post(
            reverse("users:user_create"),
            {"username": "mismatch", "password1": PASSWORD, "password2": "different"},
        )
        assert response.status_code == 200
        assert "password2" in response.context["form"].errors
        assert not User.objects.filter(username="mismatch").exists()

    def test_the_password_is_never_in_the_audit_snapshot(self, client: Client, staff: User) -> None:
        client.force_login(staff)
        client.post(
            reverse("users:user_create"),
            {"username": "hashed", "password1": PASSWORD, "password2": PASSWORD},
        )
        created = User.objects.get(username="hashed")
        event = AuditEvent.objects.get(target_type="users.User", target_id=str(created.pk))
        assert event.new_state is not None
        assert "password" not in event.new_state


class TestAccessScreen:
    def test_granting_access(self, client: Client, staff: User, branch: Branch) -> None:
        client.force_login(staff)
        target = User.objects.create_user(username="grantee", password=PASSWORD)
        client.post(
            reverse("organizations:access_list"),
            {"user": target.pk, "branch": branch.pk, "role": Role.STOREKEEPER},
        )
        assert BranchMembership.objects.filter(user=target, branch=branch, is_active=True).exists()

    def test_revoking_keeps_the_row(self, client: Client, staff: User, branch: Branch) -> None:
        client.force_login(staff)
        target = User.objects.create_user(username="revokee", password=PASSWORD)
        membership = grant_branch_access(user=target, branch=branch, role=Role.CASHIER)

        client.post(reverse("organizations:access_list"), {"revoke": membership.pk})

        membership.refresh_from_db()
        assert membership.is_active is False
        assert AuditEvent.objects.filter(action=AuditAction.ACCESS_REVOKED).exists()


class TestAuditScreen:
    def test_shows_events(self, client: Client, staff: User, branch: Branch) -> None:
        client.force_login(staff)
        body = client.get(reverse("core:audit_list")).content.decode()
        assert "organizations.Branch" in body

    def test_offers_no_way_to_edit_or_delete(
        self, client: Client, staff: User, branch: Branch
    ) -> None:
        """The trigger refuses both, so offering the action would only fail."""
        client.force_login(staff)
        body = client.get(reverse("core:audit_list")).content.decode()
        # No actions column at all, and no create button.
        assert 'class="table__actions"' not in body
        assert 'class="btn btn--primary"' not in body

    def test_previous_and_new_state_actually_differ_on_an_edit(
        self, client: Client, staff: User, branch: Branch
    ) -> None:
        """
        A ModelForm mutates its instance during validation, so a careless
        service would snapshot the new values as "previous" and the trail
        would claim nothing changed.
        """
        client.force_login(staff)
        client.post(
            reverse("organizations:branch_update", args=[branch.pk]),
            {
                "name_ar": "اسم جديد",
                "name_en": branch.name_en,
                "timezone": "Asia/Baghdad",
                "business_day_start_time": "09:00",
                "is_active": "on",
            },
        )
        event = AuditEvent.objects.filter(
            target_type="organizations.Branch", action=AuditAction.UPDATED
        ).latest("occurred_at")
        assert event.previous_state is not None
        assert event.new_state is not None
        assert event.previous_state["name_ar"] == "البنوك"
        assert event.new_state["name_ar"] == "اسم جديد"

    def test_can_be_filtered_by_action(self, client: Client, staff: User, branch: Branch) -> None:
        client.force_login(staff)
        response = client.get(reverse("core:audit_list"), {"action": AuditAction.CREATED})
        assert response.status_code == 200
        assert all(event.action == AuditAction.CREATED for event in response.context["events"])


class TestBuildStatusCard:
    def test_dashboard_shows_the_build_status(self, client: Client, staff: User) -> None:
        client.force_login(staff)
        body = client.get(reverse("users:home")).content.decode()
        assert "حالة البناء" in body
        assert "نواة المحاسبة" in body

    def test_phase_one_is_shown_as_locked(self, client: Client, staff: User) -> None:
        client.force_login(staff)
        body = client.get(reverse("users:home")).content.decode()
        assert "مقفل" in body
