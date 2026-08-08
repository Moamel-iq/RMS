"""
Branch access queries.

This module is the single answer to "which branches may this user see". Every
selector that reads branch-owned data must start from here; a query that
filters by branch on its own will eventually forget a case and leak one
branch's figures into another's report.

Reads only. Nothing here writes.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.organizations.models import Branch, BranchMembership, Organization, Role
from apps.users.models import User


def accessible_branches(user: User) -> QuerySet[Branch]:
    """
    Active branches this user may act on, in active organizations.

    Access arrives two ways: a membership at the branch itself, or
    organization-wide authority over the organization that owns it. The second
    is not a shortcut — an Accounting Manager holds authority over every branch
    of their organization by definition, and without it they could reopen a
    period covering all of them yet not post a single adjustment into any.

    The containment runs one way only. Organization authority reaches every
    branch; no accumulation of branch memberships ever adds up to organization
    authority (see `apps.organizations.authorization.organization_scope`).

    An inactive membership, an inactive branch, or an inactive organization
    each remove access. Anonymous and inactive users get nothing.
    """
    if not user.is_authenticated or not user.is_active:
        return Branch.objects.none()

    base = Branch.objects.filter(is_active=True, organization__is_active=True)

    # A Django superuser is already unrestricted everywhere else; filtering
    # here would be theatre, not security. Made explicit so it is testable.
    if user.is_superuser:
        return base

    return base.filter(
        Q(memberships__user=user, memberships__is_active=True)
        | Q(
            organization__memberships__user=user,
            organization__memberships__is_active=True,
        )
    ).distinct()


def accessible_branch_ids(user: User) -> list[int]:
    """Primary keys of the accessible branches, for use in `__in` filters."""
    return list(accessible_branches(user).values_list("id", flat=True))


def can_access_branch(user: User, branch: Branch) -> bool:
    """Whether this user may act on this specific branch."""
    return accessible_branches(user).filter(pk=branch.pk).exists()


def accessible_organizations(user: User) -> QuerySet[Organization]:
    """Active organizations reachable through the user's branch access."""
    if not user.is_authenticated or not user.is_active:
        return Organization.objects.none()

    base = Organization.objects.filter(is_active=True)
    if user.is_superuser:
        return base

    return base.filter(branches__in=accessible_branches(user)).distinct()


def role_at_branch(user: User, branch: Branch) -> str | None:
    """
    The user's role at this branch, or None if they hold no active membership.

    A superuser without an explicit membership has no role. Being able to see
    everything is not the same as holding a post, and posting rules that key
    off role must not silently treat an administrator as, say, a cashier.
    """
    membership = (
        BranchMembership.objects.filter(user=user, branch=branch, is_active=True)
        .only("role")
        .first()
    )
    return membership.role if membership else None


def has_role_at_branch(user: User, branch: Branch, *roles: str) -> bool:
    """Whether the user holds any of the given roles at this branch."""
    current = role_at_branch(user, branch)
    return current is not None and current in roles


def branches_for_role(user: User, role: Role | str) -> QuerySet[Branch]:
    """Branches where this user holds the given role."""
    return accessible_branches(user).filter(
        memberships__user=user,
        memberships__is_active=True,
        memberships__role=role,
    )
