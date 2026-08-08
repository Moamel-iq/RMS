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
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext_lazy as _

from apps.organizations.models import Branch, Organization, OrganizationMembership
from apps.organizations.selectors import accessible_branches, can_access_branch
from apps.users.models import User


class ScopeError(PermissionDenied):
    """
    The caller named a place they do not reach.

    A subclass of PermissionDenied rather than a 404: the API answers 403 so
    the caller learns their authority is insufficient, instead of being told
    the record does not exist and retrying forever.
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


def has_organization_permission(user: User, permission: str, organization: Organization) -> bool:
    """
    Both halves: the permission, and organization-wide standing here.

    A superuser satisfies both — Django grants every permission, and
    `organization_scope` returns every active organization. That is the
    emergency authority, and it is deliberately not a bypass: the service the
    superuser reaches still validates the reason, the ordering, and the audit
    actor exactly as it does for anyone else.
    """
    if not user.is_authenticated or not user.is_active:
        return False
    return user.has_perm(permission) and has_organization_scope(user, organization)


def has_branch_permission(user: User, permission: str, branch: Branch) -> bool:
    """Both halves: the permission, and access to this branch."""
    if not user.is_authenticated or not user.is_active:
        return False
    return user.has_perm(permission) and can_access_branch(user, branch)


def require_organization_permission(
    user: User, permission: str, organization: Organization
) -> None:
    """Raise unless the user may exercise this permission over this organization."""
    if not has_organization_permission(user, permission, organization):
        raise ScopeError(
            _("%(permission)s is not held over organization %(organization)s.")
            % {"permission": permission, "organization": organization.code}
        )


def require_branch_permission(user: User, permission: str, branch: Branch) -> None:
    """Raise unless the user may exercise this permission at this branch."""
    if not has_branch_permission(user, permission, branch):
        raise ScopeError(
            _("%(permission)s is not held at branch %(branch)s.")
            % {"permission": permission, "branch": branch.code}
        )


def resolve_organization(user: User, organization_id: int) -> Organization:
    """
    Turn a submitted organization id into an organization the caller reaches.

    Reaching an organization means holding organization-wide authority in it,
    or working at one of its branches. Everything else raises, whether the row
    exists or not — a caller who cannot reach it learns the same thing either
    way.
    """
    reachable = set(organization_scope(user))
    reachable.update(accessible_branches(user).values_list("organization_id", flat=True))
    if organization_id not in reachable:
        raise ScopeError(_("Organization %(id)s is outside your access.") % {"id": organization_id})
    return Organization.objects.get(pk=organization_id)


def resolve_branch(user: User, branch_id: int) -> Branch:
    """Turn a submitted branch id into a branch the caller may act on."""
    branch = accessible_branches(user).filter(pk=branch_id).first()
    if branch is None:
        raise ScopeError(_("Branch %(id)s is outside your access.") % {"id": branch_id})
    return branch
