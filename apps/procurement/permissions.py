"""
The procurement permissions, their scope, and which role holds them.

Identical machinery to `apps/inventory/permissions.py` and
`apps/accounting/permissions.py`, deliberately: a permission says *what*, a
membership says *where*, and neither alone is authorization (ADR-016).

Task 2.1 declares three. The rest arrive with the documents they guard, one
task at a time, because a permission for a workflow that does not exist is a
grant nobody can audit — the mistake `import_opening_draft` records in
inventory.

The scope split that matters here is between **stock** and **money**. Receipt
and return permissions will be warehouse-scoped when they arrive, because they
move goods and inventory already scopes custody that way. Invoice, credit note
and payment permissions are organization-scoped, because money is not stored in
a warehouse and a branch manager's authority over one branch is not authority
over what the organization owes.

`view_supplier_cost` is separate from `view_supplier` for the reason inventory
separates `view_valuation` from `view_stock`: a storekeeper receiving goods
needs the supplier's name, the item and the quantity, and has no business
seeing what was paid. The price column is **omitted, not blanked** — a blanked
column tells the reader a number exists and that they are not trusted with it,
which is a different statement from the one intended.
"""

from __future__ import annotations

from enum import Enum

from apps.organizations.models import Role

APP_LABEL = "procurement"


class PermissionScope(Enum):
    """
    Where a procurement permission is exercised.

    The same four values inventory uses, and the same meanings.
    `ORGANIZATION_MASTER_DATA` needs the caller to *reach* the organization —
    a branch membership is enough, because a branch manager legitimately
    maintains the shared supplier master. `ORGANIZATION_AUTHORITY` needs a real
    `OrganizationMembership`, and is reserved for acts over money the
    organization owes.
    """

    ORGANIZATION_MASTER_DATA = "ORGANIZATION_MASTER_DATA"
    ORGANIZATION_AUTHORITY = "ORGANIZATION_AUTHORITY"
    BRANCH = "BRANCH"
    WAREHOUSE = "WAREHOUSE"


# --- The three --------------------------------------------------------------

VIEW_SUPPLIER = f"{APP_LABEL}.view_supplier"
MANAGE_SUPPLIERS = f"{APP_LABEL}.manage_suppliers"
VIEW_SUPPLIER_COST = f"{APP_LABEL}.view_supplier_cost"
#: Task 2.2. Reading the catalogue is reading who supplies what — the
#: prices on it are guarded separately by `view_supplier_cost`, exactly as
#: stock quantity and stock valuation are guarded separately in inventory.
VIEW_SUPPLIER_ITEM = f"{APP_LABEL}.view_supplieritem"
MANAGE_SUPPLIER_ITEMS = f"{APP_LABEL}.manage_supplier_items"
#: Task 2.3. Preparing a request and deciding one are different acts, held
#: by different people — the whole point of maker-checker. Both are
#: **branch**-scoped: a request names a branch warehouse, and authority over
#: one branch is not authority over another.
VIEW_PURCHASE_REQUEST = f"{APP_LABEL}.view_purchaserequest"
CREATE_PURCHASE_REQUEST = f"{APP_LABEL}.create_purchase_request"
APPROVE_PURCHASE_REQUEST = f"{APP_LABEL}.approve_purchase_request"

ALL_PERMISSIONS: tuple[str, ...] = (
    VIEW_SUPPLIER,
    MANAGE_SUPPLIERS,
    VIEW_SUPPLIER_COST,
    VIEW_SUPPLIER_ITEM,
    MANAGE_SUPPLIER_ITEMS,
    VIEW_PURCHASE_REQUEST,
    CREATE_PURCHASE_REQUEST,
    APPROVE_PURCHASE_REQUEST,
)

PERMISSION_SCOPE: dict[str, PermissionScope] = {
    # The supplier master is organization property. One branch must not
    # reshape who the group buys from.
    VIEW_SUPPLIER: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_SUPPLIERS: PermissionScope.ORGANIZATION_MASTER_DATA,
    # Commercial terms are organization-level information: a branch manager who
    # can reach the organization can be trusted with prices, and a storekeeper
    # who cannot is refused at the same boundary they are refused inventory
    # valuation.
    VIEW_SUPPLIER_COST: PermissionScope.ORGANIZATION_MASTER_DATA,
    # The catalogue is organization master data for the same reason the
    # supplier list is: it says what the group buys and from whom.
    VIEW_SUPPLIER_ITEM: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_SUPPLIER_ITEMS: PermissionScope.ORGANIZATION_MASTER_DATA,
    # A request belongs to a branch and names one of its warehouses.
    VIEW_PURCHASE_REQUEST: PermissionScope.BRANCH,
    CREATE_PURCHASE_REQUEST: PermissionScope.BRANCH,
    APPROVE_PURCHASE_REQUEST: PermissionScope.BRANCH,
}


# --- Which role holds what --------------------------------------------------

_FULL = frozenset(ALL_PERMISSIONS)

#: Chooses suppliers and needs to see what they charge. Maintains the master,
#: because deciding who the organization buys from is the substance of the
#: role. Receipt posting will deliberately **not** come here when it arrives:
#: whoever chose the supplier should not also confirm what arrived.
_PURCHASING = frozenset(
    {
        VIEW_SUPPLIER,
        MANAGE_SUPPLIERS,
        VIEW_SUPPLIER_COST,
        VIEW_SUPPLIER_ITEM,
        MANAGE_SUPPLIER_ITEMS,
        VIEW_PURCHASE_REQUEST,
        CREATE_PURCHASE_REQUEST,
    }
)

#: Runs a branch end to end, including its buying.
_MANAGER = frozenset(
    {
        VIEW_SUPPLIER,
        MANAGE_SUPPLIERS,
        VIEW_SUPPLIER_COST,
        VIEW_SUPPLIER_ITEM,
        MANAGE_SUPPLIER_ITEMS,
        VIEW_PURCHASE_REQUEST,
        CREATE_PURCHASE_REQUEST,
        APPROVE_PURCHASE_REQUEST,
    }
)

#: Answers for the figures. Reads the master and the money; maintains neither —
#: inventing a supplier is a purchasing act, not an accounting one.
_ACCOUNTING_MANAGER = frozenset(
    {
        VIEW_SUPPLIER,
        VIEW_SUPPLIER_COST,
        VIEW_SUPPLIER_ITEM,
        VIEW_PURCHASE_REQUEST,
        # Approves what a branch asks for without being able to ask for it,
        # which is the separation the whole document exists to record.
        APPROVE_PURCHASE_REQUEST,
    }
)
_ACCOUNTANT = frozenset(
    {VIEW_SUPPLIER, VIEW_SUPPLIER_COST, VIEW_SUPPLIER_ITEM, VIEW_PURCHASE_REQUEST}
)

#: Receives goods against a supplier's delivery note, and has no business
#: seeing what was paid for them.
#: Receives goods against a delivery note, so needs to know which supplier
#: sends which item in which package — and still never what it cost.
_STOREKEEPER = frozenset(
    {
        VIEW_SUPPLIER,
        VIEW_SUPPLIER_ITEM,
        # Asks for what the store is running out of. Cannot approve it.
        VIEW_PURCHASE_REQUEST,
        CREATE_PURCHASE_REQUEST,
    }
)

#: Reads what exists, never what it cost.
_VIEWER = frozenset({VIEW_SUPPLIER, VIEW_PURCHASE_REQUEST})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.OWNER.value: _FULL,
    Role.ACCOUNTING_MANAGER.value: _ACCOUNTING_MANAGER,
    Role.MANAGER.value: _MANAGER,
    Role.STOREKEEPER.value: _STOREKEEPER,
    Role.PURCHASING.value: _PURCHASING,
    Role.ACCOUNTANT.value: _ACCOUNTANT,
    Role.VIEWER.value: _VIEWER,
    # A cashier handles takings, not purchasing.
    Role.CASHIER.value: frozenset(),
}


def permissions_for_role(role: Role | str) -> frozenset[str]:
    """The procurement permissions a role carries. Unknown roles carry none."""
    value = role.value if isinstance(role, Role) else str(role)
    return ROLE_PERMISSIONS.get(value, frozenset())


def scope_of(permission: str) -> PermissionScope:
    """Whether a permission is exercised over an organization, branch, or warehouse."""
    try:
        return PERMISSION_SCOPE[permission]
    except KeyError:  # pragma: no cover - a typo in a caller, not a state
        raise ValueError(f"{permission} is not a procurement permission") from None


def sync_role_groups() -> None:
    """
    Write `ROLE_PERMISSIONS` into the role groups.

    Replaces each role's *procurement* permissions and leaves every other
    app's alone, so this and the inventory and accounting equivalents can all
    run without trampling each other.
    """
    from django.contrib.auth.models import Permission

    from apps.organizations.permissions import group_for_role

    known: dict[str, Permission] = {
        f"{APP_LABEL}.{permission.codename}": permission
        for permission in Permission.objects.filter(content_type__app_label=APP_LABEL)
    }

    missing = [name for name in ALL_PERMISSIONS if name not in known]
    if missing:
        raise LookupError(f"procurement permissions are not migrated: {sorted(missing)}")

    ours = set(known.values())
    for role, permission_names in ROLE_PERMISSIONS.items():
        group = group_for_role(role)
        wanted = {known[name] for name in permission_names}
        group.permissions.remove(*(ours - wanted))
        group.permissions.add(*wanted)
