"""
Where a permission came from, not just whether the user has one.

Django permissions are global by construction: role groups are recomputed from
*every* membership a user holds, so someone who manages one organization
carries `inventory.manage_items` everywhere at once. Pairing that global answer
with mere *reach* to a second organization would hand them the first
organization's authority in the second one — and reach is cheap, because a
read-only viewer post grants it.

So both halves must come from the same place. These tests fix that:

    the permission must be carried by a role the caller holds **inside the
    target organization** — over it, or at one of its branches.

The defect this closes was live: before it,
`require_reachable_organization_permission` combined `user.has_perm(...)` with
`resolve_organization(...)`, and a manager in Khan Mandi who also held a viewer
post at a rival branch could rewrite the rival's item master.
"""

from __future__ import annotations

from datetime import time

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied

from apps.inventory.permissions import (
    MANAGE_ITEMS,
    OVERRIDE_NEGATIVE_STOCK,
    POST_OPENING_STOCK,
    VIEW_ITEM,
)
from apps.organizations.authorization import (
    OutOfScope,
    has_organization_master_data_permission,
    has_organization_permission,
    organizations_with_permission,
    require_reachable_organization_permission,
    roles_granting,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    grant_organization_access,
    revoke_branch_access,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"


@pytest.fixture
def khan_mandi() -> Organization:
    return create_organization(code="KM", name="خان مندي")


@pytest.fixture
def rival() -> Organization:
    return create_organization(code="RIVAL", name="منافس")


def _branch(organization: Organization, code: str) -> Branch:
    return create_branch(
        organization=organization,
        code=code,
        name=code,
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def bunook(khan_mandi: Organization) -> Branch:
    return _branch(khan_mandi, "BUNOOK")


@pytest.fixture
def rival_branch(rival: Organization) -> Branch:
    return _branch(rival, "RIVALBR")


def _reload(user: User) -> User:
    """Django caches resolved permissions on the instance."""
    return User.objects.get(pk=user.pk)


class TestTheProvenanceRule:
    """
    Case 1 in the review, and the one that was actually broken.
    """

    def test_a_manager_here_and_a_viewer_there_cannot_manage_there(
        self, khan_mandi: Organization, rival: Organization, bunook: Branch, rival_branch: Branch
    ) -> None:
        user = User.objects.create_user(username="two-hats", password=PASSWORD)
        grant_branch_access(user=user, branch=bunook, role=Role.MANAGER)
        grant_branch_access(user=user, branch=rival_branch, role=Role.VIEWER)
        user = _reload(user)

        # The global permission is genuinely held — Khan Mandi needs it.
        assert user.has_perm(MANAGE_ITEMS)
        # And the rival organization is genuinely reachable, through the
        # viewer post. Reach is not authority.
        assert has_organization_master_data_permission(user, MANAGE_ITEMS, khan_mandi)
        assert not has_organization_master_data_permission(user, MANAGE_ITEMS, rival)

        with pytest.raises(PermissionDenied):
            require_reachable_organization_permission(user, MANAGE_ITEMS, rival)

    def test_a_branch_manager_may_manage_their_own_organization(
        self, rival: Organization, rival_branch: Branch
    ) -> None:
        """
        Case 2. The item master is organization-owned, and a branch manager
        legitimately maintains it — that is why master data uses reach rather
        than demanding organization-wide authority.
        """
        user = User.objects.create_user(username="rival-manager", password=PASSWORD)
        grant_branch_access(user=user, branch=rival_branch, role=Role.MANAGER)
        user = _reload(user)

        require_reachable_organization_permission(user, MANAGE_ITEMS, rival)
        assert has_organization_master_data_permission(user, MANAGE_ITEMS, rival)

    def test_losing_the_manager_post_removes_authority_immediately(
        self, khan_mandi: Organization, rival: Organization, bunook: Branch, rival_branch: Branch
    ) -> None:
        """
        Case 3. Revoke the only post that carried it in this organization, and
        write authority here goes at once — even though a viewer post keeps the
        organization reachable and a manager post elsewhere keeps the global
        permission alive.
        """
        user = User.objects.create_user(username="demoted", password=PASSWORD)
        grant_branch_access(user=user, branch=rival_branch, role=Role.MANAGER)
        grant_branch_access(user=user, branch=bunook, role=Role.MANAGER)
        second_rival = _branch(rival, "RIVAL2")
        grant_branch_access(user=user, branch=second_rival, role=Role.VIEWER)
        user = _reload(user)
        assert has_organization_master_data_permission(user, MANAGE_ITEMS, rival)

        revoke_branch_access(user=user, branch=rival_branch)
        user = _reload(user)

        # Khan Mandi still needs the role, so the group and the global
        # permission both survive...
        assert user.has_perm(MANAGE_ITEMS)
        # ...and the rival organization is still reachable through the viewer.
        require_reachable_organization_permission(user, VIEW_ITEM, rival)
        # But nothing there carries the manage permission any more.
        assert not has_organization_master_data_permission(user, MANAGE_ITEMS, rival)
        with pytest.raises(PermissionDenied):
            require_reachable_organization_permission(user, MANAGE_ITEMS, rival)

    def test_two_manager_posts_stay_independently_scoped(
        self, khan_mandi: Organization, rival: Organization, bunook: Branch, rival_branch: Branch
    ) -> None:
        """
        Case 4. Holding the post in two organizations authorizes both, and
        losing one leaves the other exactly as it was.
        """
        user = User.objects.create_user(username="both", password=PASSWORD)
        grant_branch_access(user=user, branch=bunook, role=Role.MANAGER)
        grant_branch_access(user=user, branch=rival_branch, role=Role.MANAGER)
        user = _reload(user)

        assert has_organization_master_data_permission(user, MANAGE_ITEMS, khan_mandi)
        assert has_organization_master_data_permission(user, MANAGE_ITEMS, rival)

        revoke_branch_access(user=user, branch=bunook)
        user = _reload(user)

        assert not has_organization_master_data_permission(user, MANAGE_ITEMS, khan_mandi)
        assert has_organization_master_data_permission(user, MANAGE_ITEMS, rival)

    def test_a_hand_made_group_authorizes_no_organization(
        self, khan_mandi: Organization, bunook: Branch
    ) -> None:
        """
        Case 5. A permission that names no post nobody holds grants nothing.

        A deployment can still widen authority — by changing the role map, or
        by giving someone the role. What it cannot do is create authority out
        of a group that sits outside the role namespace, because such a group
        says nothing about *where* the authority applies.
        """
        user = User.objects.create_user(username="hand-made", password=PASSWORD)
        grant_branch_access(user=user, branch=bunook, role=Role.VIEWER)

        ad_hoc = Group.objects.create(name="night-shift")
        ad_hoc.permissions.add(
            Permission.objects.get(content_type__app_label="inventory", codename="manage_items")
        )
        user.groups.add(ad_hoc)
        user = _reload(user)

        assert user.has_perm(MANAGE_ITEMS)  # globally, yes
        assert not has_organization_master_data_permission(user, MANAGE_ITEMS, khan_mandi)
        with pytest.raises(PermissionDenied):
            require_reachable_organization_permission(user, MANAGE_ITEMS, khan_mandi)

    def test_a_permission_attached_directly_to_a_user_authorizes_nothing(
        self, khan_mandi: Organization, bunook: Branch
    ) -> None:
        """The same claim through the other channel Django offers."""
        user = User.objects.create_user(username="direct", password=PASSWORD)
        grant_branch_access(user=user, branch=bunook, role=Role.VIEWER)
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label="inventory", codename="manage_items")
        )
        user = _reload(user)

        assert user.has_perm(MANAGE_ITEMS)
        assert not has_organization_master_data_permission(user, MANAGE_ITEMS, khan_mandi)

    def test_an_organization_id_from_another_scope_is_a_404(
        self, rival: Organization, bunook: Branch
    ) -> None:
        """
        Case 6. Submitting an id the caller does not reach at all answers
        "does not exist", not "not permitted" — a 403 would confirm the
        organization is real, and ids are sequential.
        """
        user = User.objects.create_user(username="outsider", password=PASSWORD)
        grant_branch_access(user=user, branch=bunook, role=Role.MANAGER)
        user = _reload(user)

        with pytest.raises(OutOfScope):
            require_reachable_organization_permission(user, MANAGE_ITEMS, rival)


class TestOrganizationAuthorityProvenance:
    """
    The stronger scope, checked the same way. `post_opening_stock` and
    `override_negative_stock` need an organization membership, and the role
    that carries them must be *that* membership's role.
    """

    def test_a_branch_post_does_not_carry_organization_authority(
        self, khan_mandi: Organization, bunook: Branch
    ) -> None:
        user = User.objects.create_user(username="branch-am", password=PASSWORD)
        # Holds the branch post that carries the permission globally...
        grant_branch_access(user=user, branch=bunook, role=Role.ACCOUNTING_MANAGER)
        # ...and an organization post that does not.
        grant_organization_access(user=user, organization=khan_mandi, role=Role.VIEWER)
        user = _reload(user)

        assert user.has_perm(POST_OPENING_STOCK)
        assert not has_organization_permission(user, POST_OPENING_STOCK, khan_mandi)
        assert not has_organization_permission(user, OVERRIDE_NEGATIVE_STOCK, khan_mandi)

    def test_the_organization_post_carries_it(self, khan_mandi: Organization) -> None:
        user = User.objects.create_user(username="org-am", password=PASSWORD)
        grant_organization_access(user=user, organization=khan_mandi, role=Role.ACCOUNTING_MANAGER)
        user = _reload(user)

        assert has_organization_permission(user, POST_OPENING_STOCK, khan_mandi)


class TestRolesGranting:
    """`roles_granting` is read from the role groups, so it stays true as modules land."""

    def test_it_names_the_roles_the_map_gave_it(self, khan_mandi: Organization) -> None:
        roles = roles_granting(MANAGE_ITEMS)
        assert Role.MANAGER.value in roles
        assert Role.OWNER.value in roles
        assert Role.VIEWER.value not in roles
        assert Role.CASHIER.value not in roles

    def test_an_unknown_permission_names_nobody(self) -> None:
        assert roles_granting("inventory.no_such_permission") == set()


class TestBulkAnswersMatchTheSingleCheck:
    """
    A screen gates its buttons with `organizations_with_permission`. If that
    ever disagreed with the check the service makes, the screen would offer a
    button the write would refuse — or, far worse, hide one it would allow.
    """

    def test_the_two_agree(
        self, khan_mandi: Organization, rival: Organization, bunook: Branch, rival_branch: Branch
    ) -> None:
        user = User.objects.create_user(username="agrees", password=PASSWORD)
        grant_branch_access(user=user, branch=bunook, role=Role.MANAGER)
        grant_branch_access(user=user, branch=rival_branch, role=Role.VIEWER)
        user = _reload(user)

        bulk = set(organizations_with_permission(user, MANAGE_ITEMS).values_list("id", flat=True))
        one_by_one = {
            organization.pk
            for organization in Organization.objects.all()
            if has_organization_master_data_permission(user, MANAGE_ITEMS, organization)
        }
        assert bulk == one_by_one == {khan_mandi.pk}

    def test_a_superuser_reaches_every_organization(self, khan_mandi: Organization) -> None:
        """
        Emergency authority holds no membership, so provenance is short-
        circuited rather than failed. It is still not a bypass: the services
        behind the check validate everything else exactly as before.
        """
        root = User.objects.create_superuser(username="root", password=PASSWORD)
        assert has_organization_master_data_permission(root, MANAGE_ITEMS, khan_mandi)
        assert khan_mandi.pk in set(
            organizations_with_permission(root, MANAGE_ITEMS).values_list("id", flat=True)
        )
