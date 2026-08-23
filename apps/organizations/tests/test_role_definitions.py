"""
Custom roles (ADR-034): a post the organization defines, granted like any other.

What these fix:

* a custom role's permissions reach its members through the same `role:`
  groups and the same provenance rule as a built-in post — inside the
  organization that defined it, and nowhere else;
* a definition of one organization cannot be granted in another;
* changing the definition changes what every holder may do, at once;
* a post still held by someone cannot be archived — people are revoked one by
  one, with a record each, and only then does the post go.
"""

from __future__ import annotations

from datetime import time

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError

from apps.inventory.permissions import CREATE_ITEM, EDIT_ITEM, MANAGE_ITEMS, VIEW_ITEM, VIEW_STOCK
from apps.organizations.authorization import (
    has_organization_master_data_permission,
    organizations_with_permission,
)
from apps.organizations.models import Branch, Organization, Role, RoleDefinition
from apps.organizations.roles import custom_role_key, role_choices, role_label, validate_role_key
from apps.organizations.services import (
    archive_role_definition,
    create_branch,
    create_organization,
    create_role_definition,
    grant_branch_access,
    grant_organization_access,
    reactivate_role_definition,
    revoke_branch_access,
    update_role_definition,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

OPENING = time(9, 0)


def _codes(error: ValidationError) -> set[str]:
    return {e.code or "" for e in error.error_list}


@pytest.fixture
def khan() -> Organization:
    return create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def rival() -> Organization:
    return create_organization(code="RIVAL", name_ar="منافس", name_en="Rival")


@pytest.fixture
def bunook(khan: Organization) -> Branch:
    return create_branch(
        organization=khan,
        code="011",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=OPENING,
    )


@pytest.fixture
def rival_branch(rival: Organization) -> Branch:
    return create_branch(
        organization=rival,
        code="R01",
        name_ar="فرع المنافس",
        name_en="Rival branch",
        business_day_start_time=OPENING,
    )


@pytest.fixture
def clerk() -> User:
    return User.objects.create_user(username="clerk", password="pw-not-real-1234")


@pytest.fixture
def registrar(khan: Organization) -> RoleDefinition:
    """The owner's own example: may see and add items, may not edit them."""
    return create_role_definition(
        organization=khan,
        code="registrar",
        name_ar="مسجّل أصناف",
        permissions=[VIEW_ITEM, CREATE_ITEM],
    )


class TestDefinition:
    def test_a_definition_is_a_role_group_carrying_exactly_its_permissions(
        self, khan: Organization, registrar: RoleDefinition
    ) -> None:
        assert (
            registrar.key == custom_role_key(khan.pk, "registrar") == f"custom:{khan.pk}:registrar"
        )
        group = Group.objects.get(name=f"role:{registrar.key}")
        held = {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()}
        assert held == {VIEW_ITEM, CREATE_ITEM}

    def test_the_code_is_a_lower_case_slug_and_unique_per_organization(
        self, khan: Organization, rival: Organization, registrar: RoleDefinition
    ) -> None:
        with pytest.raises(ValidationError):
            create_role_definition(
                organization=khan, code="Registrar!", name_ar="x", permissions=[]
            )
        with pytest.raises(ValidationError):
            create_role_definition(organization=khan, code="registrar", name_ar="x", permissions=[])
        # The same code in another organization is another post entirely.
        other = create_role_definition(
            organization=rival, code="registrar", name_ar="x", permissions=[]
        )
        assert other.key != registrar.key

    def test_a_permission_outside_the_modules_or_unknown_refuses_the_whole_call(
        self, khan: Organization
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            create_role_definition(
                organization=khan,
                code="admin-ish",
                name_ar="x",
                permissions=[VIEW_ITEM, "auth.add_user"],
            )
        assert "unknown_permission" in _codes(refused.value)
        assert not RoleDefinition.objects.filter(code="admin-ish").exists()

    def test_labels_and_choices_name_the_post_in_arabic(
        self, khan: Organization, registrar: RoleDefinition
    ) -> None:
        assert role_label(registrar.key) == "مسجّل أصناف"
        assert role_label(Role.ACCOUNTANT.value) == str(Role.ACCOUNTANT.label)
        choices = dict(role_choices([khan]))
        assert choices[registrar.key] == "مسجّل أصناف"
        assert choices[Role.OWNER.value] == str(Role.OWNER.label)


class TestGrantAndProvenance:
    def test_a_holder_gets_the_permissions_inside_the_organization_and_nowhere_else(
        self,
        khan: Organization,
        rival: Organization,
        bunook: Branch,
        rival_branch: Branch,
        clerk: User,
        registrar: RoleDefinition,
    ) -> None:
        grant_branch_access(user=clerk, branch=bunook, role=registrar.key)
        # A read-only post at the rival: reach, but no item authority there.
        grant_branch_access(user=clerk, branch=rival_branch, role=Role.VIEWER)

        assert has_organization_master_data_permission(clerk, CREATE_ITEM, khan)
        assert has_organization_master_data_permission(clerk, VIEW_ITEM, khan)
        assert not has_organization_master_data_permission(clerk, EDIT_ITEM, khan)
        assert not has_organization_master_data_permission(clerk, MANAGE_ITEMS, khan)
        # Provenance: the custom post was granted in Khan Mandi only.
        assert not has_organization_master_data_permission(clerk, CREATE_ITEM, rival)
        assert list(organizations_with_permission(clerk, CREATE_ITEM)) == [khan]

    def test_a_definition_cannot_be_granted_in_another_organization(
        self, rival_branch: Branch, rival: Organization, clerk: User, registrar: RoleDefinition
    ) -> None:
        with pytest.raises(ValidationError) as refused:
            grant_branch_access(user=clerk, branch=rival_branch, role=registrar.key)
        assert "role_belongs_to_another_organization" in _codes(refused.value)
        with pytest.raises(ValidationError) as refused_org:
            grant_organization_access(user=clerk, organization=rival, role=registrar.key)
        assert "role_belongs_to_another_organization" in _codes(refused_org.value)

    def test_an_unknown_or_malformed_key_is_refused(
        self, khan: Organization, bunook: Branch, clerk: User
    ) -> None:
        for key in ("custom:", "custom:x:y", f"custom:{khan.pk}:never-defined", "SUPERVISOR"):
            with pytest.raises(ValidationError) as refused:
                grant_branch_access(user=clerk, branch=bunook, role=key)
            assert "unknown_role" in _codes(refused.value), key
        assert validate_role_key(Role.MANAGER, khan) == "MANAGER"

    def test_changing_the_definition_changes_what_every_holder_may_do(
        self, khan: Organization, bunook: Branch, clerk: User, registrar: RoleDefinition
    ) -> None:
        grant_branch_access(user=clerk, branch=bunook, role=registrar.key)
        assert not has_organization_master_data_permission(clerk, EDIT_ITEM, khan)

        update_role_definition(
            definition=registrar, permissions=[VIEW_ITEM, CREATE_ITEM, EDIT_ITEM]
        )
        # A fresh instance: Django caches resolved permissions on the object.
        holder = User.objects.get(pk=clerk.pk)
        assert has_organization_master_data_permission(holder, EDIT_ITEM, khan)

        update_role_definition(definition=registrar, permissions=[VIEW_STOCK])
        holder = User.objects.get(pk=clerk.pk)
        assert not has_organization_master_data_permission(holder, CREATE_ITEM, khan)
        assert has_organization_master_data_permission(holder, VIEW_STOCK, khan)


class TestLifecycle:
    def test_a_post_still_held_cannot_be_archived(
        self, bunook: Branch, clerk: User, registrar: RoleDefinition
    ) -> None:
        grant_branch_access(user=clerk, branch=bunook, role=registrar.key)
        with pytest.raises(ValidationError) as refused:
            archive_role_definition(definition=registrar, reason="لم يعد مطلوباً")
        assert "role_in_use" in _codes(refused.value)

        revoke_branch_access(user=clerk, branch=bunook)
        archived = archive_role_definition(definition=registrar, reason="لم يعد مطلوباً")
        assert not archived.is_active

    def test_an_archived_post_is_not_granted_and_keeps_its_label(
        self, khan: Organization, bunook: Branch, clerk: User, registrar: RoleDefinition
    ) -> None:
        archive_role_definition(definition=registrar, reason="تجربة")
        with pytest.raises(ValidationError) as refused:
            grant_branch_access(user=clerk, branch=bunook, role=registrar.key)
        assert "role_archived" in _codes(refused.value)
        assert role_label(registrar.key) == "مسجّل أصناف"
        assert registrar.key not in dict(role_choices([khan]))

        reactivate_role_definition(definition=registrar, reason="عاد الاحتياج")
        grant_branch_access(user=clerk, branch=bunook, role=registrar.key)
        assert has_organization_master_data_permission(clerk, CREATE_ITEM, khan)

    def test_the_built_in_groups_are_left_alone_by_custom_sync(
        self, khan: Organization, registrar: RoleDefinition
    ) -> None:
        """A custom role writes its own group and never the charter's posts."""
        manager_group = Group.objects.get(name="role:MANAGER")
        before = set(manager_group.permissions.values_list("codename", flat=True))
        update_role_definition(definition=registrar, permissions=[])
        after = set(
            Group.objects.get(name="role:MANAGER").permissions.values_list("codename", flat=True)
        )
        assert before == after and "manage_items" in after
