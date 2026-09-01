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
    "users:user_list",
    "units:unit_list",
    "core:audit_list",
]


@pytest.fixture
def staff() -> User:
    return User.objects.create_superuser(username="admin.user", password=PASSWORD)


@pytest.fixture
def plain_user() -> User:
    return User.objects.create_user(username="cashier", password=PASSWORD)


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name="خان مندي")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK",
        name="البنوك",
        business_day_start_time=time(9, 0),
    )


class TestAccessControl:
    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_anonymous_is_redirected_to_login(self, client: Client, url_name: str) -> None:
        response = client.get(reverse(url_name))
        assert response.status_code == 302
        assert reverse("users:login") in response["Location"]

    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_unprivileged_user_is_refused(
        self, client: Client, plain_user: User, url_name: str
    ) -> None:
        """
        These screens create users, branches, and the units every quantity is
        measured in. A signed-in cashier must not reach them.
        """
        client.force_login(plain_user)
        assert client.get(reverse(url_name)).status_code == 403

    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_superuser_may_enter(self, client: Client, staff: User, url_name: str) -> None:
        client.force_login(staff)
        assert client.get(reverse(url_name)).status_code == 200


class TestScreensLiveInTheShell:
    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_every_screen_renders_inside_the_shell(
        self, client: Client, staff: User, url_name: str
    ) -> None:
        """Not Django admin: the same primary, secondary, and header shell."""
        client.force_login(staff)
        body = client.get(reverse(url_name)).content.decode()
        assert 'class="ui-app-shell ui-app-shell--compact"' in body
        assert 'class="ui-primary-nav"' in body
        assert 'class="ui-secondary-nav"' in body
        assert 'class="ui-app-header"' in body

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

    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_navigation_returns_one_page_fragment_without_a_nested_shell(
        self, client: Client, staff: User, url_name: str
    ) -> None:
        """An HTMX navigation swap must never put a second header in the page."""
        client.force_login(staff)
        response = client.get(
            reverse(url_name),
            headers={"HX-Request": "true", "HX-Target": "main-content"},
        )
        body = response.content.decode()

        assert response.status_code == 200
        assert 'class="ui-page ui-page--list"' in body
        assert 'class="ui-app-shell"' not in body
        assert "<html" not in body
        assert 'hx-swap-oob="outerHTML:' in body

    @pytest.mark.parametrize("url_name", FOUNDATION_LIST_URLS)
    def test_live_filter_returns_only_the_results_region(
        self, client: Client, staff: User, url_name: str
    ) -> None:
        """Search and pagination update the table, not the whole settings page."""
        client.force_login(staff)
        response = client.get(
            reverse(url_name),
            {"q": "not-present"},
            headers={"HX-Request": "true", "HX-Target": "list-results"},
        )
        body = response.content.decode()

        assert response.status_code == 200
        assert 'id="list-results"' in body
        assert 'class="ui-page-header"' not in body
        assert 'class="ui-app-shell"' not in body
        assert "<html" not in body


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
            {"code": "NEWORG", "name": "جديدة"},
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
            {"code": "AUDITED", "name": "مدققة"},
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
            {"code": "bad code", "name": "س"},
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
                "name": "بلا",
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
                "name": branch.name,
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
            name="أونصة",
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
            name="أونصة",
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
                "name": "ملعقة",
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
            name="كغم",
            dimension=Dimension.MASS,
            factor_to_base=Decimal("1"),
            is_base=True,
        )
        response = client.get(reverse("units:unit_update", args=[base.pk]))
        assert response.context["form"].fields["factor_to_base"].disabled


class TestUserScreens:
    def test_create_account(self, client: Client, staff: User) -> None:
        organization = create_organization(code="USERORG", name="مستخدم")
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
                "organization": organization.pk,
            },
        )
        assert response.status_code == 302
        created = User.objects.get(username="newstore")
        assert created.phone == "+9647701234567"

    def test_mismatched_passwords_are_refused(self, client: Client, staff: User) -> None:
        organization = create_organization(code="MISMATCH", name="تجربة")
        client.force_login(staff)
        response = client.post(
            reverse("users:user_create"),
            {
                "username": "mismatch",
                "password1": PASSWORD,
                "password2": "different",
                "organization": organization.pk,
            },
        )
        assert response.status_code == 200
        assert "password2" in response.context["form"].errors
        assert not User.objects.filter(username="mismatch").exists()

    def test_the_password_is_never_in_the_audit_snapshot(self, client: Client, staff: User) -> None:
        organization = create_organization(code="HASHORG", name="تشفير")
        client.force_login(staff)
        client.post(
            reverse("users:user_create"),
            {
                "username": "hashed",
                "password1": PASSWORD,
                "password2": PASSWORD,
                "organization": organization.pk,
            },
        )
        created = User.objects.get(username="hashed")
        event = AuditEvent.objects.get(target_type="users.User", target_id=str(created.pk))
        assert event.new_state is not None
        assert "password" not in event.new_state


class TestAccessScreen:
    """
    صلاحيات الموظف — the manager assigns posts on the employee's own file.

    This replaced a request-and-approve screen at `settings/access/`. The
    coverage is deliberately the same two acts, because they are what the old
    screen existed to do; only the route and the number of people required
    have changed.
    """

    def test_granting_access(self, client: Client, staff: User, branch: Branch) -> None:
        client.force_login(staff)
        target = User.objects.create_user(username="grantee", password=PASSWORD)
        grant_branch_access(user=target, branch=branch, role=Role.VIEWER)

        client.post(
            reverse("users:user_access", args=[target.pk]),
            {"scope": f"branch:{branch.pk}", "role": Role.STOREKEEPER},
        )

        assert BranchMembership.objects.filter(
            user=target, branch=branch, role=Role.STOREKEEPER, is_active=True
        ).exists()

    def test_revoking_keeps_the_row(self, client: Client, staff: User, branch: Branch) -> None:
        """Deactivated, never deleted: the audit trail names a row that stays."""
        client.force_login(staff)
        target = User.objects.create_user(username="revokee", password=PASSWORD)
        membership = grant_branch_access(user=target, branch=branch, role=Role.CASHIER)

        client.post(
            reverse("users:user_access", args=[target.pk]),
            {"revoke": f"branch:{branch.pk}"},
        )

        membership.refresh_from_db()
        assert membership.is_active is False
        assert AuditEvent.objects.filter(action=AuditAction.ACCESS_REVOKED).exists()

    def test_the_owner_post_is_not_offered_and_not_accepted(
        self, client: Client, staff: User, branch: Branch
    ) -> None:
        """
        The one post this screen never hands out.

        A manager holds `manage_roles` and `manage_access` together, so
        without this they could promote somebody to the only authority able to
        remove them. It is absent from the form's choices *and* refused by the
        service, because a choice list is a courtesy and the service is the
        gate.
        """
        target = User.objects.create_user(username="would-be-owner", password=PASSWORD)
        grant_branch_access(user=target, branch=branch, role=Role.VIEWER)
        client.force_login(staff)

        client.post(
            reverse("users:user_access", args=[target.pk]),
            {"scope": f"branch:{branch.pk}", "role": Role.OWNER},
        )

        assert not BranchMembership.objects.filter(
            user=target, branch=branch, role=Role.OWNER, is_active=True
        ).exists()


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
        assert 'class="ui-table__actions"' not in body
        assert 'class="ui-button ui-button--primary"' not in body

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
                "name": "اسم جديد",
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
        assert event.previous_state["name"] == "البنوك"
        assert event.new_state["name"] == "اسم جديد"

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

    def test_phase_one_is_shown_as_ready_once_phase_zero_closes(
        self, client: Client, staff: User
    ) -> None:
        """
        Was "locked" until the Task 0.8 exit gate passed. Ready is a different
        claim from not-started: it says the phase before it is finished.
        """
        client.force_login(staff)
        body = client.get(reverse("users:home")).content.decode()
        assert "جاهز للبدء" in body
        assert "مقفل" not in body
