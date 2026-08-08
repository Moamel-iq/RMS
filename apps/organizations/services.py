"""
Organization and branch commands.

Every state change goes through here. Models stay free of business logic, and
callers cannot construct a half-valid row by writing fields directly.
"""

from __future__ import annotations

from datetime import time

from django.conf import settings
from django.db import transaction

from apps.organizations.models import Branch, BranchMembership, Organization, Role
from apps.users.models import User


@transaction.atomic
def create_organization(*, code: str, name_ar: str, name_en: str) -> Organization:
    organization = Organization(
        code=code.strip().upper(),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
    )
    organization.full_clean()
    organization.save()
    return organization


@transaction.atomic
def create_branch(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    name_en: str,
    business_day_start_time: time,
    timezone: str = settings.TIME_ZONE,
) -> Branch:
    """
    Create a branch.

    `business_day_start_time` has no default on purpose. The operating-day
    cutoff is an unanswered business question (ADR-008), and a default here
    would quietly become the answer.
    """
    branch = Branch(
        organization=organization,
        code=code.strip().upper(),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        business_day_start_time=business_day_start_time,
        timezone=timezone,
    )
    branch.full_clean()
    branch.save()
    return branch


@transaction.atomic
def grant_branch_access(*, user: User, branch: Branch, role: Role | str) -> BranchMembership:
    """
    Give a user access to a branch in a role.

    Re-granting an existing membership updates the role and reactivates it
    rather than failing, so repairing access is not a delete-and-recreate that
    would lose the original creation timestamp.
    """
    membership, created = BranchMembership.objects.get_or_create(
        user=user,
        branch=branch,
        defaults={"role": role, "is_active": True},
    )
    if not created:
        membership.role = role
        membership.is_active = True
        membership.full_clean()
        membership.save(update_fields=["role", "is_active", "updated_at"])
    return membership


@transaction.atomic
def revoke_branch_access(*, user: User, branch: Branch) -> None:
    """
    Withdraw access without deleting the row.

    Deleting would erase the record that this person once held this post,
    which is exactly what an audit needs to see.
    """
    BranchMembership.objects.filter(user=user, branch=branch, is_active=True).update(
        is_active=False
    )


@transaction.atomic
def deactivate_branch(*, branch: Branch) -> Branch:
    """Close a branch to further activity, preserving its history."""
    branch.is_active = False
    branch.save(update_fields=["is_active", "updated_at"])
    return branch
