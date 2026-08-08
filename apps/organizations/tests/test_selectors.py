"""
Branch access isolation.

These are the tests that stop one branch's figures appearing in another
branch's report, and one organization's data appearing in another's at all.
"""

from __future__ import annotations

from datetime import time

import pytest
from django.contrib.auth.models import AnonymousUser

from apps.organizations.models import Branch, Organization, Role
from apps.organizations.selectors import (
    accessible_branch_ids,
    accessible_branches,
    accessible_organizations,
    branches_for_role,
    can_access_branch,
    has_role_at_branch,
    role_at_branch,
)
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    revoke_branch_access,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

OPENING = time(9, 0)
PASSWORD = "pw-not-real-1234"


def _branch(organization: Organization, code: str) -> Branch:
    return create_branch(
        organization=organization,
        code=code,
        name_ar=f"فرع {code}",
        name_en=f"Branch {code}",
        business_day_start_time=OPENING,
    )


@pytest.fixture
def khan() -> Organization:
    return create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def rival() -> Organization:
    return create_organization(code="RIVAL", name_ar="منافس", name_en="Rival Group")


@pytest.fixture
def bunook(khan: Organization) -> Branch:
    return _branch(khan, "BUNOOK")


@pytest.fixture
def karrada(khan: Organization) -> Branch:
    return _branch(khan, "KARRADA")


@pytest.fixture
def rival_branch(rival: Organization) -> Branch:
    return _branch(rival, "RIVALBR")


@pytest.fixture
def accountant(bunook: Branch) -> User:
    user = User.objects.create_user(username="accountant", password=PASSWORD)
    grant_branch_access(user=user, branch=bunook, role=Role.ACCOUNTANT)
    return user


class TestAccessIsGranted:
    def test_member_sees_their_branch(self, accountant: User, bunook: Branch) -> None:
        assert list(accessible_branches(accountant)) == [bunook]

    def test_member_does_not_see_a_branch_they_were_not_granted(
        self, accountant: User, karrada: Branch
    ) -> None:
        assert karrada not in accessible_branches(accountant)
        assert can_access_branch(accountant, karrada) is False

    def test_member_does_not_see_another_organizations_branch(
        self, accountant: User, rival_branch: Branch
    ) -> None:
        """Cross-organization isolation. The important one."""
        assert can_access_branch(accountant, rival_branch) is False
        assert rival_branch not in accessible_branches(accountant)

    def test_accessible_organizations_follows_branch_access(
        self, accountant: User, khan: Organization, rival: Organization
    ) -> None:
        organizations = list(accessible_organizations(accountant))
        assert organizations == [khan]
        assert rival not in organizations

    def test_multiple_grants_are_not_duplicated(
        self, accountant: User, bunook: Branch, karrada: Branch
    ) -> None:
        grant_branch_access(user=accountant, branch=karrada, role=Role.ACCOUNTANT)
        branch_ids = accessible_branch_ids(accountant)
        assert sorted(branch_ids) == sorted([bunook.id, karrada.id])
        assert len(branch_ids) == len(set(branch_ids))


class TestAccessIsWithdrawn:
    def test_revoking_removes_access(self, accountant: User, bunook: Branch) -> None:
        revoke_branch_access(user=accountant, branch=bunook)
        assert can_access_branch(accountant, bunook) is False

    def test_revoking_keeps_the_record(self, accountant: User, bunook: Branch) -> None:
        """An audit needs to see that this person once held this post."""
        revoke_branch_access(user=accountant, branch=bunook)
        assert accountant.branch_memberships.filter(branch=bunook).exists()

    def test_inactive_branch_is_not_accessible(self, accountant: User, bunook: Branch) -> None:
        bunook.is_active = False
        bunook.save(update_fields=["is_active"])
        assert can_access_branch(accountant, bunook) is False

    def test_inactive_organization_removes_all_its_branches(
        self, accountant: User, bunook: Branch, khan: Organization
    ) -> None:
        khan.is_active = False
        khan.save(update_fields=["is_active"])
        assert list(accessible_branches(accountant)) == []

    def test_deactivated_user_loses_access(self, accountant: User) -> None:
        accountant.is_active = False
        accountant.save(update_fields=["is_active"])
        assert list(accessible_branches(accountant)) == []

    def test_anonymous_user_sees_nothing(self, bunook: Branch) -> None:
        anonymous = AnonymousUser()
        assert list(accessible_branches(anonymous)) == []  # type: ignore[arg-type]
        assert can_access_branch(anonymous, bunook) is False  # type: ignore[arg-type]

    def test_user_with_no_memberships_sees_nothing(self, bunook: Branch) -> None:
        stranger = User.objects.create_user(username="stranger", password=PASSWORD)
        assert list(accessible_branches(stranger)) == []


class TestSuperuser:
    def test_superuser_sees_every_active_branch(
        self, bunook: Branch, karrada: Branch, rival_branch: Branch
    ) -> None:
        """Explicit and tested, rather than an accident of the ORM."""
        admin = User.objects.create_superuser(username="admin", password=PASSWORD)
        assert set(accessible_branches(admin)) == {bunook, karrada, rival_branch}

    def test_superuser_does_not_see_inactive_branches(
        self, bunook: Branch, karrada: Branch
    ) -> None:
        karrada.is_active = False
        karrada.save(update_fields=["is_active"])
        admin = User.objects.create_superuser(username="admin", password=PASSWORD)
        assert set(accessible_branches(admin)) == {bunook}

    def test_superuser_holds_no_role_without_a_membership(self, bunook: Branch) -> None:
        """
        Seeing everything is not the same as holding a post. A posting rule
        keyed on role must not treat an administrator as a cashier.
        """
        admin = User.objects.create_superuser(username="admin", password=PASSWORD)
        assert role_at_branch(admin, bunook) is None


class TestRoles:
    def test_role_is_reported(self, accountant: User, bunook: Branch) -> None:
        assert role_at_branch(accountant, bunook) == Role.ACCOUNTANT

    def test_role_is_none_at_a_branch_without_membership(
        self, accountant: User, karrada: Branch
    ) -> None:
        assert role_at_branch(accountant, karrada) is None

    def test_has_role_matches_any_listed_role(self, accountant: User, bunook: Branch) -> None:
        assert has_role_at_branch(accountant, bunook, Role.ACCOUNTANT, Role.MANAGER) is True
        assert has_role_at_branch(accountant, bunook, Role.CASHIER) is False

    def test_revoked_membership_reports_no_role(self, accountant: User, bunook: Branch) -> None:
        revoke_branch_access(user=accountant, branch=bunook)
        assert role_at_branch(accountant, bunook) is None

    def test_branches_for_role_filters_correctly(
        self, accountant: User, bunook: Branch, karrada: Branch
    ) -> None:
        grant_branch_access(user=accountant, branch=karrada, role=Role.CASHIER)
        assert list(branches_for_role(accountant, Role.ACCOUNTANT)) == [bunook]
        assert list(branches_for_role(accountant, Role.CASHIER)) == [karrada]


class TestGrantIsRepeatable:
    def test_regranting_updates_the_role_in_place(self, accountant: User, bunook: Branch) -> None:
        original = accountant.branch_memberships.get(branch=bunook)
        grant_branch_access(user=accountant, branch=bunook, role=Role.MANAGER)
        refreshed = accountant.branch_memberships.get(branch=bunook)
        assert refreshed.pk == original.pk
        assert refreshed.role == Role.MANAGER
        assert accountant.branch_memberships.filter(branch=bunook).count() == 1

    def test_regranting_reactivates_a_revoked_membership(
        self, accountant: User, bunook: Branch
    ) -> None:
        revoke_branch_access(user=accountant, branch=bunook)
        grant_branch_access(user=accountant, branch=bunook, role=Role.ACCOUNTANT)
        assert can_access_branch(accountant, bunook) is True
