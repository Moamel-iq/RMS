"""Regression tests for organization-scoped security administration."""

from __future__ import annotations

from datetime import time

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from apps.core.models import AuditAction, AuditEvent
from apps.organizations.authorization import OutOfScope
from apps.organizations.models import (
    Branch,
    Organization,
    OrganizationMembership,
    Role,
)
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    grant_organization_access,
)
from apps.users.forms import UserAccountCreateForm, UserAccountUpdateForm
from apps.users.models import User
from apps.users.services import create_user_account

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"


def _branch(organization: Organization, code: str) -> Branch:
    return create_branch(
        organization=organization,
        code=code,
        name=code,
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name="خان مندي")


@pytest.fixture
def rival() -> Organization:
    return create_organization(code="RIVAL", name="منافس")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return _branch(organization, "BUNOOK")


@pytest.fixture
def rival_branch(rival: Organization) -> Branch:
    return _branch(rival, "RIVALBR")


@pytest.fixture
def owner(organization: Organization) -> User:
    user = User.objects.create_user(username="owner", password=PASSWORD)
    # Bootstrap/data setup is trusted; browser paths always supply an actor.
    grant_organization_access(user=user, organization=organization, role=Role.OWNER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def reviewer(organization: Organization) -> User:
    user = User.objects.create_user(username="reviewer", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.OWNER)
    return User.objects.get(pk=user.pk)


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_is_staff_alone_cannot_open_security_settings() -> None:
    staff_only = User.objects.create_user(username="staff-only", password=PASSWORD, is_staff=True)
    response = _client(staff_only).get(reverse("organizations:organization_list"))
    assert response.status_code == 403


def test_owner_only_sees_and_edits_its_own_organization(
    owner: User, organization: Organization, rival: Organization
) -> None:
    client = _client(owner)

    response = client.get(reverse("organizations:organization_list"))
    rows = list(response.context["organizations"])
    assert response.status_code == 200
    assert rows == [organization]

    # A foreign identifier behaves as missing rather than confirming a tenant.
    assert (
        client.get(reverse("organizations:organization_update", args=[rival.pk])).status_code == 404
    )


def test_direct_access_grant_refuses_self_owner_and_privileged_targets(
    owner: User, branch: Branch
) -> None:
    target = User.objects.create_user(username="target", password=PASSWORD)
    staff_target = User.objects.create_user(
        username="staff-target", password=PASSWORD, is_staff=True
    )

    with pytest.raises(ValidationError, match="نفسه"):
        grant_branch_access(user=owner, branch=branch, role=Role.CASHIER, actor=owner)
    with pytest.raises(ValidationError, match="المالك"):
        grant_branch_access(user=target, branch=branch, role=Role.OWNER, actor=owner)
    with pytest.raises(ValidationError, match="الإدارية"):
        grant_branch_access(user=staff_target, branch=branch, role=Role.CASHIER, actor=owner)


def test_direct_access_grant_cannot_cross_organization_scope(
    owner: User, rival_branch: Branch
) -> None:
    target = User.objects.create_user(username="rival-target", password=PASSWORD)
    with pytest.raises(OutOfScope):
        grant_branch_access(user=target, branch=rival_branch, role=Role.CASHIER, actor=owner)


def test_account_forms_and_service_cannot_create_staff_users(
    owner: User, organization: Organization
) -> None:
    assert "is_staff" not in UserAccountCreateForm(actor=owner).fields
    assert "is_staff" not in UserAccountUpdateForm().fields

    with pytest.raises(ValidationError, match="إداري"):
        create_user_account(
            username="not-staff",
            password=PASSWORD,
            is_staff=True,
            organization=organization,
            actor=owner,
        )


def test_scoped_audit_screen_hides_other_organizations_events(
    owner: User, organization: Organization, branch: Branch, rival_branch: Branch
) -> None:
    response = _client(owner).get(reverse("core:audit_list"))
    branch_ids = {event.branch_id for event in response.context["events"]}

    assert response.status_code == 200
    assert branch.pk in branch_ids
    assert rival_branch.pk not in branch_ids
    assert all(event.organization_id == organization.pk for event in response.context["events"])


def test_employee_access_manager_can_assign_and_revoke_with_audit(
    organization: Organization,
) -> None:
    manager = User.objects.create_user(username="people-manager", password=PASSWORD)
    target = User.objects.create_user(username="employee-accountant", password=PASSWORD)
    grant_organization_access(user=manager, organization=organization, role=Role.MANAGER)
    grant_organization_access(user=target, organization=organization, role=Role.VIEWER)
    client = _client(manager)
    url = reverse("users:user_access", args=[target.pk])
    response = client.post(
        url, {"scope": f"org:{organization.pk}", "role": Role.ACCOUNTING_MANAGER}
    )
    assert response.status_code == 302
    membership = OrganizationMembership.objects.get(user=target, organization=organization)
    assert membership.role == Role.ACCOUNTING_MANAGER
    assert membership.is_active
    assert AuditEvent.objects.filter(
        actor=manager,
        action=AuditAction.ACCESS_GRANTED,
        target_type="organizations.OrganizationMembership",
        target_id=str(membership.pk),
    ).exists()
    assert client.post(url, {"revoke": f"org:{organization.pk}"}).status_code == 302
    membership = OrganizationMembership.objects.get(pk=membership.pk)
    assert not membership.is_active
    assert AuditEvent.objects.filter(
        actor=manager,
        action=AuditAction.ACCESS_REVOKED,
        target_type="organizations.OrganizationMembership",
        target_id=str(membership.pk),
    ).exists()


def test_employee_access_rejects_forged_scope_and_hidden_targets(
    owner: User,
    organization: Organization,
    rival: Organization,
) -> None:
    local_user = User.objects.create_user(username="local-employee", password=PASSWORD)
    foreign_user = User.objects.create_user(username="foreign-employee", password=PASSWORD)
    grant_organization_access(user=local_user, organization=organization, role=Role.VIEWER)
    grant_organization_access(user=foreign_user, organization=rival, role=Role.VIEWER)
    client = _client(owner)
    assert client.get(reverse("users:user_access", args=[foreign_user.pk])).status_code == 404
    response = client.post(
        reverse("users:user_access", args=[local_user.pk]),
        {"scope": f"org:{rival.pk}", "role": Role.ACCOUNTING_MANAGER},
    )
    assert response.status_code == 200
    assert response.context["form"].errors
    assert not OrganizationMembership.objects.filter(user=local_user, organization=rival).exists()


def test_accounting_manager_cannot_administer_employee_permissions(
    organization: Organization,
) -> None:
    accountant = User.objects.create_user(username="account-manager", password=PASSWORD)
    target = User.objects.create_user(username="cashier-target", password=PASSWORD)
    grant_organization_access(
        user=accountant, organization=organization, role=Role.ACCOUNTING_MANAGER
    )
    grant_organization_access(user=target, organization=organization, role=Role.CASHIER)
    client = _client(accountant)
    url = reverse("users:user_access", args=[target.pk])
    assert client.get(url).status_code == 403
    assert (
        client.post(url, {"scope": f"org:{organization.pk}", "role": Role.MANAGER}).status_code
        == 403
    )
    assert (
        OrganizationMembership.objects.get(user=target, organization=organization).role
        == Role.CASHIER
    )


def test_shared_employee_access_hides_memberships_in_other_organizations(
    owner: User,
    organization: Organization,
    branch: Branch,
    rival: Organization,
    rival_branch: Branch,
) -> None:
    target = User.objects.create_user(username="shared-employee", password=PASSWORD)
    local_membership = grant_organization_access(
        user=target, organization=organization, role=Role.VIEWER
    )
    grant_organization_access(user=target, organization=rival, role=Role.ACCOUNTANT)
    local_branch_membership = grant_branch_access(user=target, branch=branch, role=Role.CASHIER)
    grant_branch_access(user=target, branch=rival_branch, role=Role.STOREKEEPER)
    response = _client(owner).get(reverse("users:user_access", args=[target.pk]))
    assert response.status_code == 200
    assert list(response.context["organization_memberships"]) == [local_membership]
    assert list(response.context["branch_memberships"]) == [local_branch_membership]
    fragment = _client(owner).get(
        reverse("users:user_access", args=[target.pk]),
        headers={"HX-Request": "true", "HX-Target": "main-content"},
    )
    assert fragment.status_code == 200
    assert 'id="employee-access"' in fragment.content.decode()
    assert "<html" not in fragment.content.decode()
    assert "hx-request" in fragment.headers["Vary"].lower()
