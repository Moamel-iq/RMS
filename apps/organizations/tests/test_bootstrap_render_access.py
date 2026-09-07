"""Tests for the idempotent Render access bootstrap."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.organizations.models import Organization, OrganizationMembership, Role, RoleDefinition
from apps.organizations.roles import sync_role_definition_group
from apps.organizations.services import create_organization
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name="خان مندي")


def test_bootstrap_repairs_existing_owner_and_is_idempotent(organization: Organization) -> None:
    owner = User.objects.create_user(username="moamel", password="safe-test-password")

    call_command(
        "bootstrap_render_access",
        organization_code=organization.code,
        owner_username=owner.username,
    )
    call_command(
        "bootstrap_render_access",
        organization_code=organization.code,
        owner_username=owner.username,
    )

    owner.refresh_from_db()
    assert owner.is_active
    assert owner.is_staff
    assert owner.is_superuser
    assert (
        OrganizationMembership.objects.filter(
            user=owner, organization=organization, role=Role.OWNER, is_active=True
        ).count()
        == 1
    )
    assert owner.groups.filter(name="role:OWNER").exists()
    assert Group.objects.filter(name="role:STOREKEEPER").exists()
    assert Group.objects.filter(
        name="role:MANAGER",
        permissions__content_type__app_label="organizations",
        permissions__codename="manage_users",
    ).exists()
    assert Group.objects.filter(
        name="role:OWNER",
        permissions__content_type__app_label="insights",
        permissions__codename="view_insight",
    ).exists()
    assert not Group.objects.filter(
        name="role:STOREKEEPER",
        permissions__content_type__app_label="procurement",
        permissions__codename="create_purchase_request",
    ).exists()


def test_bootstrap_removes_retired_procurement_permissions_from_custom_roles(
    organization: Organization,
) -> None:
    definition = RoleDefinition.objects.create(
        organization=organization,
        code="legacy-buyer",
        name="مشتريات قديمة",
    )
    legacy = Permission.objects.get(
        content_type__app_label="procurement", codename="create_purchase_request"
    )
    current = Permission.objects.get(
        content_type__app_label="procurement", codename="view_supplier"
    )
    definition.permissions.add(legacy, current)
    sync_role_definition_group(definition)

    owner = User.objects.create_user(username="moamel", password="safe-test-password")
    call_command(
        "bootstrap_render_access",
        organization_code=organization.code,
        owner_username=owner.username,
    )

    definition.refresh_from_db()
    held = {permission.codename for permission in definition.permissions.all()}
    assert held == {"view_supplier"}
    group = Group.objects.get(name=f"role:{definition.key}")
    assert set(group.permissions.values_list("codename", flat=True)) == {"view_supplier"}


def test_bootstrap_creates_owner_only_with_an_explicit_password(organization: Organization) -> None:
    with pytest.raises(CommandError, match="RENDER_OWNER_PASSWORD"):
        call_command(
            "bootstrap_render_access",
            organization_code=organization.code,
            owner_username="new-owner",
        )

    call_command(
        "bootstrap_render_access",
        organization_code=organization.code,
        owner_username="new-owner",
        owner_password="safe-test-password",
    )

    owner = User.objects.get(username="new-owner")
    assert owner.check_password("safe-test-password")
    assert owner.is_superuser
    assert OrganizationMembership.objects.filter(
        user=owner, organization=organization, role=Role.OWNER, is_active=True
    ).exists()
