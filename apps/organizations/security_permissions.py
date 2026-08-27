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

# Security administration is intentionally narrower than operational access.
# Only the organization's accountable owner can change who holds authority.
# Accounting managers may review the scoped audit trail but cannot grant
# themselves or anyone else a wider post.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.OWNER.value: frozenset(ALL_PERMISSIONS),
    Role.ACCOUNTING_MANAGER.value: frozenset({VIEW_AUDIT}),
    Role.MANAGER.value: frozenset(),
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
