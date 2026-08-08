"""
The twelve accounting permissions, their scope, and which role holds them.

Django's `add`/`change`/`delete` describe table access, not accounting acts.
"May amend a draft" and "may post it to the ledger" are different authorities
over the same table, and a ledger where they are the same permission has no
separation of duties at all. So the kernel is gated by named acts.

Two independent facts about every permission:

* **Scope** — is it exercised over an organization, or at a branch? A journal
  line names a branch, so posting is branch-scoped. A period spans every
  branch in the organization at once, so period acts are organization-scoped
  and branch authority never reaches them (ADR-013).
* **Roles** — which posts carry it. Scope and role are separate on purpose: a
  branch accountant and an organization accountant hold the same list of
  permissions and are limited only by where they hold them.
"""

from __future__ import annotations

from enum import Enum

from apps.organizations.models import Role

APP_LABEL = "accounting"


class PermissionScope(Enum):
    """Where a permission is exercised."""

    ORGANIZATION = "ORGANIZATION"
    BRANCH = "BRANCH"


# --- The twelve -----------------------------------------------------------

VIEW_JOURNAL = f"{APP_LABEL}.view_journal"
CREATE_DRAFT = f"{APP_LABEL}.create_draft"
EDIT_DRAFT = f"{APP_LABEL}.edit_draft"
POST_JOURNAL = f"{APP_LABEL}.post_journal"
REVERSE_JOURNAL = f"{APP_LABEL}.reverse_journal"
MANAGE_ACCOUNTS = f"{APP_LABEL}.manage_accounts"
MANAGE_COST_CENTERS = f"{APP_LABEL}.manage_cost_centers"
SOFT_CLOSE_PERIOD = f"{APP_LABEL}.soft_close_period"
CLOSE_PERIOD = f"{APP_LABEL}.close_period"
REOPEN_PERIOD = f"{APP_LABEL}.reopen_period"
POST_SOFT_CLOSED_ADJUSTMENT = f"{APP_LABEL}.post_soft_closed_adjustment"
REVERSE_IN_SOFT_CLOSED_PERIOD = f"{APP_LABEL}.reverse_in_soft_closed_period"

ALL_PERMISSIONS: tuple[str, ...] = (
    VIEW_JOURNAL,
    CREATE_DRAFT,
    EDIT_DRAFT,
    POST_JOURNAL,
    REVERSE_JOURNAL,
    MANAGE_ACCOUNTS,
    MANAGE_COST_CENTERS,
    SOFT_CLOSE_PERIOD,
    CLOSE_PERIOD,
    REOPEN_PERIOD,
    POST_SOFT_CLOSED_ADJUSTMENT,
    REVERSE_IN_SOFT_CLOSED_PERIOD,
)


PERMISSION_SCOPE: dict[str, PermissionScope] = {
    # A journal line names a branch, so these are answered per branch.
    VIEW_JOURNAL: PermissionScope.BRANCH,
    CREATE_DRAFT: PermissionScope.BRANCH,
    EDIT_DRAFT: PermissionScope.BRANCH,
    POST_JOURNAL: PermissionScope.BRANCH,
    REVERSE_JOURNAL: PermissionScope.BRANCH,
    # The chart of accounts and the cost centres belong to the organization
    # (ADR-014, ADR-015). One branch must not reshape what the others post to.
    MANAGE_ACCOUNTS: PermissionScope.ORGANIZATION,
    MANAGE_COST_CENTERS: PermissionScope.ORGANIZATION,
    # A period covers every branch at once. Closing one from a single branch's
    # authority would stop the other branches posting.
    SOFT_CLOSE_PERIOD: PermissionScope.ORGANIZATION,
    CLOSE_PERIOD: PermissionScope.ORGANIZATION,
    REOPEN_PERIOD: PermissionScope.ORGANIZATION,
    # Overriding a soft close is an organization-level accounting decision,
    # even though the posting it authorizes lands on a branch. Both are
    # checked: the override over the organization, the lines at their branches.
    POST_SOFT_CLOSED_ADJUSTMENT: PermissionScope.ORGANIZATION,
    REVERSE_IN_SOFT_CLOSED_PERIOD: PermissionScope.ORGANIZATION,
}


# --- Which role holds what ------------------------------------------------

#: The full accounting authority. Held by the posts that answer for the
#: ledger as a whole.
_FULL = frozenset(ALL_PERMISSIONS)

#: Day-to-day accounting. Note what is absent: `reopen_period` and both
#: soft-closed override permissions. Reopening a closed period lets history be
#: added to after the fact, and an accountant who can both post and reopen can
#: undo their own close unobserved. It is the Accounting Manager's decision.
_ACCOUNTANT = frozenset(
    {
        VIEW_JOURNAL,
        CREATE_DRAFT,
        EDIT_DRAFT,
        POST_JOURNAL,
        REVERSE_JOURNAL,
        MANAGE_ACCOUNTS,
        MANAGE_COST_CENTERS,
        SOFT_CLOSE_PERIOD,
        CLOSE_PERIOD,
    }
)

#: Read the ledger, change nothing in it.
_READ_ONLY = frozenset({VIEW_JOURNAL})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.OWNER.value: _FULL,
    Role.ACCOUNTING_MANAGER.value: _FULL,
    Role.ACCOUNTANT.value: _ACCOUNTANT,
    Role.MANAGER.value: _READ_ONLY,
    Role.PURCHASING.value: _READ_ONLY,
    Role.VIEWER.value: _READ_ONLY,
    # Neither post carries accounting authority. A cashier records takings
    # through the sales module, which posts on their behalf under its own
    # rules; a storekeeper moves stock. Granting either a journal permission
    # would let them write the ledger directly.
    Role.STOREKEEPER.value: frozenset(),
    Role.CASHIER.value: frozenset(),
}


def permissions_for_role(role: Role | str) -> frozenset[str]:
    """The accounting permissions a role carries. Unknown roles carry none."""
    value = role.value if isinstance(role, Role) else str(role)
    return ROLE_PERMISSIONS.get(value, frozenset())


def scope_of(permission: str) -> PermissionScope:
    """Whether a permission is exercised over an organization or at a branch."""
    try:
        return PERMISSION_SCOPE[permission]
    except KeyError:  # pragma: no cover - a typo in a caller, not a state
        raise ValueError(f"{permission} is not an accounting permission") from None


def sync_role_groups() -> None:
    """
    Write `ROLE_PERMISSIONS` into the role groups.

    Each role's accounting permissions are *replaced*, not added to, so a
    permission removed from the table above is actually removed from the
    group. Permissions belonging to other apps are left untouched, so this can
    run alongside the sync commands modules will add later without any of them
    trampling the others.

    Idempotent, and run on every migrate — a role whose group does not yet
    exist gets one.
    """
    from django.contrib.auth.models import Permission

    from apps.organizations.permissions import group_for_role

    known: dict[str, Permission] = {}
    for permission in Permission.objects.filter(content_type__app_label=APP_LABEL):
        known[f"{APP_LABEL}.{permission.codename}"] = permission

    missing = [name for name in ALL_PERMISSIONS if name not in known]
    if missing:
        # Only reachable if a permission was removed from a model's Meta but
        # left in the table above. Silence would hand out a role that quietly
        # lacks an authority someone believes it has.
        raise LookupError(f"accounting permissions are not migrated: {sorted(missing)}")

    ours = set(known.values())
    for role, permission_names in ROLE_PERMISSIONS.items():
        group = group_for_role(role)
        wanted = {known[name] for name in permission_names}
        group.permissions.remove(*(ours - wanted))
        group.permissions.add(*wanted)
