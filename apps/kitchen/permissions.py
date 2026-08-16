"""
The kitchen permissions, their scope, and which role holds them.

Identical machinery to `apps/inventory/permissions.py`,
`apps/accounting/permissions.py` and `apps/procurement/permissions.py`,
deliberately: a permission says *what*, a membership says *where*, and neither
alone is authorization (ADR-016).

Task 3.1 declares **three**, and no more. The production, meal, report and
import permissions arrive with the documents they guard, one task at a time,
because a permission for a workflow that does not exist is a grant nobody can
audit. In particular there is no `approve_recipe_version` here: Task 3.2 owns
approval, and registering its permission early would hand out authority over a
lifecycle that cannot yet be reached.

The split that matters is between the **card** and the **cost**. A cook reads
the recipe card, the quantities and the method — that is the job. What the dish
costs is a separate question with a separate permission, exactly as inventory
separates `view_valuation` from `view_stock` and procurement separates
`view_supplier_cost` from `view_supplier`. Cost columns are **omitted, not
blanked**: a blanked column tells the reader a number exists and that they are
not trusted with it, which is a different statement from the one intended.

Task 3.1 ships no cost column at all — costing is Task 3.3 — so
`view_recipe_cost` guards nothing yet. It is registered now because the roles
that will hold it are decided here, in one place, rather than discovered later
when the first cost column appears.
"""

from __future__ import annotations

from enum import Enum

from apps.organizations.models import Role

APP_LABEL = "kitchen"


class PermissionScope(Enum):
    """
    Where a kitchen permission is exercised.

    Task 3.1 uses one value. `ORGANIZATION_MASTER_DATA` needs the caller to
    *reach* the organization — a branch membership is enough, because a branch
    manager legitimately maintains the shared recipe master, exactly as they
    maintain the shared item and supplier masters.

    The other values are declared because Task 3.5's production permissions
    will be **warehouse**-scoped: a batch moves stock, and inventory already
    scopes custody that way (RCP-051). Declaring the enum in full now keeps
    that later addition from looking like a redesign.
    """

    ORGANIZATION_MASTER_DATA = "ORGANIZATION_MASTER_DATA"
    ORGANIZATION_AUTHORITY = "ORGANIZATION_AUTHORITY"
    BRANCH = "BRANCH"
    WAREHOUSE = "WAREHOUSE"


# --- The three --------------------------------------------------------------

#: Arrives from Django's default set for the `Recipe` model rather than from
#: `Meta.permissions`, because declaring `view_recipe` again would be an
#: `auth.E005` clash with the builtin. The codename is the one this module
#: checks, so it is real — it simply comes from the other half of the table.
#: `apps.procurement` records the same arrangement for `view_supplier`.
VIEW_RECIPE = f"{APP_LABEL}.view_recipe"
MANAGE_RECIPE = f"{APP_LABEL}.manage_recipe"
VIEW_RECIPE_COST = f"{APP_LABEL}.view_recipe_cost"

ALL_PERMISSIONS: tuple[str, ...] = (
    VIEW_RECIPE,
    MANAGE_RECIPE,
    VIEW_RECIPE_COST,
)

PERMISSION_SCOPE: dict[str, PermissionScope] = {
    # A recipe is organization property. The dish is one dish; one branch must
    # not invent its own version of the group's menu (RCP-006).
    VIEW_RECIPE: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_RECIPE: PermissionScope.ORGANIZATION_MASTER_DATA,
    VIEW_RECIPE_COST: PermissionScope.ORGANIZATION_MASTER_DATA,
}


# --- Which role holds what --------------------------------------------------

_FULL = frozenset(ALL_PERMISSIONS)

#: Runs a branch end to end, including its kitchen. The nearest post this
#: deployment has to the workbook's الشيف, which is also why no new Chef role
#: is invented here: `KM-RCP-004` assigns the approved quantity to chef **plus**
#: accountant **plus** manager, and inventing a role to hold one third of a
#: three-party control would misrepresent the control.
_MANAGER = frozenset({VIEW_RECIPE, MANAGE_RECIPE, VIEW_RECIPE_COST})

#: Answers for the figures. Reads recipes and their costs; writes neither —
#: inventing a dish is a kitchen act, not an accounting one.
_ACCOUNTING_MANAGER = frozenset({VIEW_RECIPE, VIEW_RECIPE_COST})

#: The workbook assigns `كلفة الوحدة` and `كلفة المكون` to المحاسب, so the
#: accountant reads recipe cost by the kitchen's own arrangement.
_ACCOUNTANT = frozenset({VIEW_RECIPE, VIEW_RECIPE_COST})

#: Issues ingredients against a recipe card, and has no business seeing what
#: they cost — the same boundary that keeps stock valuation away from the
#: person counting the shelves.
_STOREKEEPER = frozenset({VIEW_RECIPE})

#: Buys the ingredients a recipe names, so needs to read the card. Recipe cost
#: is a kitchen and accounting figure, not a purchasing one.
_PURCHASING = frozenset({VIEW_RECIPE})

#: Reads what exists, never what it cost.
_VIEWER = frozenset({VIEW_RECIPE})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.OWNER.value: _FULL,
    Role.ACCOUNTING_MANAGER.value: _ACCOUNTING_MANAGER,
    Role.MANAGER.value: _MANAGER,
    Role.STOREKEEPER.value: _STOREKEEPER,
    Role.PURCHASING.value: _PURCHASING,
    Role.ACCOUNTANT.value: _ACCOUNTANT,
    Role.VIEWER.value: _VIEWER,
    # A cashier handles takings, not recipes.
    Role.CASHIER.value: frozenset(),
}


def permissions_for_role(role: Role | str) -> frozenset[str]:
    """The kitchen permissions a role carries. Unknown roles carry none."""
    value = role.value if isinstance(role, Role) else str(role)
    return ROLE_PERMISSIONS.get(value, frozenset())


def scope_of(permission: str) -> PermissionScope:
    """Whether a permission is exercised over an organization, branch, or warehouse."""
    try:
        return PERMISSION_SCOPE[permission]
    except KeyError:  # pragma: no cover - a typo in a caller, not a state
        raise ValueError(f"{permission} is not a kitchen permission") from None


def sync_role_groups() -> None:
    """
    Write `ROLE_PERMISSIONS` into the role groups.

    Replaces each role's *kitchen* permissions and leaves every other app's
    alone, so this and the inventory, accounting and procurement equivalents
    can all run without trampling each other.
    """
    from django.contrib.auth.models import Permission

    from apps.organizations.permissions import group_for_role

    known: dict[str, Permission] = {
        f"{APP_LABEL}.{permission.codename}": permission
        for permission in Permission.objects.filter(content_type__app_label=APP_LABEL)
    }

    missing = [name for name in ALL_PERMISSIONS if name not in known]
    if missing:
        raise LookupError(f"kitchen permissions are not migrated: {sorted(missing)}")

    ours = set(known.values())
    for role, permission_names in ROLE_PERMISSIONS.items():
        group = group_for_role(role)
        wanted = {known[name] for name in permission_names}
        group.permissions.remove(*(ours - wanted))
        group.permissions.add(*wanted)
