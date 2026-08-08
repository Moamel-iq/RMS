"""
The twelve permissions, the roles that hold them, and the scope they hold them in.

Not a restatement of the mapping table in another syntax — that would pass
whatever the table said. These assert the *consequences* the mapping is
supposed to have, above all the one the whole design exists for: that holding
a permission and holding it *here* are two different facts.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, Permission

from apps.accounting.permissions import (
    ALL_PERMISSIONS,
    PERMISSION_SCOPE,
    REOPEN_PERIOD,
    ROLE_PERMISSIONS,
    PermissionScope,
    permissions_for_role,
    scope_of,
)
from apps.organizations.authorization import (
    has_branch_permission,
    has_organization_permission,
    has_organization_scope,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.permissions import role_group_name, roles_held_by
from apps.organizations.services import (
    grant_branch_access,
    revoke_branch_access,
    revoke_organization_access,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db


class TestThePermissionsExist:
    def test_all_twelve_are_migrated(self) -> None:
        codenames = set(
            Permission.objects.filter(content_type__app_label="accounting").values_list(
                "codename", flat=True
            )
        )
        for permission in ALL_PERMISSIONS:
            assert permission.split(".", 1)[1] in codenames, permission

    def test_there_are_exactly_twelve(self) -> None:
        assert len(ALL_PERMISSIONS) == 12
        assert len(set(ALL_PERMISSIONS)) == 12

    def test_every_permission_declares_a_scope(self) -> None:
        assert set(PERMISSION_SCOPE) == set(ALL_PERMISSIONS)

    def test_period_acts_are_organization_scoped(self) -> None:
        """
        A period covers every branch at once, so branch authority must not
        reach it. This is the rule that makes `reopen_period` unreachable from
        a branch role no matter which branch the caller works at.
        """
        for permission in (
            "accounting.soft_close_period",
            "accounting.close_period",
            "accounting.reopen_period",
        ):
            assert scope_of(permission) is PermissionScope.ORGANIZATION

    def test_journal_acts_are_branch_scoped(self) -> None:
        for permission in (
            "accounting.view_journal",
            "accounting.create_draft",
            "accounting.edit_draft",
            "accounting.post_journal",
            "accounting.reverse_journal",
        ):
            assert scope_of(permission) is PermissionScope.BRANCH

    def test_an_unknown_permission_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError):
            scope_of("accounting.invent_money")


class TestRoleMapping:
    """The approved mapping. Reopen authority is the point of most of these."""

    def test_accounting_manager_may_reopen(self) -> None:
        assert REOPEN_PERIOD in permissions_for_role(Role.ACCOUNTING_MANAGER)

    def test_owner_may_reopen(self) -> None:
        assert REOPEN_PERIOD in permissions_for_role(Role.OWNER)

    @pytest.mark.parametrize(
        "role",
        [
            Role.MANAGER,
            Role.ACCOUNTANT,
            Role.CASHIER,
            Role.STOREKEEPER,
            Role.PURCHASING,
            Role.VIEWER,
        ],
    )
    def test_no_other_role_may_reopen(self, role: Role) -> None:
        """
        Branch Manager, Branch Accountant, Cashier, and the warehouse roles
        are excluded by decision, not by omission.
        """
        assert REOPEN_PERIOD not in permissions_for_role(role)

    def test_the_branch_accountant_may_post_but_not_override_a_soft_close(self) -> None:
        granted = permissions_for_role(Role.ACCOUNTANT)
        assert "accounting.post_journal" in granted
        assert "accounting.post_soft_closed_adjustment" not in granted
        assert "accounting.reverse_in_soft_closed_period" not in granted

    def test_operational_roles_hold_no_accounting_authority(self) -> None:
        assert permissions_for_role(Role.CASHIER) == frozenset()
        assert permissions_for_role(Role.STOREKEEPER) == frozenset()

    def test_every_role_is_mapped(self) -> None:
        """A role with no entry would silently carry nothing."""
        assert set(ROLE_PERMISSIONS) == {role.value for role in Role}

    def test_an_unknown_role_carries_nothing(self) -> None:
        assert permissions_for_role("SUPREME_LEADER") == frozenset()


class TestRoleGroupsFollowMemberships:
    def test_granting_a_branch_role_grants_its_permissions(self, branch: Branch) -> None:
        user = User.objects.create_user(username="new-accountant", password="pw-1234")
        assert not user.has_perm("accounting.post_journal")

        grant_branch_access(user=user, branch=branch, role=Role.ACCOUNTANT)

        fresh = User.objects.get(pk=user.pk)
        assert fresh.has_perm("accounting.post_journal")

    def test_revoking_removes_them_again(self, accountant: User, branch: Branch) -> None:
        assert accountant.has_perm("accounting.post_journal")

        revoke_branch_access(user=accountant, branch=branch)

        fresh = User.objects.get(pk=accountant.pk)
        assert not fresh.has_perm("accounting.post_journal")

    def test_losing_one_of_two_memberships_keeps_the_role(
        self, branch: Branch, second_branch: Branch
    ) -> None:
        """
        The reason the groups are recomputed rather than incremented. An
        add-on-grant / remove-on-revoke pair would strip this user's authority
        at Karrada when their Bunook membership ended.
        """
        user = User.objects.create_user(username="two-branches", password="pw-1234")
        grant_branch_access(user=user, branch=branch, role=Role.ACCOUNTANT)
        grant_branch_access(user=user, branch=second_branch, role=Role.ACCOUNTANT)

        revoke_branch_access(user=user, branch=branch)

        fresh = User.objects.get(pk=user.pk)
        assert fresh.has_perm("accounting.post_journal")
        assert roles_held_by(fresh) == {Role.ACCOUNTANT.value}

    def test_organization_membership_grants_the_role_group_too(
        self, accounting_manager: User
    ) -> None:
        assert accounting_manager.has_perm("accounting.reopen_period")
        assert accounting_manager.groups.filter(
            name=role_group_name(Role.ACCOUNTING_MANAGER)
        ).exists()

    def test_revoking_organization_access_removes_reopen(
        self, accounting_manager: User, organization: Organization
    ) -> None:
        revoke_organization_access(user=accounting_manager, organization=organization)

        fresh = User.objects.get(pk=accounting_manager.pk)
        assert not fresh.has_perm("accounting.reopen_period")

    def test_hand_made_groups_survive_a_membership_change(self, branch: Branch) -> None:
        """Only the `role:` namespace is managed. Nothing else is touched."""
        user = User.objects.create_user(username="also-in-a-group", password="pw-1234")
        bespoke = Group.objects.create(name="night-shift")
        user.groups.add(bespoke)

        grant_branch_access(user=user, branch=branch, role=Role.ACCOUNTANT)
        revoke_branch_access(user=user, branch=branch)

        assert user.groups.filter(name="night-shift").exists()


class TestPermissionIsNotAuthorization:
    """
    The central claim of the design: the same user, the same permission, a
    different place, a different answer.
    """

    def test_a_branch_accountant_holds_the_permission_only_at_their_branch(
        self, accountant: User, branch: Branch, second_branch: Branch
    ) -> None:
        assert accountant.has_perm("accounting.post_journal")
        assert has_branch_permission(accountant, "accounting.post_journal", branch)
        assert not has_branch_permission(accountant, "accounting.post_journal", second_branch)

    def test_branch_authority_is_never_organization_authority(
        self, accountant: User, organization: Organization
    ) -> None:
        """
        Even at every branch there is. Organization scope comes from an
        organization membership and from nowhere else.
        """
        assert not has_organization_scope(accountant, organization)
        assert not has_organization_permission(
            accountant, "accounting.soft_close_period", organization
        )

    def test_the_accounting_manager_holds_it_over_the_organization(
        self, accounting_manager: User, organization: Organization
    ) -> None:
        assert has_organization_scope(accounting_manager, organization)
        assert has_organization_permission(
            accounting_manager, "accounting.reopen_period", organization
        )

    def test_but_not_over_a_rival_organization(
        self, accounting_manager: User, other_organization: Organization
    ) -> None:
        assert not has_organization_scope(accounting_manager, other_organization)
        assert not has_organization_permission(
            accounting_manager, "accounting.reopen_period", other_organization
        )

    def test_a_deactivated_user_holds_nothing(self, accountant: User, branch: Branch) -> None:
        accountant.is_active = False
        accountant.save(update_fields=["is_active"])

        fresh = User.objects.get(pk=accountant.pk)
        assert not has_branch_permission(fresh, "accounting.post_journal", branch)

    def test_a_superuser_reaches_everywhere(
        self, superuser: User, organization: Organization, branch: Branch
    ) -> None:
        """
        Emergency authority. It satisfies the checks rather than skipping
        them, which is why the services behind them still apply in full.
        """
        assert has_organization_permission(superuser, "accounting.reopen_period", organization)
        assert has_branch_permission(superuser, "accounting.post_journal", branch)
