"""Role grants for the shared automation foundation.

These permissions are deliberately narrow: the inbox can acknowledge work,
not approve a payment or waive an accounting exception.  A domain service is
still the only place where a workflow can approve or post.
"""

from __future__ import annotations

from django.contrib.auth.models import Permission

from apps.organizations.models import Role
from apps.organizations.permissions import group_for_role

APP_LABEL = "core"
VIEW_AUTOMATION_TASK = f"{APP_LABEL}.view_automation_task"
ACKNOWLEDGE_AUTOMATION_TASK = f"{APP_LABEL}.acknowledge_automation_task"
VIEW_AUTOMATION_EXCEPTION = f"{APP_LABEL}.view_automation_exception"
VIEW_AUTOMATION_OUTBOX = f"{APP_LABEL}.view_automation_outbox"
REPLAY_AUTOMATION_OUTBOX = f"{APP_LABEL}.replay_automation_outbox"

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.OWNER: frozenset(
        {
            VIEW_AUTOMATION_TASK,
            ACKNOWLEDGE_AUTOMATION_TASK,
            VIEW_AUTOMATION_EXCEPTION,
            VIEW_AUTOMATION_OUTBOX,
            REPLAY_AUTOMATION_OUTBOX,
        }
    ),
    Role.ACCOUNTING_MANAGER: frozenset(
        {
            VIEW_AUTOMATION_TASK,
            ACKNOWLEDGE_AUTOMATION_TASK,
            VIEW_AUTOMATION_EXCEPTION,
            VIEW_AUTOMATION_OUTBOX,
            REPLAY_AUTOMATION_OUTBOX,
        }
    ),
    Role.MANAGER: frozenset(
        {VIEW_AUTOMATION_TASK, ACKNOWLEDGE_AUTOMATION_TASK, VIEW_AUTOMATION_EXCEPTION}
    ),
    Role.ACCOUNTANT: frozenset(
        {VIEW_AUTOMATION_TASK, ACKNOWLEDGE_AUTOMATION_TASK, VIEW_AUTOMATION_EXCEPTION}
    ),
    Role.PURCHASING: frozenset({VIEW_AUTOMATION_TASK}),
    Role.STOREKEEPER: frozenset({VIEW_AUTOMATION_TASK}),
    Role.CASHIER: frozenset({VIEW_AUTOMATION_TASK}),
    Role.VIEWER: frozenset(),
}


def sync_role_groups() -> None:
    """Install only Core's grants without touching another module's grants."""

    known = {
        f"{APP_LABEL}.{permission.codename}": permission
        for permission in Permission.objects.filter(content_type__app_label=APP_LABEL)
        if permission.codename
        in {
            "view_automation_task",
            "acknowledge_automation_task",
            "view_automation_exception",
            "view_automation_outbox",
            "replay_automation_outbox",
        }
    }
    expected = {
        permission for permissions in ROLE_PERMISSIONS.values() for permission in permissions
    }
    missing = expected - set(known)
    if missing:
        raise LookupError(f"core permissions are not migrated: {sorted(missing)}")
    ours = set(known.values())
    for role, names in ROLE_PERMISSIONS.items():
        group = group_for_role(role)
        wanted = {known[name] for name in names}
        group.permissions.remove(*(ours - wanted))
        group.permissions.add(*wanted)


__all__ = [
    "ACKNOWLEDGE_AUTOMATION_TASK",
    "REPLAY_AUTOMATION_OUTBOX",
    "VIEW_AUTOMATION_EXCEPTION",
    "VIEW_AUTOMATION_OUTBOX",
    "VIEW_AUTOMATION_TASK",
    "sync_role_groups",
]
