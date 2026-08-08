"""
Roles as Django groups.

Django's permission system is global: `user.has_perm("accounting.post_journal")`
answers "may this person post at all", never "may this person post here". That
second question is answered separately by `apps.organizations.authorization`,
against the user's memberships.

Splitting it this way keeps one authority list per role instead of one per
role-and-place, and it means a permission can only ever be *narrowed* by
scope, never widened by it.

Each role is one group named `role:<ROLE>`. Modules contribute their own
permissions into those groups — accounting does so in
`apps/accounting/permissions.py` — so a role's authority grows as modules
arrive without this file having to know about them.
"""

from __future__ import annotations

from django.contrib.auth.models import Group

from apps.organizations.models import Role
from apps.users.models import User

#: Prefix so a role group is never confused with a hand-made admin group.
ROLE_GROUP_PREFIX = "role:"


def role_group_name(role: Role | str) -> str:
    """The group name carrying one role's permissions."""
    value = role.value if isinstance(role, Role) else str(role)
    return f"{ROLE_GROUP_PREFIX}{value}"


def group_for_role(role: Role | str) -> Group:
    """The group for a role, created empty if it does not exist yet."""
    group, _created = Group.objects.get_or_create(name=role_group_name(role))
    return group


def roles_held_by(user: User) -> set[str]:
    """
    Every role this user currently holds anywhere, branch or organization.

    Only active memberships in active places count. An inactive membership is
    kept for the audit trail, not for access.
    """
    from apps.organizations.models import BranchMembership, OrganizationMembership

    branch_roles = BranchMembership.objects.filter(
        user=user,
        is_active=True,
        branch__is_active=True,
        branch__organization__is_active=True,
    ).values_list("role", flat=True)

    organization_roles = OrganizationMembership.objects.filter(
        user=user,
        is_active=True,
        organization__is_active=True,
    ).values_list("role", flat=True)

    return set(branch_roles) | set(organization_roles)


def sync_user_role_groups(user: User) -> None:
    """
    Recompute this user's role groups from their memberships.

    Recomputed rather than incremented: an add-on-grant / remove-on-revoke
    pair silently leaves the permission behind when someone holds the same
    role in two places and loses only one of them. Deriving the whole set from
    the memberships that actually exist cannot drift.

    Groups outside the `role:` namespace are left alone, so a hand-made group
    is never destroyed by a membership change.
    """
    wanted = {role_group_name(role) for role in roles_held_by(user)}
    current = set(
        user.groups.filter(name__startswith=ROLE_GROUP_PREFIX).values_list("name", flat=True)
    )

    for name in current - wanted:
        user.groups.remove(Group.objects.get(name=name))
    for name in wanted - current:
        group, _created = Group.objects.get_or_create(name=name)
        user.groups.add(group)

    # Django caches resolved permissions on the user instance. Without this,
    # a check made later in the same request would still see the old set.
    for attribute in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
        user.__dict__.pop(attribute, None)
