"""
The twenty inventory permissions, their scope, and which role holds them.

Eighteen were approved with Task 1.0. The nineteenth, `manage_package_units`,
follows from the amendment that made `PackageUnit` its own model: a model
nobody may administer is a model nobody can populate. The twentieth,
`create_opening_stock`, arrived with the Task 1.3 opening document: preparing
a branch's opening list and *posting* it to the ledger are different acts, and
maker-checker needs them separable.

Identical machinery to `apps/accounting/permissions.py`, and deliberately so:
a permission says *what*, a membership says *where*, and neither alone is
authorization (ADR-016). Inventory adds one scope the accounting module did
not need — **warehouse** — because custody of stock is finer-grained than
authority over a branch.
"""

from __future__ import annotations

from enum import Enum

from apps.organizations.models import Role

APP_LABEL = "inventory"

#: Release 1 policy: the kernel refuses negative stock for everyone, and no
#: code path consults `inventory.override_negative_stock`. The permission code
#: stays reserved so the vocabulary is stable, but while this is False it is
#: granted to **no role by default**, exposed by no command and no UI, and no
#: documentation should imply that any role can currently use it. Turning it
#: on is an architectural decision with its own approval, tests, reason,
#: actor, audit and exception reporting — and it must relax the
#: `stock_balance_quantity_not_negative` constraint in the same migration.
NEGATIVE_STOCK_OVERRIDE_ENABLED = False


class PermissionScope(Enum):
    """
    Where a permission is exercised — and, for the two organization values,
    *how strongly*.

    `ORGANIZATION_MASTER_DATA` needs the caller to **reach** the organization
    (an organization membership, or a branch of it) and hold the permission.
    A branch manager legitimately maintains the shared item master.

    `ORGANIZATION_AUTHORITY` needs an actual `OrganizationMembership`. It is
    for acts over state that has no branch — setting the ledger's starting
    point, overriding the negative-stock rule — where authority over one
    branch is authority over a part of something that has no parts.

    Collapsing the two would force every deployment to choose between locking
    branch managers out of the item master and handing them period-level
    authority as a side effect.
    """

    ORGANIZATION_MASTER_DATA = "ORGANIZATION_MASTER_DATA"
    ORGANIZATION_AUTHORITY = "ORGANIZATION_AUTHORITY"
    BRANCH = "BRANCH"
    WAREHOUSE = "WAREHOUSE"


# --- The twenty -----------------------------------------------------------

VIEW_ITEM = f"{APP_LABEL}.view_item"
MANAGE_CATEGORIES = f"{APP_LABEL}.manage_categories"
MANAGE_PACKAGE_UNITS = f"{APP_LABEL}.manage_package_units"
MANAGE_ITEMS = f"{APP_LABEL}.manage_items"
MANAGE_CONVERSIONS = f"{APP_LABEL}.manage_conversions"
MANAGE_WAREHOUSES = f"{APP_LABEL}.manage_warehouses"
VIEW_STOCK = f"{APP_LABEL}.view_stock"
VIEW_VALUATION = f"{APP_LABEL}.view_valuation"
CREATE_DRAFT_MOVEMENT = f"{APP_LABEL}.create_draft_movement"
CREATE_OPENING_STOCK = f"{APP_LABEL}.create_opening_stock"
POST_OPENING_STOCK = f"{APP_LABEL}.post_opening_stock"
POST_RECEIPT = f"{APP_LABEL}.post_receipt"
POST_ISSUE = f"{APP_LABEL}.post_issue"
POST_RETURN_IN = f"{APP_LABEL}.post_return_in"
POST_TRANSFER = f"{APP_LABEL}.post_transfer"
POST_WASTE = f"{APP_LABEL}.post_waste"
CONDUCT_STOCK_COUNT = f"{APP_LABEL}.conduct_stock_count"
APPROVE_STOCK_COUNT = f"{APP_LABEL}.approve_stock_count"
POST_ADJUSTMENT = f"{APP_LABEL}.post_adjustment"
REVERSE_MOVEMENT = f"{APP_LABEL}.reverse_movement"
OVERRIDE_NEGATIVE_STOCK = f"{APP_LABEL}.override_negative_stock"

ALL_PERMISSIONS: tuple[str, ...] = (
    VIEW_ITEM,
    MANAGE_CATEGORIES,
    MANAGE_PACKAGE_UNITS,
    MANAGE_ITEMS,
    MANAGE_CONVERSIONS,
    MANAGE_WAREHOUSES,
    VIEW_STOCK,
    VIEW_VALUATION,
    CREATE_DRAFT_MOVEMENT,
    CREATE_OPENING_STOCK,
    POST_OPENING_STOCK,
    POST_RECEIPT,
    POST_ISSUE,
    POST_RETURN_IN,
    POST_TRANSFER,
    POST_WASTE,
    CONDUCT_STOCK_COUNT,
    APPROVE_STOCK_COUNT,
    POST_ADJUSTMENT,
    REVERSE_MOVEMENT,
    OVERRIDE_NEGATIVE_STOCK,
)


PERMISSION_SCOPE: dict[str, PermissionScope] = {
    # The item master, the categories, the packages, and the conversions are
    # organization property (ADR-014's reasoning, applied to items): one
    # branch must not reshape what the others stock.
    VIEW_ITEM: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_CATEGORIES: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_PACKAGE_UNITS: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_ITEMS: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_CONVERSIONS: PermissionScope.ORGANIZATION_MASTER_DATA,
    # Custody structures and figures belong to the branch.
    MANAGE_WAREHOUSES: PermissionScope.BRANCH,
    VIEW_STOCK: PermissionScope.BRANCH,
    VIEW_VALUATION: PermissionScope.BRANCH,
    CREATE_DRAFT_MOVEMENT: PermissionScope.BRANCH,
    # Preparing a branch's opening list is branch work; POSTING it is the
    # organization-authority act. The split is what lets a branch manager
    # prepare and submit while only accounting authority sets the ledger's
    # starting point.
    CREATE_OPENING_STOCK: PermissionScope.BRANCH,
    APPROVE_STOCK_COUNT: PermissionScope.BRANCH,
    POST_ADJUSTMENT: PermissionScope.BRANCH,
    REVERSE_MOVEMENT: PermissionScope.BRANCH,
    # A movement names a warehouse, so posting is answered per warehouse.
    POST_RECEIPT: PermissionScope.WAREHOUSE,
    POST_ISSUE: PermissionScope.WAREHOUSE,
    POST_RETURN_IN: PermissionScope.WAREHOUSE,
    POST_TRANSFER: PermissionScope.WAREHOUSE,
    POST_WASTE: PermissionScope.WAREHOUSE,
    CONDUCT_STOCK_COUNT: PermissionScope.WAREHOUSE,
    # Setting the ledger's starting point, and overriding its central rule,
    # are organization-level accounting decisions.
    POST_OPENING_STOCK: PermissionScope.ORGANIZATION_AUTHORITY,
    OVERRIDE_NEGATIVE_STOCK: PermissionScope.ORGANIZATION_AUTHORITY,
}


# --- Which role holds what ------------------------------------------------

#: Everything except the disabled override. While
#: `NEGATIVE_STOCK_OVERRIDE_ENABLED` is False the permission is granted to no
#: role at all — a default grant of a non-operational authority would let a
#: role *look* like it can do something the kernel refuses for everyone, and
#: the moment the feature is enabled the grant would spring to life without
#: anyone deciding it should.
_FULL = frozenset(ALL_PERMISSIONS) - {OVERRIDE_NEGATIVE_STOCK}

#: Sees and approves everything; performs no warehouse operations. No
#: receipt, issue, transfer, or count — they answer for the figures, they do
#: not move the goods. Prepares and posts openings, subject to maker-checker:
#: whichever accounting manager submitted a document, a different user posts it.
_ACCOUNTING_MANAGER = frozenset(
    {
        VIEW_ITEM,
        VIEW_STOCK,
        VIEW_VALUATION,
        CREATE_OPENING_STOCK,
        POST_OPENING_STOCK,
        APPROVE_STOCK_COUNT,
        POST_ADJUSTMENT,
        REVERSE_MOVEMENT,
    }
)

#: Runs a branch end to end. Holds both conduct and approve for a count, and
#: is still refused when approving their own — maker-checker is enforced on
#: the act, not on the permission. Prepares and submits the branch's opening
#: document; posting it is accounting authority and is deliberately absent.
_MANAGER = frozenset(
    {
        VIEW_ITEM,
        MANAGE_CATEGORIES,
        MANAGE_PACKAGE_UNITS,
        MANAGE_ITEMS,
        MANAGE_CONVERSIONS,
        MANAGE_WAREHOUSES,
        VIEW_STOCK,
        VIEW_VALUATION,
        CREATE_DRAFT_MOVEMENT,
        CREATE_OPENING_STOCK,
        POST_RECEIPT,
        POST_ISSUE,
        POST_RETURN_IN,
        POST_TRANSFER,
        POST_WASTE,
        CONDUCT_STOCK_COUNT,
        APPROVE_STOCK_COUNT,
        POST_ADJUSTMENT,
        REVERSE_MOVEMENT,
    }
)

#: Moves goods. Deliberately **cannot see cost**, cannot approve their own
#: count, and cannot waste, adjust, reverse, or override.
#:
#: `post_return_in` sits here because returning unused stock to the shelf is
#: the same custody act as issuing it, done backwards — the storekeeper who
#: handed the goods out is the one who takes them back. It carries no cost
#: decision at all: a return is valued from the issue it reverses.
_STOREKEEPER = frozenset(
    {
        VIEW_ITEM,
        VIEW_STOCK,
        CREATE_DRAFT_MOVEMENT,
        POST_RECEIPT,
        POST_ISSUE,
        POST_RETURN_IN,
        POST_TRANSFER,
        CONDUCT_STOCK_COUNT,
    }
)

#: Chooses suppliers and needs to see cost. Given no master-data mutation and
#: no receipt posting: whoever chose the supplier should not also confirm what
#: arrived.
_PURCHASING = frozenset({VIEW_ITEM, VIEW_STOCK, VIEW_VALUATION})

#: Reads the figures. Posting opening stock is Accounting Manager authority —
#: it sets the ledger's starting point.
_ACCOUNTANT = frozenset({VIEW_ITEM, VIEW_STOCK, VIEW_VALUATION})

#: Reads what exists, never what it cost.
_VIEWER = frozenset({VIEW_ITEM, VIEW_STOCK})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.OWNER.value: _FULL,
    Role.ACCOUNTING_MANAGER.value: _ACCOUNTING_MANAGER,
    Role.MANAGER.value: _MANAGER,
    Role.STOREKEEPER.value: _STOREKEEPER,
    Role.PURCHASING.value: _PURCHASING,
    Role.ACCOUNTANT.value: _ACCOUNTANT,
    Role.VIEWER.value: _VIEWER,
    # A cashier handles takings, not stock.
    Role.CASHIER.value: frozenset(),
}


def permissions_for_role(role: Role | str) -> frozenset[str]:
    """The inventory permissions a role carries. Unknown roles carry none."""
    value = role.value if isinstance(role, Role) else str(role)
    return ROLE_PERMISSIONS.get(value, frozenset())


def scope_of(permission: str) -> PermissionScope:
    """Whether a permission is exercised over an organization, branch, or warehouse."""
    try:
        return PERMISSION_SCOPE[permission]
    except KeyError:  # pragma: no cover - a typo in a caller, not a state
        raise ValueError(f"{permission} is not an inventory permission") from None


def sync_role_groups() -> None:
    """
    Write `ROLE_PERMISSIONS` into the role groups.

    Replaces each role's *inventory* permissions and leaves every other app's
    alone, so this and `apps.accounting`'s equivalent can both run without
    trampling each other.
    """
    from django.contrib.auth.models import Permission

    from apps.organizations.permissions import group_for_role

    known: dict[str, Permission] = {
        f"{APP_LABEL}.{permission.codename}": permission
        for permission in Permission.objects.filter(content_type__app_label=APP_LABEL)
    }

    missing = [name for name in ALL_PERMISSIONS if name not in known]
    if missing:
        raise LookupError(f"inventory permissions are not migrated: {sorted(missing)}")

    ours = set(known.values())
    for role, permission_names in ROLE_PERMISSIONS.items():
        group = group_for_role(role)
        wanted = {known[name] for name in permission_names}
        group.permissions.remove(*(ours - wanted))
        group.permissions.add(*wanted)
