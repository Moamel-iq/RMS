"""
Insights permissions, and which post holds them.

Four capabilities, deliberately separate, because they are four different
kinds of trust:

    view_insight        read the findings for a scope you already reach
    manage_insight      acknowledge, dismiss, reopen — decide about a case
    run_insights        start an analysis
    configure_insights  move a threshold

The last one is the sharp one. A threshold decides what counts as a finding at
all, so somebody who can change it can make an inconvenient case disappear
without ever touching the case. It is granted to the owner alone and has no UI
in this stage — a service and model boundary only.

## Scope, not just permission

None of these is authorization on its own. Every one is checked with an
organization or branch through `apps.organizations.authorization`, per ADR-016:
a permission says *what*, a membership says *where*, and out-of-scope answers
404 rather than 403 so an id-guessing loop learns nothing.

## Sensitivity is a second gate, not this one

Stage 1's detector is `OPERATIONAL`, so `view_insight` is enough for it. Later
detectors carry `HR_RESTRICTED` findings about people, and those need their own
additional permission on top of these — the kernel keeps the sensitivity field
so that gate has something to stand on. Nothing here checks a role name; a role
is an input to a permission, never a substitute for one.
"""

from __future__ import annotations

from django.contrib.auth.models import Permission

from apps.organizations.models import Role
from apps.organizations.permissions import group_for_role

APP_LABEL = "insights"

VIEW_INSIGHT = f"{APP_LABEL}.view_insight"
MANAGE_INSIGHT = f"{APP_LABEL}.manage_insight"
RUN_INSIGHTS = f"{APP_LABEL}.run_insights"
CONFIGURE_INSIGHTS = f"{APP_LABEL}.configure_insights"

ALL_PERMISSIONS: tuple[str, ...] = (
    VIEW_INSIGHT,
    MANAGE_INSIGHT,
    RUN_INSIGHTS,
    CONFIGURE_INSIGHTS,
)

#: Who reads the analysis, and who may act on it.
#:
#: The owner and the two managers read and manage; the accountant and the
#: purchasing officer read. A storekeeper and a cashier are deliberately absent
#: for now: Stage 1's only finding is about *their own* recording discipline,
#: and the conversation it is meant to start is a managerial one. That is a
#: default, not a rule — the roles screen can grant it to anybody.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.OWNER.value: frozenset(ALL_PERMISSIONS),
    Role.MANAGER.value: frozenset({VIEW_INSIGHT, MANAGE_INSIGHT, RUN_INSIGHTS}),
    Role.ACCOUNTING_MANAGER.value: frozenset({VIEW_INSIGHT, MANAGE_INSIGHT, RUN_INSIGHTS}),
    Role.ACCOUNTANT.value: frozenset({VIEW_INSIGHT}),
    Role.PURCHASING.value: frozenset({VIEW_INSIGHT}),
    Role.STOREKEEPER.value: frozenset(),
    Role.CASHIER.value: frozenset(),
    Role.VIEWER.value: frozenset(),
}


def sync_role_groups() -> None:
    """
    Install this app's grants on the built-in role groups.

    Replaces only this app's permissions and leaves every other app's grants
    on the same group untouched — the group is shared, and a wholesale `set()`
    here would silently strip a role of its inventory or accounting rights.
    """
    known = {
        f"{APP_LABEL}.{permission.codename}": permission
        for permission in Permission.objects.filter(
            content_type__app_label=APP_LABEL, content_type__model="insight"
        )
    }
    missing = sorted(set(ALL_PERMISSIONS) - set(known))
    if missing:
        raise LookupError(f"insights permissions are not migrated: {missing}")

    ours = set(known.values())
    for role, names in ROLE_PERMISSIONS.items():
        group = group_for_role(role)
        wanted = {known[name] for name in names}
        group.permissions.remove(*(ours - wanted))
        group.permissions.add(*wanted)


__all__ = [
    "ALL_PERMISSIONS",
    "CONFIGURE_INSIGHTS",
    "MANAGE_INSIGHT",
    "ROLE_PERMISSIONS",
    "RUN_INSIGHTS",
    "VIEW_INSIGHT",
    "sync_role_groups",
]
