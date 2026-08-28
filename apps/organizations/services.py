"""
Organization and branch commands.

Every state change goes through here. Models stay free of business logic, and
callers cannot construct a half-valid row by writing fields directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import time
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.context import audit_context, get_correlation_id
from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.organizations.authorization import require_organization_permission
from apps.organizations.models import (
    AccessChangeAction,
    AccessChangeRequest,
    AccessChangeRequestStatus,
    Branch,
    BranchMembership,
    Organization,
    OrganizationMembership,
    Role,
    RoleDefinition,
)
from apps.organizations.permissions import sync_user_role_groups
from apps.organizations.roles import (
    resolve_permissions,
    sync_role_definition_group,
    validate_role_key,
)
from apps.users.models import User


def _require_access_administrator(
    *, actor: User | None, target: User, organization: Organization, role: Role | str | None
) -> None:
    """Refuse an administrative access change that could escalate authority.

    ``actor=None`` is retained solely for trusted bootstrap, fixture, and data
    import code that predates the web administration surface.  Every request
    path passes an actor and is therefore checked here as well as in its view.
    """
    if actor is None:
        return

    from apps.organizations.security_permissions import MANAGE_ACCESS

    require_organization_permission(actor, MANAGE_ACCESS, organization)
    if actor.pk == target.pk:
        raise ValidationError(
            _("لا يجوز للمستخدم منح نفسه صلاحية أو سحبها."), code="self_access_change"
        )
    if target.is_staff or target.is_superuser:
        raise ValidationError(
            _("لا تُدار الحسابات الإدارية أو فائقة الصلاحية من هذه الشاشة."),
            code="privileged_target",
        )
    if role is not None and str(role) == Role.OWNER:
        raise ValidationError(
            _("منح دور المالك يحتاج طلبًا واعتمادًا مستقلين ولا يتم مباشرة."),
            code="owner_requires_approval",
        )
    # A current owner is equal to the actor's highest ordinary authority in
    # this organization.  Do not let one owner silently amend or remove
    # another owner's access; the maker-checker workflow will handle that.
    if (
        OrganizationMembership.objects.filter(
            user=target, organization=organization, role=Role.OWNER, is_active=True
        ).exists()
        or BranchMembership.objects.filter(
            user=target,
            branch__organization=organization,
            role=Role.OWNER,
            is_active=True,
        ).exists()
    ):
        raise ValidationError(
            _("لا يمكن تعديل صلاحية مالك قائم من هذا المسار."),
            code="equal_or_higher_authority_target",
        )


def _require_access_change_requester(
    *, actor: User, target: User, organization: Organization, branch: Branch | None = None
) -> None:
    """Validate the maker before an access change is even proposed."""
    from apps.organizations.security_permissions import MANAGE_ACCESS

    require_organization_permission(actor, MANAGE_ACCESS, organization)
    if actor.pk == target.pk:
        raise ValidationError(
            _("لا يجوز للمستخدم طلب تغيير صلاحيته بنفسه."), code="self_access_change"
        )
    if target.is_staff or target.is_superuser:
        raise ValidationError(
            _("لا تُدار الحسابات الإدارية أو فائقة الصلاحية من هذه الشاشة."),
            code="privileged_target",
        )
    if branch is not None and branch.organization_id != organization.pk:
        raise ValidationError(
            _("الفرع لا يتبع المؤسسة المحددة."), code="branch_organization_mismatch"
        )


def _require_role_administrator(*, actor: User | None, organization: Organization) -> None:
    """Apply organization-scoped role-management authority when an actor is known."""
    if actor is None:
        return
    from apps.organizations.security_permissions import MANAGE_ROLES

    require_organization_permission(actor, MANAGE_ROLES, organization)


def _require_org_settings_administrator(*, actor: User | None, organization: Organization) -> None:
    """Apply organization-scoped organization/branch configuration authority."""
    if actor is None:
        return
    from apps.organizations.security_permissions import MANAGE_ORG_SETTINGS

    require_organization_permission(actor, MANAGE_ORG_SETTINGS, organization)


if TYPE_CHECKING:
    # Imported for typing only: `apps.inventory` depends on this module, so a
    # runtime import here would close the cycle.
    from apps.inventory.models import Warehouse


@transaction.atomic
def create_organization(*, code: str, name: str) -> Organization:
    organization = Organization(
        code=code.strip().upper(),
        name=name.strip(),
    )
    organization.full_clean()
    organization.save()
    record_audit_event(
        action=AuditAction.CREATED, target=organization, new_state=snapshot(organization)
    )
    return organization


@transaction.atomic
def update_organization(
    *,
    organization: Organization,
    name: str,
    is_active: bool,
    actor: User | None = None,
) -> Organization:
    """
    Rename or deactivate an organization.

    The code is not editable: it appears in document numbering and reports,
    and changing it would silently rewrite what historic documents claim to
    belong to.
    """
    _require_org_settings_administrator(actor=actor, organization=organization)
    # Re-read from the database. A ModelForm mutates its instance in place
    # during validation, so an in-memory snapshot taken here would already
    # hold the NEW values and the audit trail would record before == after.
    before = snapshot(Organization.objects.get(pk=organization.pk))
    organization.name = name.strip()
    organization.is_active = is_active
    organization.full_clean()
    organization.save(update_fields=["name", "is_active", "updated_at"])
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
    name: str,
    business_day_start_time: time,
    timezone: str = settings.TIME_ZONE,
    actor: User | None = None,
) -> Branch:
    """
    Create a branch.

    `business_day_start_time` has no default on purpose. The operating-day
    cutoff is an unanswered business question (ADR-008), and a default here
    would quietly become the answer.
    """
    _require_org_settings_administrator(actor=actor, organization=organization)
    branch = Branch(
        organization=organization,
        code=code.strip().upper(),
        name=name.strip(),
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
    name: str,
    business_day_start_time: time,
    timezone: str,
    is_active: bool,
    actor: User | None = None,
) -> Branch:
    """
    Update a branch.

    Changing the timezone or the operating-day cutoff restates the business
    date of everything already recorded against this branch, so the previous
    values are captured in the audit event and in row history. Once the ledger
    is live this needs a controlled process, not a form (ADR-008).
    """
    _require_org_settings_administrator(actor=actor, organization=branch.organization)
    # Re-read: see the note in update_organization.
    before = snapshot(Branch.objects.get(pk=branch.pk))
    branch.name = name.strip()
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
def _apply_branch_access(*, user: User, branch: Branch, role: Role | str) -> BranchMembership:
    """
    Give a user access to a branch in a role.

    Re-granting an existing membership updates the role and reactivates it
    rather than failing, so repairing access is not a delete-and-recreate that
    would lose the original creation timestamp.
    """
    # A built-in post, or a post this branch's organization defined (ADR-034).
    role = validate_role_key(role, branch.organization)
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
def _apply_branch_access_revocation(*, user: User, branch: Branch) -> None:
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


def grant_branch_access(
    *, user: User, branch: Branch, role: Role | str, actor: User | None = None
) -> BranchMembership:
    """Trusted bootstrap helper; interactive changes must use a request.

    Keeping the actor-less path supports fixtures and one-time migrations.  A
    browser actor is never allowed to grant access directly, even to an
    ordinary role: it must create an :class:`AccessChangeRequest` and another
    authorized person must approve it.
    """
    if actor is not None:
        _require_access_change_requester(
            actor=actor, target=user, organization=branch.organization, branch=branch
        )
        raise ValidationError(
            _("تغيير الصلاحية يحتاج طلباً واعتماداً من مستخدم مستقل."),
            code="access_change_requires_approval",
        )
    return _apply_branch_access(user=user, branch=branch, role=role)


def revoke_branch_access(*, user: User, branch: Branch, actor: User | None = None) -> None:
    """Trusted bootstrap helper; see :func:`grant_branch_access`."""
    if actor is not None:
        _require_access_change_requester(
            actor=actor, target=user, organization=branch.organization, branch=branch
        )
        raise ValidationError(
            _("سحب الصلاحية يحتاج طلباً واعتماداً من مستخدم مستقل."),
            code="access_change_requires_approval",
        )
    _apply_branch_access_revocation(user=user, branch=branch)


@transaction.atomic
def set_membership_warehouse_scope(
    *,
    membership: BranchMembership,
    mode: str,
    warehouses: Sequence[Warehouse] | None = None,
) -> BranchMembership:
    """
    Narrow a branch membership to particular warehouses, or widen it again.

    `ALL` is stored as a mode rather than expanded into rows on purpose: a
    membership granted "all warehouses" must cover the one that opens next
    month, and a snapshot of today's warehouses would silently fail to.

    A selected warehouse must belong to the membership's own branch. Allowing
    otherwise would let warehouse scope *widen* branch access, which is the
    exact inversion this model exists to prevent.
    """
    from apps.organizations.models import BranchMembershipWarehouse, WarehouseScopeMode

    before = snapshot(BranchMembership.objects.get(pk=membership.pk))

    if mode == WarehouseScopeMode.SELECTED:
        chosen = list(warehouses or [])
        if not chosen:
            raise ValidationError(
                _("Selected scope needs at least one warehouse."),
                code="no_warehouse_selected",
            )
        for warehouse in chosen:
            if warehouse.branch_id != membership.branch_id:
                raise ValidationError(
                    _("Warehouse %(code)s belongs to another branch."),
                    code="warehouse_branch_mismatch",
                    params={"code": warehouse.code},
                )
        BranchMembershipWarehouse.objects.filter(branch_membership=membership).delete()
        BranchMembershipWarehouse.objects.bulk_create(
            [
                BranchMembershipWarehouse(branch_membership=membership, warehouse=warehouse)
                for warehouse in chosen
            ]
        )

    membership.warehouse_scope_mode = mode
    membership.full_clean()
    membership.save(update_fields=["warehouse_scope_mode", "updated_at"])

    record_audit_event(
        action=AuditAction.ACCESS_GRANTED,
        target=membership,
        branch=membership.branch,
        previous_state=before,
        new_state=snapshot(membership),
        reason=f"warehouse scope set to {mode}",
    )
    return membership


@transaction.atomic
def _apply_organization_access(
    *, user: User, organization: Organization, role: Role | str
) -> OrganizationMembership:
    """
    Give a user authority across a whole organization, in a role.

    Deliberately separate from `grant_branch_access` rather than a flag on it.
    Organization authority reaches state that has no branch — a fiscal period
    covers every branch at once — so granting it is a different decision, made
    by a different person, and it should read that way at the call site.
    """
    role = validate_role_key(role, organization)
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
def _apply_organization_access_revocation(*, user: User, organization: Organization) -> None:
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


def grant_organization_access(
    *, user: User, organization: Organization, role: Role | str, actor: User | None = None
) -> OrganizationMembership:
    """Trusted bootstrap helper; interactive changes use maker-checker."""
    if actor is not None:
        _require_access_change_requester(actor=actor, target=user, organization=organization)
        raise ValidationError(
            _("تغيير الصلاحية يحتاج طلباً واعتماداً من مستخدم مستقل."),
            code="access_change_requires_approval",
        )
    return _apply_organization_access(user=user, organization=organization, role=role)


def revoke_organization_access(
    *, user: User, organization: Organization, actor: User | None = None
) -> None:
    """Trusted bootstrap helper; interactive changes use maker-checker."""
    if actor is not None:
        _require_access_change_requester(actor=actor, target=user, organization=organization)
        raise ValidationError(
            _("سحب الصلاحية يحتاج طلباً واعتماداً من مستخدم مستقل."),
            code="access_change_requires_approval",
        )
    _apply_organization_access_revocation(user=user, organization=organization)


def _current_access_snapshot(
    *, user: User, organization: Organization, branch: Branch | None
) -> dict[str, object]:
    """Capture current active authority before a reviewer can change it."""
    membership = (
        BranchMembership.objects.filter(user=user, branch=branch, is_active=True).first()
        if branch is not None
        else OrganizationMembership.objects.filter(
            user=user, organization=organization, is_active=True
        ).first()
    )
    return snapshot(membership) if membership is not None else {}


@transaction.atomic
def request_access_change(
    *,
    actor: User,
    target_user: User,
    organization: Organization,
    action: AccessChangeAction | str,
    reason: str,
    branch: Branch | None = None,
    requested_role: Role | str = "",
) -> AccessChangeRequest:
    """Create, but never apply, an organization or branch access change."""
    _require_access_change_requester(
        actor=actor, target=target_user, organization=organization, branch=branch
    )
    action = AccessChangeAction(action)
    reason = reason.strip()
    if not reason:
        raise ValidationError(_("طلب تغيير الصلاحية يحتاج سبباً."), code="reason_required")

    role = ""
    if action == AccessChangeAction.GRANT:
        role = validate_role_key(requested_role, organization)
        # Owners govern an organization, never an arbitrary individual branch.
        if role == Role.OWNER and branch is not None:
            raise ValidationError(
                _("دور المالك يُطلب على مستوى المؤسسة وليس فرعاً منفرداً."),
                code="owner_requires_organization_scope",
            )

    request = AccessChangeRequest(
        organization=organization,
        branch=branch,
        target_user=target_user,
        action=action,
        requested_role=role,
        previous_access=_current_access_snapshot(
            user=target_user, organization=organization, branch=branch
        ),
        reason=reason,
        requested_by=actor,
    )
    request.full_clean()
    request.save()
    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        record_audit_event(
            action=AuditAction.SUBMITTED,
            target=request,
            branch=branch,
            organization=organization,
            new_state=snapshot(request),
            reason=reason,
            metadata={"access_action": action, "requested_role": role},
        )
    return request


@transaction.atomic
def decide_access_change(
    *, request: AccessChangeRequest, actor: User, approve: bool, reason: str = ""
) -> AccessChangeRequest:
    """Independently approve or reject a pending access request.

    The database row is locked before its status is checked, so two reviewers
    cannot approve the same request and write competing access histories.
    """
    request = (
        AccessChangeRequest.objects.select_for_update(of=("self",))
        .select_related("organization", "branch", "target_user", "requested_by")
        .get(pk=request.pk)
    )
    if request.status != AccessChangeRequestStatus.PENDING:
        raise ValidationError(_("تم اتخاذ قرار في هذا الطلب بالفعل."), code="request_not_pending")
    _require_access_change_requester(
        actor=actor,
        target=request.target_user,
        organization=request.organization,
        branch=request.branch,
    )
    if actor.pk in {request.requested_by_id, request.target_user_id}:
        raise ValidationError(
            _("لا يجوز لمنشئ الطلب أو المستفيد منه اعتماده أو رفضه."),
            code="maker_cannot_check",
        )
    reason = reason.strip()
    if not approve and not reason:
        raise ValidationError(_("رفض الطلب يحتاج سبباً."), code="decision_reason_required")

    before = snapshot(request)
    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        if approve:
            # The stubs type the nullable FK as ``Branch | None`` and cannot see
            # that ``branch_id`` has already decided the scope: the branch calls
            # below are only reached for a request that carries one.
            if request.action == AccessChangeAction.GRANT:
                if request.branch_id:
                    _apply_branch_access(
                        user=request.target_user,
                        branch=request.branch,  # type: ignore[arg-type]
                        role=request.requested_role,
                    )
                else:
                    _apply_organization_access(
                        user=request.target_user,
                        organization=request.organization,
                        role=request.requested_role,
                    )
            elif request.branch_id:
                _apply_branch_access_revocation(user=request.target_user, branch=request.branch)  # type: ignore[arg-type]
            else:
                _apply_organization_access_revocation(
                    user=request.target_user, organization=request.organization
                )

        request.status = (
            AccessChangeRequestStatus.APPROVED if approve else AccessChangeRequestStatus.REJECTED
        )
        request.reviewed_by = actor
        request.decided_at = timezone.now()
        request.decision_reason = reason
        request.full_clean()
        request.save(
            update_fields=["status", "reviewed_by", "decided_at", "decision_reason", "updated_at"]
        )
        record_audit_event(
            action=AuditAction.APPROVED if approve else AuditAction.REJECTED,
            target=request,
            branch=request.branch,
            organization=request.organization,
            previous_state=before,
            new_state=snapshot(request),
            reason=reason,
            metadata={
                "access_action": request.action,
                "requested_role": request.requested_role,
            },
        )
    return request


@transaction.atomic
def cancel_access_change(
    *, request: AccessChangeRequest, actor: User, reason: str
) -> AccessChangeRequest:
    """Let only the maker withdraw a still-pending request, with a record."""
    request = AccessChangeRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != AccessChangeRequestStatus.PENDING:
        raise ValidationError(_("لا يمكن إلغاء طلب تم اتخاذ قرار فيه."), code="request_not_pending")
    if request.requested_by_id != actor.pk:
        raise ValidationError(_("منشئ الطلب فقط يستطيع إلغاءه."), code="not_request_maker")
    reason = reason.strip()
    if not reason:
        raise ValidationError(_("إلغاء الطلب يحتاج سبباً."), code="reason_required")
    before = snapshot(request)
    with audit_context(actor=actor, correlation_id=get_correlation_id()):
        request.status = AccessChangeRequestStatus.CANCELLED
        request.decided_at = timezone.now()
        request.decision_reason = reason
        request.full_clean()
        request.save(update_fields=["status", "decided_at", "decision_reason", "updated_at"])
        record_audit_event(
            action=AuditAction.CANCELLED,
            target=request,
            branch=request.branch,
            organization=request.organization,
            previous_state=before,
            new_state=snapshot(request),
            reason=reason,
            metadata={"access_action": request.action, "requested_role": request.requested_role},
        )
    return request


@transaction.atomic
def deactivate_branch(*, branch: Branch) -> Branch:
    """Close a branch to further activity, preserving its history."""
    branch.is_active = False
    branch.save(update_fields=["is_active", "updated_at"])
    return branch


# ---------------------------------------------------------------------------
# Custom roles (ADR-034)
# ---------------------------------------------------------------------------
#
# A definition is a post the organization invented: a name and a set of
# permissions. Granting it is `grant_branch_access` / `grant_organization_access`
# with the definition's key, exactly as for a built-in post, so nothing below
# touches memberships. What lives here is the definition's own lifecycle, and
# the one rule that keeps it honest: its group is rewritten from its
# permissions every time they change, so the group can never say more or less
# than the screen the owner saved.


def _permission_names(definition: RoleDefinition) -> list[str]:
    return sorted(
        f"{permission.content_type.app_label}.{permission.codename}"
        for permission in definition.permissions.select_related("content_type")
    )


@transaction.atomic
def create_role_definition(
    *,
    organization: Organization,
    code: str,
    name: str,
    permissions: Sequence[str],
    description: str = "",
    based_on: Role | str = "",
    actor: User | None = None,
) -> RoleDefinition:
    """
    Define a post for one organization.

    `permissions` are `app_label.codename` strings. Every one must exist and
    belong to a module the owner may configure; an unknown name refuses the
    whole call rather than being dropped, because a role saved with fewer
    permissions than the owner ticked is a silent narrowing nobody asked for.
    """
    _require_role_administrator(actor=actor, organization=organization)
    rows = resolve_permissions(permissions)
    definition = RoleDefinition(
        organization=organization,
        code=code.strip().lower(),
        name=name.strip(),
        description=description.strip(),
        based_on=based_on.value if isinstance(based_on, Role) else str(based_on),
    )
    definition.full_clean()
    definition.save()
    definition.permissions.set(rows)
    sync_role_definition_group(definition)
    record_audit_event(
        action=AuditAction.CREATED,
        target=definition,
        new_state=snapshot(definition),
        reason=f"role {definition.key} defined with {len(rows)} permissions",
        metadata={"permissions": _permission_names(definition)},
    )
    return definition


@transaction.atomic
def update_role_definition(
    *,
    definition: RoleDefinition,
    name: str | None = None,
    description: str | None = None,
    permissions: Sequence[str] | None = None,
    actor: User | None = None,
) -> RoleDefinition:
    """
    Change a post's name or permissions. The code is fixed: it is part of
    every membership key and every group name that already names this post.

    A permission change reaches everyone holding the post at once — the group
    is theirs — which is the point of a post rather than per-user grants, and
    the reason the change is audited with the before and after sets.
    """
    _require_role_administrator(actor=actor, organization=definition.organization)
    before = snapshot(RoleDefinition.objects.get(pk=definition.pk))
    before_permissions = _permission_names(definition)

    if name is not None:
        definition.name = name.strip()
    if description is not None:
        definition.description = description.strip()
    definition.full_clean()
    definition.save(update_fields=["name", "description", "updated_at"])

    if permissions is not None:
        definition.permissions.set(resolve_permissions(permissions))
        sync_role_definition_group(definition)

    record_audit_event(
        action=AuditAction.UPDATED,
        target=definition,
        previous_state=before,
        new_state=snapshot(definition),
        reason=f"role {definition.key} updated",
        metadata={"before": before_permissions, "after": _permission_names(definition)},
    )
    return definition


def role_definition_member_count(definition: RoleDefinition) -> int:
    """Active memberships, at branches or over the organization, holding this post."""
    key = definition.key
    return (
        BranchMembership.objects.filter(role=key, is_active=True).count()
        + OrganizationMembership.objects.filter(role=key, is_active=True).count()
    )


@transaction.atomic
def archive_role_definition(
    *, definition: RoleDefinition, reason: str, actor: User | None = None
) -> RoleDefinition:
    """
    Retire a post. Refused while anyone still holds it.

    Archiving must not be a way to strip authority from people without a
    record per person: each membership is revoked first, through the revoke
    services that audit it, and only then does the post go.
    """
    _require_role_administrator(actor=actor, organization=definition.organization)
    held_by = role_definition_member_count(definition)
    if held_by:
        raise ValidationError(
            _("لا يمكن أرشفة الدور وهو ممنوح لـ %(count)d مستخدم؛ اسحب الصلاحيات أولاً."),
            code="role_in_use",
            params={"count": held_by},
        )
    if not reason.strip():
        raise ValidationError(_("الأرشفة تحتاج سبباً."), code="reason_required")

    before = snapshot(RoleDefinition.objects.get(pk=definition.pk))
    definition.is_active = False
    definition.save(update_fields=["is_active", "updated_at"])
    record_audit_event(
        action=AuditAction.DEACTIVATED,
        target=definition,
        previous_state=before,
        new_state=snapshot(definition),
        reason=reason.strip(),
    )
    return definition


@transaction.atomic
def reactivate_role_definition(
    *, definition: RoleDefinition, reason: str, actor: User | None = None
) -> RoleDefinition:
    """Bring a retired post back, with its permissions exactly as they were left."""
    _require_role_administrator(actor=actor, organization=definition.organization)
    if not reason.strip():
        raise ValidationError(_("إعادة التفعيل تحتاج سبباً."), code="reason_required")
    before = snapshot(RoleDefinition.objects.get(pk=definition.pk))
    definition.is_active = True
    definition.save(update_fields=["is_active", "updated_at"])
    sync_role_definition_group(definition)
    record_audit_event(
        action=AuditAction.UPDATED,
        target=definition,
        previous_state=before,
        new_state=snapshot(definition),
        reason=reason.strip(),
    )
    return definition
