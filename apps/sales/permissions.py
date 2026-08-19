"""
The sales permissions, their scope, and which role holds them.

Same machinery as `apps/inventory/permissions.py`,
`apps/procurement/permissions.py` and `apps/kitchen/permissions.py`,
deliberately: a permission says *what*, a membership says *where*, and neither
alone is authorization (ADR-016).

## Declared one checkpoint at a time

`_TABLE` grows as Phase 4's documents land, and never ahead of them. That is
the repository's standing rule rather than a convenience — kitchen's own
permission table records the same growth across Tasks 3.1 to 3.8 — because a
permission for a workflow that does not exist is a grant nobody can audit, and
`sync_role_groups` would fail loudly on a name with no migration behind it.

One row per permission, carrying its scope *and* the roles that hold it, so
adding a permission is one edit rather than three that can disagree.
`ALL_PERMISSIONS`, `PERMISSION_SCOPE` and `ROLE_PERMISSIONS` are all derived
and keep the same shape the other modules expose.

## The three separations this table exists to make

**Master data from operations.** Who may set a commission rate is not who may
enter Tuesday's takings. An agreement decides what every future application
order is worth to the restaurant; a day's sales entry records what happened. A
cashier legitimately does the second and must never do the first.

**Entering from posting.** `create_daily_sales` writes a draft that moves no
money. `post_daily_sales` writes a journal, an application receivable and a
theoretical-consumption contribution, and correcting it afterwards is a
reversal that stays on the record forever. A restaurant where the person who
types the numbers also commits them has no second pair of eyes on the one step
that reaches the ledger.

**Counting from approving.** `close_cashier_shift` is the cashier declaring
what is in the drawer; `approve_cashier_closing` is somebody else agreeing.
Maker-checker is enforced on the **actor**, not on the permission — a branch
manager may legitimately hold both, and what they may not do is use both on the
same shift. Encoding it as "only some role may approve" would break the moment
a branch had one manager, and would be a weaker control besides.

## Cost is its own permission, and money is omitted rather than blanked

`view_sales_cost` guards food cost, recipe cost, margin and every derived
profitability figure. A blanked column tells the reader a number exists and
that they are not trusted with it, which is a different statement from the one
intended, so the columns are **absent** — the rule inventory applies to
valuation and procurement to supplier cost.

Note what it is *not*: it does not guard the selling price, the discount, the
commission or the receivable. Those are sales figures, and a cashier who may
not know what a plate costs still has to read what it sold for.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from apps.organizations.models import Role

APP_LABEL = "sales"


class PermissionScope(Enum):
    """
    Where a sales permission is exercised.

    No `WAREHOUSE` value, and its absence is the point: Release 1 sells recipe
    servings, and a serving leaves stock through the production batch that
    cooked it rather than through the sale. Nothing here takes custody of a
    store, so scoping anything to a warehouse would be asking a question sales
    cannot answer.
    """

    #: The caller need only *reach* the organization — a branch membership is
    #: enough, exactly as it is for the item, supplier and recipe masters. One
    #: menu, one set of channels, one set of contracts.
    ORGANIZATION_MASTER_DATA = "ORGANIZATION_MASTER_DATA"
    #: The caller needs the authority across the whole organization. Used for
    #: the acts that decide policy or settle a contract.
    ORGANIZATION_AUTHORITY = "ORGANIZATION_AUTHORITY"
    #: Exercised at one branch. A day of trading, a till, a shift.
    BRANCH = "BRANCH"


# --- Names ------------------------------------------------------------------
#
# Every Phase 4 permission is named here so the vocabulary is readable in one
# place, but a name is not a grant: only the rows in `_TABLE` below are real,
# and a name whose checkpoint has not landed appears in neither
# `ALL_PERMISSIONS` nor any role.

VIEW_SALES = f"{APP_LABEL}.view_sales"
MANAGE_MENU = f"{APP_LABEL}.manage_menu"
VIEW_SALES_REPORTS = f"{APP_LABEL}.view_sales_reports"
VIEW_SALES_COST = f"{APP_LABEL}.view_sales_cost"
MANAGE_SALES_CHANNELS = f"{APP_LABEL}.manage_sales_channels"

MANAGE_DELIVERY_APPLICATIONS = f"{APP_LABEL}.manage_delivery_applications"
MANAGE_SALES_AGREEMENTS = f"{APP_LABEL}.manage_sales_agreements"
MANAGE_SALES_DISCOUNTS = f"{APP_LABEL}.manage_sales_discounts"

CREATE_DAILY_SALES = f"{APP_LABEL}.create_daily_sales"
SUBMIT_DAILY_SALES = f"{APP_LABEL}.submit_daily_sales"
POST_DAILY_SALES = f"{APP_LABEL}.post_daily_sales"
REVERSE_DAILY_SALES = f"{APP_LABEL}.reverse_daily_sales"

MANAGE_SALES_ADJUSTMENTS = f"{APP_LABEL}.manage_sales_adjustments"

VIEW_APPLICATION_RECEIVABLES = f"{APP_LABEL}.view_application_receivables"
MANAGE_APPLICATION_SETTLEMENTS = f"{APP_LABEL}.manage_application_settlements"

CLOSE_CASHIER_SHIFT = f"{APP_LABEL}.close_cashier_shift"
APPROVE_CASHIER_CLOSING = f"{APP_LABEL}.approve_cashier_closing"


# --- What is actually granted -----------------------------------------------

_OWNER = Role.OWNER.value
_ACCOUNTING_MANAGER = Role.ACCOUNTING_MANAGER.value
_MANAGER = Role.MANAGER.value
_ACCOUNTANT = Role.ACCOUNTANT.value
_PURCHASING = Role.PURCHASING.value
_STOREKEEPER = Role.STOREKEEPER.value
_CASHIER = Role.CASHIER.value
_VIEWER = Role.VIEWER.value


@dataclass(frozen=True)
class _Declared:
    """One permission, its scope, and the roles that carry it."""

    permission: str
    scope: PermissionScope
    roles: frozenset[str]


#: **Checkpoint 1.** The menu, its prices, the channels, and the two reads.
#:
#: `Role.CASHIER` receives its first permissions in the entire system here,
#: which is worth noting: until Phase 4 the role existed and granted nothing
#: anywhere, in any module.
_TABLE: tuple[_Declared, ...] = (
    # The domain read. Wide on purpose — a storekeeper legitimately needs to
    # know what the branch sells to understand what its kitchen will demand,
    # and a purchaser does not, because buying is not selling.
    _Declared(
        VIEW_SALES,
        PermissionScope.ORGANIZATION_MASTER_DATA,
        frozenset(
            {_OWNER, _ACCOUNTING_MANAGER, _MANAGER, _ACCOUNTANT, _STOREKEEPER, _CASHIER, _VIEWER}
        ),
    ),
    # The menu is organization property, exactly as the recipe master is: one
    # dish, one menu. Finance holds it as well as operations — not to design
    # the menu, but because a price's effective date decides where money lands
    # and when.
    _Declared(
        MANAGE_MENU,
        PermissionScope.ORGANIZATION_MASTER_DATA,
        frozenset({_OWNER, _ACCOUNTING_MANAGER, _MANAGER}),
    ),
    _Declared(
        MANAGE_SALES_CHANNELS,
        PermissionScope.ORGANIZATION_MASTER_DATA,
        frozenset({_OWNER, _ACCOUNTING_MANAGER, _MANAGER}),
    ),
    _Declared(
        VIEW_SALES_REPORTS,
        PermissionScope.ORGANIZATION_MASTER_DATA,
        frozenset({_OWNER, _ACCOUNTING_MANAGER, _MANAGER, _ACCOUNTANT, _VIEWER}),
    ),
    # Deliberately **not** the cashier, and deliberately not the viewer. What a
    # plate costs to make is not information a till needs, and every cost
    # column is omitted rather than blanked.
    # --- Checkpoint 2 -----------------------------------------------------
    #
    # The delivery master and the two contract permissions. The split between
    # them is the module's first real separation of duties: registering that
    # the restaurant trades with a company is operational, and agreeing what
    # that company charges is not.
    _Declared(
        MANAGE_DELIVERY_APPLICATIONS,
        PermissionScope.ORGANIZATION_MASTER_DATA,
        frozenset({_OWNER, _ACCOUNTING_MANAGER, _MANAGER}),
    ),
    # `ORGANIZATION_AUTHORITY`, not `ORGANIZATION_MASTER_DATA`, and the
    # difference is the whole point: an agreement decides what every future
    # order at that branch is worth to the restaurant, and reaching the
    # organization through one branch membership is not enough to change a
    # commercial contract.
    _Declared(
        MANAGE_SALES_AGREEMENTS,
        PermissionScope.ORGANIZATION_AUTHORITY,
        frozenset({_OWNER, _ACCOUNTING_MANAGER, _MANAGER}),
    ),
    # Same scope, and deliberately **not** the cashier. A discount that a till
    # can invent is a discount nobody approved; manual discounts exist and are
    # a separate, reasoned, audited act on a sales line rather than a new
    # programme.
    _Declared(
        MANAGE_SALES_DISCOUNTS,
        PermissionScope.ORGANIZATION_AUTHORITY,
        frozenset({_OWNER, _ACCOUNTING_MANAGER, _MANAGER}),
    ),
    # --- Checkpoint 3 -----------------------------------------------------
    #
    # Entering is separated from posting, and posting from reversing. A draft
    # moves no money; posting writes a journal, an application receivable and a
    # theoretical-consumption contribution, and correcting it afterwards is a
    # reversal that stays on the record forever.
    _Declared(
        CREATE_DAILY_SALES,
        PermissionScope.BRANCH,
        frozenset({_OWNER, _ACCOUNTING_MANAGER, _MANAGER, _ACCOUNTANT, _CASHIER}),
    ),
    _Declared(
        SUBMIT_DAILY_SALES,
        PermissionScope.BRANCH,
        frozenset({_OWNER, _ACCOUNTING_MANAGER, _MANAGER, _ACCOUNTANT, _CASHIER}),
    ),
    # **Not the cashier.** A till that could commit its own takings to the
    # ledger has no second pair of eyes on the one step that reaches it.
    _Declared(
        POST_DAILY_SALES,
        PermissionScope.BRANCH,
        frozenset({_OWNER, _ACCOUNTING_MANAGER, _MANAGER}),
    ),
    # Elevated, and organization-wide: undoing a posted economic event is
    # supervisory, exactly as the goods-receipt reversal is, and the person who
    # posted should not be the only one who can make it disappear.
    _Declared(
        REVERSE_DAILY_SALES,
        PermissionScope.ORGANIZATION_AUTHORITY,
        frozenset({_OWNER, _ACCOUNTING_MANAGER}),
    ),
    _Declared(
        VIEW_SALES_COST,
        PermissionScope.ORGANIZATION_MASTER_DATA,
        frozenset({_OWNER, _ACCOUNTING_MANAGER, _MANAGER, _ACCOUNTANT}),
    ),
)


ALL_PERMISSIONS: tuple[str, ...] = tuple(row.permission for row in _TABLE)

PERMISSION_SCOPE: dict[str, PermissionScope] = {row.permission: row.scope for row in _TABLE}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    role: frozenset(row.permission for row in _TABLE if role in row.roles) for role in Role.values
}


def permissions_for_role(role: Role | str) -> frozenset[str]:
    """The sales permissions a role carries. Unknown roles carry none."""
    value = role.value if isinstance(role, Role) else str(role)
    return ROLE_PERMISSIONS.get(value, frozenset())


def scope_of(permission: str) -> PermissionScope:
    """Whether a permission is exercised over an organization or a branch."""
    try:
        return PERMISSION_SCOPE[permission]
    except KeyError:  # pragma: no cover - a typo in a caller, not a state
        raise ValueError(f"{permission} is not a declared sales permission") from None


def sync_role_groups() -> None:
    """
    Write `ROLE_PERMISSIONS` into the role groups.

    Replaces each role's *sales* permissions and leaves every other app's
    alone, so this and the inventory, accounting, procurement and kitchen
    equivalents can all run without trampling each other.
    """
    from django.contrib.auth.models import Permission

    from apps.organizations.permissions import group_for_role

    known: dict[str, Permission] = {
        f"{APP_LABEL}.{permission.codename}": permission
        for permission in Permission.objects.filter(content_type__app_label=APP_LABEL)
    }

    missing = [name for name in ALL_PERMISSIONS if name not in known]
    if missing:
        raise LookupError(f"sales permissions are not migrated: {sorted(missing)}")

    ours = set(known.values())
    for role, permission_names in ROLE_PERMISSIONS.items():
        group = group_for_role(role)
        wanted = {known[name] for name in permission_names}
        group.permissions.remove(*(ours - wanted))
        group.permissions.add(*wanted)


__all__ = [
    "ALL_PERMISSIONS",
    "APPROVE_CASHIER_CLOSING",
    "APP_LABEL",
    "CLOSE_CASHIER_SHIFT",
    "CREATE_DAILY_SALES",
    "MANAGE_APPLICATION_SETTLEMENTS",
    "MANAGE_DELIVERY_APPLICATIONS",
    "MANAGE_MENU",
    "MANAGE_SALES_ADJUSTMENTS",
    "MANAGE_SALES_AGREEMENTS",
    "MANAGE_SALES_CHANNELS",
    "MANAGE_SALES_DISCOUNTS",
    "PERMISSION_SCOPE",
    "POST_DAILY_SALES",
    "REVERSE_DAILY_SALES",
    "ROLE_PERMISSIONS",
    "SUBMIT_DAILY_SALES",
    "VIEW_APPLICATION_RECEIVABLES",
    "VIEW_SALES",
    "VIEW_SALES_COST",
    "VIEW_SALES_REPORTS",
    "PermissionScope",
    "permissions_for_role",
    "scope_of",
    "sync_role_groups",
]
