"""
The sales permissions, their scope, and which role holds them.

Identical machinery to `apps/inventory/permissions.py`,
`apps/procurement/permissions.py` and `apps/kitchen/permissions.py`,
deliberately: a permission says *what*, a membership says *where*, and neither
alone is authorization (ADR-016).

## The three separations this table exists to make

**Master data from operations.** Who may set a commission rate is not who may
enter Tuesday's takings. An agreement decides what every future application
order is worth to the restaurant; a day's sales entry records what happened.
A cashier legitimately does the second and must never do the first, which is
why `manage_sales_agreements` sits with the manager and `create_daily_sales`
sits with everybody who works a till.

**Entering from posting.** `create_daily_sales` writes a draft that moves no
money. `post_daily_sales` writes a journal, an application receivable and a
theoretical-consumption contribution, and correcting it afterwards is a
reversal that stays on the record forever. A restaurant where the person who
types the numbers also commits them has no second pair of eyes on the one step
that reaches the ledger.

**Counting from approving.** `close_cashier_shift` is the cashier declaring
what is in the drawer. `approve_cashier_closing` is somebody else agreeing.
Maker-checker is enforced on the **actor**, not on the permission — a branch
manager may legitimately hold both, and what they may not do is use both on the
same shift. Encoding it as "only some role may approve" would break the moment
a branch had one manager, and would be a weaker control besides.

## Cost is its own permission, and money is omitted rather than blanked

`view_sales_cost` guards food cost, recipe cost, margin and every derived
profitability figure on the dashboard. A blanked column tells the reader a
number exists and that they are not trusted with it, which is a different
statement from the one intended, so the columns are **absent** — the same rule
inventory applies to valuation and procurement to supplier cost.

Note what `view_sales_cost` is *not*: it does not guard the selling price, the
discount, the commission or the receivable. Those are sales figures, and a
cashier who may not know what a plate costs still has to be able to read what
it sold for.
"""

from __future__ import annotations

from enum import Enum

from apps.organizations.models import Role

APP_LABEL = "sales"


class PermissionScope(Enum):
    """
    Where a sales permission is exercised.

    No `WAREHOUSE` value, and its absence is the point: Release 1 sells
    recipe servings, and a recipe serving leaves stock through the production
    batch that cooked it rather than through the sale. Nothing in this module
    takes custody of a store, so scoping anything here to a warehouse would be
    asking a question sales cannot answer.
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


# --- The seventeen ----------------------------------------------------------

#: Arrives from Django's default set for `SalesDay` rather than from
#: `Meta.permissions`, because declaring `view_salesday` again would be an
#: `auth.E005` clash with the builtin. `apps.kitchen` records the same
#: arrangement for `view_recipe`, and `apps.procurement` for `view_supplier`.
VIEW_SALES = f"{APP_LABEL}.view_salesday"

MANAGE_MENU = f"{APP_LABEL}.manage_menu"
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

VIEW_SALES_REPORTS = f"{APP_LABEL}.view_sales_reports"
VIEW_SALES_COST = f"{APP_LABEL}.view_sales_cost"

ALL_PERMISSIONS: tuple[str, ...] = (
    VIEW_SALES,
    MANAGE_MENU,
    MANAGE_SALES_CHANNELS,
    MANAGE_DELIVERY_APPLICATIONS,
    MANAGE_SALES_AGREEMENTS,
    MANAGE_SALES_DISCOUNTS,
    CREATE_DAILY_SALES,
    SUBMIT_DAILY_SALES,
    POST_DAILY_SALES,
    REVERSE_DAILY_SALES,
    MANAGE_SALES_ADJUSTMENTS,
    VIEW_APPLICATION_RECEIVABLES,
    MANAGE_APPLICATION_SETTLEMENTS,
    CLOSE_CASHIER_SHIFT,
    APPROVE_CASHIER_CLOSING,
    VIEW_SALES_REPORTS,
    VIEW_SALES_COST,
)

PERMISSION_SCOPE: dict[str, PermissionScope] = {
    # The menu is organization property, exactly as the recipe master is. One
    # dish, one menu; a branch must not invent its own price list for the
    # group's food. Where a branch genuinely differs — this item is not sold
    # here, this channel charges more — that is *data* on the item and its
    # prices, not a separate branch-owned master.
    VIEW_SALES: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_MENU: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_SALES_CHANNELS: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_DELIVERY_APPLICATIONS: PermissionScope.ORGANIZATION_MASTER_DATA,
    # An agreement and a discount programme are **contracts**, and reaching the
    # organization is not enough to change one. `ORGANIZATION_AUTHORITY` means
    # the caller holds the permission across the organization rather than at
    # one branch: a branch manager who could quietly change the commission
    # basis would be changing what every other branch earns.
    MANAGE_SALES_AGREEMENTS: PermissionScope.ORGANIZATION_AUTHORITY,
    MANAGE_SALES_DISCOUNTS: PermissionScope.ORGANIZATION_AUTHORITY,
    # A day of trading belongs to one branch.
    CREATE_DAILY_SALES: PermissionScope.BRANCH,
    SUBMIT_DAILY_SALES: PermissionScope.BRANCH,
    POST_DAILY_SALES: PermissionScope.BRANCH,
    # Undoing a posted economic event is supervisory, exactly as the goods
    # receipt reversal is, and the person who posted should not be the only one
    # who can make it disappear.
    REVERSE_DAILY_SALES: PermissionScope.ORGANIZATION_AUTHORITY,
    MANAGE_SALES_ADJUSTMENTS: PermissionScope.BRANCH,
    VIEW_APPLICATION_RECEIVABLES: PermissionScope.ORGANIZATION_MASTER_DATA,
    # Settling with an application is a statement about the organization's
    # contract with that company, not about one branch's takings, even though
    # the receivable it clears was earned branch by branch.
    MANAGE_APPLICATION_SETTLEMENTS: PermissionScope.ORGANIZATION_AUTHORITY,
    CLOSE_CASHIER_SHIFT: PermissionScope.BRANCH,
    APPROVE_CASHIER_CLOSING: PermissionScope.BRANCH,
    VIEW_SALES_REPORTS: PermissionScope.ORGANIZATION_MASTER_DATA,
    VIEW_SALES_COST: PermissionScope.ORGANIZATION_MASTER_DATA,
}


# --- Which role holds what --------------------------------------------------

_FULL = frozenset(ALL_PERMISSIONS)

#: Runs a branch end to end. Holds the menu, channel and application masters
#: and the whole daily-sales lifecycle, and is still refused the moment it
#: tries to approve its own cashier closing: the separation is between
#: *people*, and both the service and the database check the actor.
#:
#: **No settlement authority.** Agreeing what a delivery company actually
#: remitted against what it owed is a finance act, and a branch manager whose
#: own takings are the thing being reconciled is the wrong person to sign it.
_MANAGER = frozenset(
    {
        VIEW_SALES,
        MANAGE_MENU,
        MANAGE_SALES_CHANNELS,
        MANAGE_DELIVERY_APPLICATIONS,
        MANAGE_SALES_AGREEMENTS,
        MANAGE_SALES_DISCOUNTS,
        CREATE_DAILY_SALES,
        SUBMIT_DAILY_SALES,
        POST_DAILY_SALES,
        MANAGE_SALES_ADJUSTMENTS,
        VIEW_APPLICATION_RECEIVABLES,
        CLOSE_CASHIER_SHIFT,
        APPROVE_CASHIER_CLOSING,
        VIEW_SALES_REPORTS,
        VIEW_SALES_COST,
    }
)

#: Owns the financial side: posting, reversal, receivables, settlements and
#: reconciliation. Also holds `MANAGE_MENU` — not to design the menu, but
#: because a menu item's account mapping and a price's effective date are the
#: things that decide where money lands, and finance carries that.
_ACCOUNTING_MANAGER = frozenset(
    {
        VIEW_SALES,
        MANAGE_MENU,
        MANAGE_SALES_CHANNELS,
        MANAGE_DELIVERY_APPLICATIONS,
        MANAGE_SALES_AGREEMENTS,
        MANAGE_SALES_DISCOUNTS,
        CREATE_DAILY_SALES,
        SUBMIT_DAILY_SALES,
        POST_DAILY_SALES,
        REVERSE_DAILY_SALES,
        MANAGE_SALES_ADJUSTMENTS,
        VIEW_APPLICATION_RECEIVABLES,
        MANAGE_APPLICATION_SETTLEMENTS,
        APPROVE_CASHIER_CLOSING,
        VIEW_SALES_REPORTS,
        VIEW_SALES_COST,
    }
)

#: Prepares and reviews, and signs nothing that needs a second party.
#:
#: Holds `MANAGE_APPLICATION_SETTLEMENTS` because matching a statement line by
#: line **is** the accountant's job, and the control on it is not a second
#: permission — it is that an unexplained variance blocks posting until it is
#: categorised with a reason. No `APPROVE_CASHIER_CLOSING`: an accountant who
#: could approve a count they also reconciled would close the loop on
#: themselves. No `REVERSE_DAILY_SALES`, for the same reason.
_ACCOUNTANT = frozenset(
    {
        VIEW_SALES,
        CREATE_DAILY_SALES,
        SUBMIT_DAILY_SALES,
        MANAGE_SALES_ADJUSTMENTS,
        VIEW_APPLICATION_RECEIVABLES,
        MANAGE_APPLICATION_SETTLEMENTS,
        VIEW_SALES_REPORTS,
        VIEW_SALES_COST,
    }
)

#: The till. Enters the day's sales, opens and closes their own shift, and
#: stops there.
#:
#: **No `POST_DAILY_SALES`**, no adjustments, no discount or agreement master,
#: no settlement authority and no `APPROVE_CASHIER_CLOSING` — a cashier who
#: could approve their own count is a cashier with no control at all. And no
#: `VIEW_SALES_COST`: what a plate costs to make is not information a till
#: needs, and every cost column is omitted rather than blanked.
_CASHIER = frozenset(
    {
        VIEW_SALES,
        CREATE_DAILY_SALES,
        SUBMIT_DAILY_SALES,
        CLOSE_CASHIER_SHIFT,
    }
)

#: Reads the menu and the channels, because a storekeeper legitimately needs to
#: know what the branch sells to understand what its kitchen will demand. No
#: financial authority of any kind, and no cost.
_STOREKEEPER = frozenset({VIEW_SALES})

#: Buying is not selling. A purchaser reads nothing here.
_PURCHASING: frozenset[str] = frozenset()

#: Reads what exists, never what it cost.
_VIEWER = frozenset({VIEW_SALES, VIEW_SALES_REPORTS})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.OWNER.value: _FULL,
    Role.ACCOUNTING_MANAGER.value: _ACCOUNTING_MANAGER,
    Role.MANAGER.value: _MANAGER,
    Role.STOREKEEPER.value: _STOREKEEPER,
    Role.PURCHASING.value: _PURCHASING,
    Role.ACCOUNTANT.value: _ACCOUNTANT,
    Role.CASHIER.value: _CASHIER,
    Role.VIEWER.value: _VIEWER,
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
        raise ValueError(f"{permission} is not a sales permission") from None


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
