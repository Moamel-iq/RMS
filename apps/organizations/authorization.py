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

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.utils.translation import gettext_lazy as _

from apps.organizations.models import Branch, Organization, OrganizationMembership
from apps.organizations.selectors import accessible_branches, can_access_branch
from apps.users.models import User


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


def require_branch_permission(user: User, permission: str, branch: Branch) -> None:
    """Raise unless the user may exercise this permission at this branch."""
    if not can_access_branch(user, branch):
        raise OutOfScope(_("Branch %(id)s does not exist.") % {"id": branch.pk})
    if not user.is_authenticated or not user.is_active or not user.has_perm(permission):
        raise PermissionMissing(
            _("%(permission)s is not held at branch %(branch)s.")
            % {"permission": permission, "branch": branch.code}
        )


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
