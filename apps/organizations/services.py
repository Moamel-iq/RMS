"""
Organization and branch commands.

Every state change goes through here. Models stay free of business logic, and
callers cannot construct a half-valid row by writing fields directly.
"""

from __future__ import annotations

from datetime import time

from django.conf import settings
from django.db import transaction

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.organizations.models import (
    Branch,
    BranchMembership,
    Organization,
    OrganizationMembership,
    Role,
)
from apps.organizations.permissions import sync_user_role_groups
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
    record_audit_event(
        action=AuditAction.CREATED, target=organization, new_state=snapshot(organization)
    )
    return organization


@transaction.atomic
def update_organization(
    *, organization: Organization, name_ar: str, name_en: str, is_active: bool
) -> Organization:
    """
    Rename or deactivate an organization.

    The code is not editable: it appears in document numbering and reports,
    and changing it would silently rewrite what historic documents claim to
    belong to.
    """
    # Re-read from the database. A ModelForm mutates its instance in place
    # during validation, so an in-memory snapshot taken here would already
    # hold the NEW values and the audit trail would record before == after.
    before = snapshot(Organization.objects.get(pk=organization.pk))
    organization.name_ar = name_ar.strip()
    organization.name_en = name_en.strip()
    organization.is_active = is_active
    organization.full_clean()
    organization.save(update_fields=["name_ar", "name_en", "is_active", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED if is_active else AuditAction.DEACTIVATED,
        target=organization,
        previous_state=before,
        new_state=snapshot(organization),
    )
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
    record_audit_event(
        action=AuditAction.CREATED, target=branch, branch=branch, new_state=snapshot(branch)
    )
    return branch


@transaction.atomic
def update_branch(
    *,
    branch: Branch,
    name_ar: str,
    name_en: str,
    business_day_start_time: time,
    timezone: str,
    is_active: bool,
) -> Branch:
    """
    Update a branch.

    Changing the timezone or the operating-day cutoff restates the business
    date of everything already recorded against this branch, so the previous
    values are captured in the audit event and in row history. Once the ledger
    is live this needs a controlled process, not a form (ADR-008).
    """
    # Re-read: see the note in update_organization.
    before = snapshot(Branch.objects.get(pk=branch.pk))
    branch.name_ar = name_ar.strip()
    branch.name_en = name_en.strip()
    branch.business_day_start_time = business_day_start_time
    branch.timezone = timezone
    branch.is_active = is_active
    branch.full_clean()
    branch.save()

    cutoff_changed = (
        before["business_day_start_time"] != snapshot(branch)["business_day_start_time"]
        or before["timezone"] != branch.timezone
    )
    record_audit_event(
        action=AuditAction.UPDATED if is_active else AuditAction.DEACTIVATED,
        target=branch,
        branch=branch,
        previous_state=before,
        new_state=snapshot(branch),
        reason="operating day changed" if cutoff_changed else "",
    )
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

    # The role's permissions follow the membership. Recomputed from every
    # membership the user holds, so re-granting a role they already have
    # elsewhere cannot leave them with a stale permission set.
    sync_user_role_groups(user)

    record_audit_event(
        action=AuditAction.ACCESS_GRANTED,
        target=membership,
        branch=branch,
        new_state=snapshot(membership),
        reason=f"{user} granted {role} at {branch.code}",
    )
    return membership


@transaction.atomic
def revoke_branch_access(*, user: User, branch: Branch) -> None:
    """
    Withdraw access without deleting the row.

    Deleting would erase the record that this person once held this post,
    which is exactly what an audit needs to see.
    """
    memberships = list(BranchMembership.objects.filter(user=user, branch=branch, is_active=True))
    if not memberships:
        return

    BranchMembership.objects.filter(user=user, branch=branch, is_active=True).update(
        is_active=False
    )
    sync_user_role_groups(user)
    for membership in memberships:
        record_audit_event(
            action=AuditAction.ACCESS_REVOKED,
            target=membership,
            branch=branch,
            previous_state=snapshot(membership),
            reason=f"{user} revoked at {branch.code}",
        )


@transaction.atomic
def grant_organization_access(
    *, user: User, organization: Organization, role: Role | str
) -> OrganizationMembership:
    """
    Give a user authority across a whole organization, in a role.

    Deliberately separate from `grant_branch_access` rather than a flag on it.
    Organization authority reaches state that has no branch — a fiscal period
    covers every branch at once — so granting it is a different decision, made
    by a different person, and it should read that way at the call site.
    """
    membership, created = OrganizationMembership.objects.get_or_create(
        user=user,
        organization=organization,
        defaults={"role": role, "is_active": True},
    )
    if not created:
        before = snapshot(OrganizationMembership.objects.get(pk=membership.pk))
        membership.role = role
        membership.is_active = True
        membership.full_clean()
        membership.save(update_fields=["role", "is_active", "updated_at"])
    else:
        before = None

    sync_user_role_groups(user)

    record_audit_event(
        action=AuditAction.ACCESS_GRANTED,
        target=membership,
        previous_state=before,
        new_state=snapshot(membership),
        reason=f"{user} granted {role} across {organization.code}",
    )
    return membership


@transaction.atomic
def revoke_organization_access(*, user: User, organization: Organization) -> None:
    """
    Withdraw organization-wide authority without deleting the row.

    The row is what tells an auditor who was able to reopen a period last
    March. Deleting it would erase exactly the fact they came to check.
    """
    memberships = list(
        OrganizationMembership.objects.filter(user=user, organization=organization, is_active=True)
    )
    if not memberships:
        return

    OrganizationMembership.objects.filter(
        user=user, organization=organization, is_active=True
    ).update(is_active=False)
    sync_user_role_groups(user)

    for membership in memberships:
        record_audit_event(
            action=AuditAction.ACCESS_REVOKED,
            target=membership,
            previous_state=snapshot(membership),
            reason=f"{user} revoked across {organization.code}",
        )


@transaction.atomic
def deactivate_branch(*, branch: Branch) -> Branch:
    """Close a branch to further activity, preserving its history."""
    branch.is_active = False
    branch.save(update_fields=["is_active", "updated_at"])
    return branch
