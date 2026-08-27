"""
Permission *and* place. Neither alone is authorization.

`user.has_perm("accounting.post_journal")` says the person may post; it does
not say where. Every check here pairs a Django permission with a scope
resolved from the caller's own memberships, so an identifier submitted in a
request can only ever select from what the caller already reaches — it can
never add to it.

The rule this module exists to enforce:

    A user authorized for one organization or branch must not gain access
    merely by submitting another organization_id or branch_id.

That is why `resolve_organization` and `resolve_branch` take an id and a user
together. Fetching the object first and checking afterwards is the shape of
the bug: it invites a code path that fetches, uses, and forgets to check.

**Provenance.** A Django permission is global: `user.has_perm(...)` answers
"anywhere at all", because role groups are recomputed from *every* membership
a user holds. Pairing that global answer with mere reach to the target is not
enough — a manager in one organization who also holds a viewer post in another
would satisfy both halves in the second organization and inherit the first
one's authority there. So the two halves must come from the **same place**:
the permission has to be carried by a role the user holds *inside the target
organization, branch, or warehouse*. See `roles_granting`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.organizations.models import (
    Branch,
    BranchMembership,
    Organization,
    OrganizationMembership,
    WarehouseScopeMode,
)
from apps.organizations.selectors import accessible_branches, can_access_branch
from apps.users.models import User

if TYPE_CHECKING:
    from apps.inventory.models import Warehouse


class OutOfScope(ObjectDoesNotExist):
    """
    The caller named something outside their organization or branch scope.

    Deliberately an `ObjectDoesNotExist`, so the API answers **404**. Outside
    the caller's tenancy, a record does not exist as far as they are
    concerned, and saying "403" about it confirms that it is real — which
    turns any id-guessing loop into a census of another organization's
    documents, invoice numbers, and account ids.

    The messages here never say *why* something is unreachable. A missing
    record and a record belonging to someone else must be indistinguishable,
    or the status code is the only thing that was fixed.
    """


class PermissionMissing(PermissionDenied):
    """
    The caller reaches the object but may not perform this act on it.

    **403**, and the honest answer: they can see this journal, they simply may
    not post it. Nothing is disclosed that they were not already entitled to.
    """


def organization_scope(user: User) -> list[int]:
    """
    Primary keys of organizations where this user holds organization-wide
    authority.

    Branch memberships deliberately do not appear. Holding a role at every
    branch is not the same as holding it over the organization — see
    `OrganizationMembership`.
    """
    if not user.is_authenticated or not user.is_active:
        return []

    if user.is_superuser:
        return list(Organization.objects.filter(is_active=True).values_list("id", flat=True))

    return list(
        OrganizationMembership.objects.filter(
            user=user, is_active=True, organization__is_active=True
        ).values_list("organization_id", flat=True)
    )


def has_organization_scope(user: User, organization: Organization) -> bool:
    """Whether this user holds organization-wide authority here."""
    return organization.pk in organization_scope(user)


# ---------------------------------------------------------------------------
# Provenance — which role carried the permission, and where it was held
# ---------------------------------------------------------------------------


def roles_granting(permission: str) -> set[str]:
    """
    The roles that carry this permission, read from the role groups themselves.

    Derived rather than declared. Each module writes its own role map into the
    `role:` groups (`apps.accounting.permissions.sync_role_groups` and the
    inventory equivalent), so reading the groups back keeps this one function
    correct as modules are added — and keeps `apps.organizations` from having
    to import every module that might grant something.

    It is also the same source `user.has_perm` reads, restricted to the `role:`
    namespace. A permission that no role carries authorizes nobody: a hand-made
    group, or a permission attached directly to a user, grants no authority
    over any organization, because it names no post that anybody holds and
    therefore says nothing about *where* it applies.
    """
    from django.contrib.auth.models import Group

    from apps.organizations.permissions import ROLE_GROUP_PREFIX

    app_label, _sep, codename = permission.partition(".")
    return {
        name.removeprefix(ROLE_GROUP_PREFIX)
        for name in Group.objects.filter(
            name__startswith=ROLE_GROUP_PREFIX,
            permissions__codename=codename,
            permissions__content_type__app_label=app_label,
        ).values_list("name", flat=True)
    }


def _active(user: User) -> bool:
    return bool(user.is_authenticated and user.is_active)


def organization_authority_roles(user: User, organization: Organization) -> set[str]:
    """
    Roles held **over** this organization — `OrganizationMembership` only.

    Branch posts are deliberately excluded. Organization authority reaches
    state that has no branch, so it must be granted by a post that has no
    branch either.
    """
    if not _active(user):
        return set()
    return set(
        OrganizationMembership.objects.filter(
            user=user,
            organization=organization,
            is_active=True,
            organization__is_active=True,
        ).values_list("role", flat=True)
    )


def roles_in_organization(user: User, organization: Organization) -> set[str]:
    """
    Every role held **inside** this organization — over it, or at one of its
    branches.

    The provenance set for organization-owned master data: a branch manager
    maintains the shared item master from the post they actually hold here,
    and a post held somewhere else contributes nothing.
    """
    if not _active(user):
        return set()
    branch_roles = BranchMembership.objects.filter(
        user=user,
        is_active=True,
        branch__is_active=True,
        branch__organization=organization,
        branch__organization__is_active=True,
    ).values_list("role", flat=True)
    return organization_authority_roles(user, organization) | set(branch_roles)


def roles_at_branch(user: User, branch: Branch) -> set[str]:
    """
    Roles held at this branch, plus organization-wide roles over its owner.

    The second half matches `accessible_branches`: organization authority
    reaches every branch it owns, so the role that grants that authority is a
    role held at each of them.
    """
    if not _active(user):
        return set()
    branch_roles = BranchMembership.objects.filter(
        user=user,
        branch=branch,
        is_active=True,
        branch__is_active=True,
        branch__organization__is_active=True,
    ).values_list("role", flat=True)
    organization_roles = OrganizationMembership.objects.filter(
        user=user,
        organization_id=branch.organization_id,
        is_active=True,
        organization__is_active=True,
    ).values_list("role", flat=True)
    return set(branch_roles) | set(organization_roles)


def roles_at_warehouse(user: User, warehouse: Warehouse) -> set[str]:
    """
    Roles held by a membership that actually covers this warehouse.

    A `SELECTED` membership that does not list this warehouse contributes no
    role here, so narrowing custody narrows authority with it rather than
    leaving the permission behind.
    """
    if not _active(user):
        return set()
    branch_roles = (
        BranchMembership.objects.filter(
            user=user,
            branch_id=warehouse.branch_id,
            is_active=True,
            branch__is_active=True,
            branch__organization__is_active=True,
        )
        .filter(
            Q(warehouse_scope_mode=WarehouseScopeMode.ALL)
            | Q(
                warehouse_scope_mode=WarehouseScopeMode.SELECTED,
                warehouse_scopes__warehouse=warehouse,
            )
        )
        .values_list("role", flat=True)
    )
    organization_roles = OrganizationMembership.objects.filter(
        user=user,
        organization_id=warehouse.branch.organization_id,
        is_active=True,
        organization__is_active=True,
    ).values_list("role", flat=True)
    return set(branch_roles) | set(organization_roles)


def _carried_by(roles: set[str], permission: str) -> bool:
    """Whether any role in this set carries the permission."""
    return bool(roles & roles_granting(permission))


def organizations_with_permission(user: User, permission: str) -> QuerySet[Organization]:
    """
    Organizations where a post the caller actually holds carries this
    permission — the bulk form of `has_organization_master_data_permission`.

    One query rather than one per row, so a screen can gate its buttons and
    fill its selectors without asking the same question repeatedly. It answers
    identically to the single-object check; a screen that showed a button the
    service would refuse is a worse bug than no button.
    """
    if not _active(user):
        return Organization.objects.none()

    base = Organization.objects.filter(is_active=True)
    if user.is_superuser:
        return base

    roles = roles_granting(permission)
    if not roles:
        return Organization.objects.none()

    return base.filter(
        Q(
            memberships__user=user,
            memberships__is_active=True,
            memberships__role__in=roles,
        )
        | Q(
            branches__memberships__user=user,
            branches__memberships__is_active=True,
            branches__memberships__role__in=roles,
            branches__is_active=True,
        )
    ).distinct()


def organizations_with_organization_permission(
    user: User, permission: str
) -> QuerySet[Organization]:
    """Organizations where ``user`` holds an organization-level permission.

    This is intentionally stricter than :func:`organizations_with_permission`:
    a branch post never authorizes changing users, roles, or organization-wide
    settings.  The query is the bulk equivalent of
    :func:`has_organization_permission`, so settings screens use the same
    scope rule as their command services.
    """
    if not _active(user):
        return Organization.objects.none()

    base = Organization.objects.filter(is_active=True)
    if user.is_superuser:
        return base

    roles = roles_granting(permission)
    if not roles:
        return Organization.objects.none()

    return base.filter(
        memberships__user=user,
        memberships__is_active=True,
        memberships__role__in=roles,
    ).distinct()


def branches_with_permission(user: User, permission: str) -> QuerySet[Branch]:
    """Branches where a post the caller holds carries this permission."""
    if not _active(user):
        return Branch.objects.none()

    reachable = accessible_branches(user)
    if user.is_superuser:
        return reachable

    roles = roles_granting(permission)
    if not roles:
        return Branch.objects.none()

    return reachable.filter(
        Q(
            memberships__user=user,
            memberships__is_active=True,
            memberships__role__in=roles,
        )
        | Q(
            organization__memberships__user=user,
            organization__memberships__is_active=True,
            organization__memberships__role__in=roles,
        )
    ).distinct()


def has_organization_permission(user: User, permission: str, organization: Organization) -> bool:
    """
    Both halves, from the same place: organization-wide standing here, and a
    post *here* that carries the permission.

    A superuser satisfies both — `organization_scope` returns every active
    organization, and emergency authority is not held through a membership, so
    the provenance check is short-circuited rather than failed. It is
    deliberately still not a bypass: the service the superuser reaches
    validates the reason, the ordering, and the audit actor exactly as it does
    for anyone else.
    """
    if not _active(user):
        return False
    if user.is_superuser:
        return True
    return has_organization_scope(user, organization) and _carried_by(
        organization_authority_roles(user, organization), permission
    )


def has_branch_permission(user: User, permission: str, branch: Branch) -> bool:
    """Both halves, from the same place: access to this branch, and a post at it."""
    if not _active(user):
        return False
    if user.is_superuser:
        return True
    return can_access_branch(user, branch) and _carried_by(
        roles_at_branch(user, branch), permission
    )


def require_organization_permission(
    user: User, permission: str, organization: Organization
) -> None:
    """
    Raise unless the user may exercise this permission over this organization.

    Two different failures, and the difference is *reachability*, not
    authority:

    * The caller cannot reach this organization at all — no membership in it,
      and no branch of it. It is not theirs to know about, so **404**.
    * The caller reaches it, through an organization or a branch membership,
      but lacks organization-level authority for this act. A branch
      accountant asking to close a period in their own organization is told
      **403** — the period is not foreign to them, they simply may not.

    Note that reaching an organization is deliberately weaker than holding
    organization *scope*. Reaching it only decides whether they may be told
    "no"; it never grants anything.
    """
    # Raises OutOfScope (404) when the organization is not reachable at all.
    resolve_organization(user, organization.pk)

    if not has_organization_permission(user, permission, organization):
        raise PermissionMissing(
            _("%(permission)s is not held over organization %(organization)s.")
            % {"permission": permission, "organization": organization.code}
        )


def require_reachable_organization_permission(
    user: User, permission: str, organization: Organization
) -> None:
    """
    Raise unless the user reaches this organization and holds this permission.

    **Weaker than `require_organization_permission` on purpose.** That one
    demands an `OrganizationMembership` and is for genuinely organization-level
    *acts* — closing a period, posting opening stock, overriding the
    negative-stock rule — where authority over one branch is authority over a
    part of something that has no parts.

    This one is for organization-owned **master data**: the item master, the
    categories, the packages, the conversions. A branch manager legitimately
    maintains those, and requiring them to also hold organization-wide
    authority would either lock them out or push every deployment into
    granting organization membership far too widely — which would quietly
    hand out period-closing authority as a side effect.

    Weaker in *scope*, not in provenance. The permission must still be carried
    by a post held **in this organization** — over it, or at one of its
    branches. Accepting a global `has_perm` here would have made a viewer post
    in one organization a channel for a manager post in another.
    """
    # Raises OutOfScope (404) when the organization is not reachable at all.
    resolve_organization(user, organization.pk)

    if not has_organization_master_data_permission(user, permission, organization):
        raise PermissionMissing(
            _("%(permission)s is not held in organization %(organization)s.")
            % {"permission": permission, "organization": organization.code}
        )


def has_organization_master_data_permission(
    user: User, permission: str, organization: Organization
) -> bool:
    """Whether a post held inside this organization carries this permission."""
    if not _active(user):
        return False
    if user.is_superuser:
        return True
    return _carried_by(roles_in_organization(user, organization), permission)


def require_branch_permission(user: User, permission: str, branch: Branch) -> None:
    """Raise unless the user may exercise this permission at this branch."""
    if not can_access_branch(user, branch):
        raise OutOfScope(_("Branch %(id)s does not exist.") % {"id": branch.pk})
    if not has_branch_permission(user, permission, branch):
        raise PermissionMissing(
            _("%(permission)s is not held at branch %(branch)s.")
            % {"permission": permission, "branch": branch.code}
        )


def accessible_warehouses(user: User) -> QuerySet[Warehouse]:
    """
    Active warehouses this user may act on.

    Reached three ways, and the third is the one that matters:

    1. A `BranchMembership` in `ALL` mode — every warehouse in that branch,
       **including ones created later**. That is why `ALL` is not expanded
       into rows at grant time.
    2. A `BranchMembership` in `SELECTED` mode — only the listed warehouses.
    3. Organization-wide authority — every warehouse in the organization,
       consistent with `accessible_branches`.

    A superuser reaches all of them, made explicit so it is testable.
    """
    from apps.inventory.models import Warehouse

    if not user.is_authenticated or not user.is_active:
        return Warehouse.objects.none()

    base = Warehouse.objects.filter(
        is_active=True,
        branch__is_active=True,
        branch__organization__is_active=True,
    )
    if user.is_superuser:
        return base

    return base.filter(
        # Organization-wide authority.
        Q(
            branch__organization__memberships__user=user,
            branch__organization__memberships__is_active=True,
        )
        # A branch membership covering the whole branch.
        | Q(
            branch__memberships__user=user,
            branch__memberships__is_active=True,
            branch__memberships__warehouse_scope_mode=WarehouseScopeMode.ALL,
        )
        # A branch membership restricted to specific warehouses.
        | Q(
            membership_scopes__branch_membership__user=user,
            membership_scopes__branch_membership__is_active=True,
            membership_scopes__branch_membership__warehouse_scope_mode=(
                WarehouseScopeMode.SELECTED
            ),
        )
    ).distinct()


def can_access_warehouse(user: User, warehouse: Warehouse) -> bool:
    """Whether this user may act on this specific warehouse."""
    return accessible_warehouses(user).filter(pk=warehouse.pk).exists()


def has_warehouse_permission(user: User, permission: str, warehouse: Warehouse) -> bool:
    """Both halves, from the same place: custody of this warehouse, and a post over it."""
    if not _active(user):
        return False
    if user.is_superuser:
        return True
    return can_access_warehouse(user, warehouse) and _carried_by(
        roles_at_warehouse(user, warehouse), permission
    )


def require_warehouse_permission(user: User, permission: str, warehouse: Warehouse) -> None:
    """
    Raise unless the user may exercise this permission at this warehouse.

    Same shape as the branch check: out of reach is a 404, reachable without
    the permission is a 403.
    """
    if not can_access_warehouse(user, warehouse):
        raise OutOfScope(_("Warehouse %(id)s does not exist.") % {"id": warehouse.pk})
    if not has_warehouse_permission(user, permission, warehouse):
        raise PermissionMissing(
            _("%(permission)s is not held at warehouse %(warehouse)s.")
            % {"permission": permission, "warehouse": warehouse.code}
        )


def resolve_warehouse(user: User, warehouse_id: int) -> Warehouse:
    """Turn a submitted warehouse id into one the caller may act on."""
    warehouse = accessible_warehouses(user).filter(pk=warehouse_id).first()
    if warehouse is None:
        raise OutOfScope(_("Warehouse %(id)s does not exist.") % {"id": warehouse_id})
    return warehouse


def resolve_organization(user: User, organization_id: int) -> Organization:
    """
    Turn a submitted organization id into an organization the caller reaches.

    Reaching an organization means holding organization-wide authority in it,
    or working at one of its branches. Everything else raises `OutOfScope`,
    whether the row exists or not, and with the same message either way — an
    organization the caller cannot reach must be indistinguishable from one
    that was never created.
    """
    reachable = set(organization_scope(user))
    reachable.update(accessible_branches(user).values_list("organization_id", flat=True))
    if organization_id not in reachable:
        raise OutOfScope(_("Organization %(id)s does not exist.") % {"id": organization_id})
    return Organization.objects.get(pk=organization_id)


def resolve_branch(user: User, branch_id: int) -> Branch:
    """Turn a submitted branch id into a branch the caller may act on."""
    branch = accessible_branches(user).filter(pk=branch_id).first()
    if branch is None:
        raise OutOfScope(_("Branch %(id)s does not exist.") % {"id": branch_id})
    return branch
