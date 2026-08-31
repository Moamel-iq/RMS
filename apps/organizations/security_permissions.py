"""Security-administration permissions and their built-in role grants.

These permissions deliberately live on :class:`Organization`.  Managing a
user, access grant, or role is never a global staff capability: it is an act
over one restaurant organization and must be paired with an active
``OrganizationMembership`` by the authorization layer.
"""

from __future__ import annotations

from django.contrib.auth.models import Permission

from apps.organizations.models import Role
from apps.organizations.permissions import group_for_role

APP_LABEL = "organizations"

MANAGE_USERS = f"{APP_LABEL}.manage_users"
MANAGE_ACCESS = f"{APP_LABEL}.manage_access"
MANAGE_ROLES = f"{APP_LABEL}.manage_roles"
VIEW_AUDIT = f"{APP_LABEL}.view_audit"
MANAGE_ORG_SETTINGS = f"{APP_LABEL}.manage_org_settings"

ALL_PERMISSIONS: tuple[str, ...] = (
    MANAGE_USERS,
    MANAGE_ACCESS,
    MANAGE_ROLES,
    VIEW_AUDIT,
    MANAGE_ORG_SETTINGS,
)

# Security administration is narrower than operational access, and narrower
# still than it looks: the manager runs the staff, and the owner runs the
# organization.
#
# **The manager administers people.** They hire, they assign posts, and they
# decide who works which branch — so they hold `manage_users`, `manage_access`
# and `manage_roles`. Requiring the owner for every new storekeeper made the
# owner a bottleneck on an everyday act, and the owner is not the person who
# knows which shift somebody works.
#
# What a manager still cannot do is make an owner. `_require_access_change_actor`
# refuses the OWNER role outright, refuses a manager changing their own access,
# and refuses touching a sitting owner's. Those three are what keeps
# `manage_roles` from being a route to unlimited authority: a manager may
# define any post and grant any post, except the one that could remove them.
#
# Accounting managers may review the scoped audit trail and grant nothing.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.OWNER.value: frozenset(ALL_PERMISSIONS),
    Role.ACCOUNTING_MANAGER.value: frozenset({VIEW_AUDIT}),
    Role.MANAGER.value: frozenset({MANAGE_USERS, MANAGE_ACCESS, MANAGE_ROLES}),
    Role.ACCOUNTANT.value: frozenset(),
    Role.PURCHASING.value: frozenset(),
    Role.STOREKEEPER.value: frozenset(),
    Role.CASHIER.value: frozenset(),
    Role.VIEWER.value: frozenset(),
}


def sync_role_groups() -> None:
    """Replace only this app's grants in the built-in role groups."""
    known = {
        f"{APP_LABEL}.{permission.codename}": permission
        for permission in Permission.objects.filter(
            content_type__app_label=APP_LABEL,
            content_type__model="organization",
        )
    }
    missing = sorted(set(ALL_PERMISSIONS) - set(known))
    if missing:
        raise LookupError(f"organization security permissions are not migrated: {missing}")

    ours = set(known.values())
    for role, permission_names in ROLE_PERMISSIONS.items():
        group = group_for_role(role)
        wanted = {known[name] for name in permission_names}
        group.permissions.remove(*(ours - wanted))
        group.permissions.add(*wanted)


__all__ = [
    "ALL_PERMISSIONS",
    "MANAGE_ACCESS",
    "MANAGE_ORG_SETTINGS",
    "MANAGE_ROLES",
    "MANAGE_USERS",
    "ROLE_PERMISSIONS",
    "VIEW_AUDIT",
    "sync_role_groups",
]
