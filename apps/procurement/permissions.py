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
#: Task 2.4. Recording an offer and awarding one are separate: the first is
#: data entry, the second is the decision the whole comparison exists to
#: record. Both are organization master data — a quotation is priced for
#: the group, not for one branch.
VIEW_QUOTATION = f"{APP_LABEL}.view_supplierquotation"
MANAGE_QUOTATIONS = f"{APP_LABEL}.manage_quotations"
AWARD_QUOTATION = f"{APP_LABEL}.award_quotation"
#: Task 2.6. Four permissions for one document, because they are four
#: decisions: prepare it, agree to spend the money, send it to the
#: supplier, and withdraw it. A deployment that trusts a buyer to draft an
#: order without trusting them to commit the business to it must be able to
#: say so, and one permission could not.
VIEW_PURCHASE_ORDER = f"{APP_LABEL}.view_purchaseorder"
CREATE_PURCHASE_ORDER = f"{APP_LABEL}.create_purchase_order"
APPROVE_PURCHASE_ORDER = f"{APP_LABEL}.approve_purchase_order"
ISSUE_PURCHASE_ORDER = f"{APP_LABEL}.issue_purchase_order"
CANCEL_PURCHASE_ORDER = f"{APP_LABEL}.cancel_purchase_order"
#: Task 2.8. Recording what arrived and deciding what is acceptable are
#: different acts, so they are different permissions: the person unloading
#: the lorry writes down twelve boxes, and somebody else says three of them
#: are off. Posting and reversing are elevated again, because those are the
#: two that touch the ledger.
#:
#: All four are **warehouse**-scoped: a receipt names a warehouse and moves
#: stock into it, and inventory already scopes custody that way.
VIEW_GOODS_RECEIPT = f"{APP_LABEL}.view_goodsreceipt"
CREATE_GOODS_RECEIPT = f"{APP_LABEL}.create_goods_receipt"
INSPECT_GOODS_RECEIPT = f"{APP_LABEL}.inspect_goods_receipt"
POST_GOODS_RECEIPT = f"{APP_LABEL}.post_goods_receipt"
REVERSE_GOODS_RECEIPT = f"{APP_LABEL}.reverse_goods_receipt"
#: Task 2.10. Five, because entering a claim, agreeing it is real, putting it
#: in the ledger and taking it back out are four different acts by up to four
#: different people — the separation the whole approval step exists to record.
#:
#: All five are **organization**-scoped and, unlike the supplier master,
#: require real organization authority rather than mere reach: an invoice is
#: money the organization owes, and a branch membership is custody of a store
#: rather than authority over a debt (PRC-060).
VIEW_SUPPLIER_INVOICE = f"{APP_LABEL}.view_supplierinvoice"
CREATE_SUPPLIER_INVOICE = f"{APP_LABEL}.create_supplier_invoice"
APPROVE_SUPPLIER_INVOICE = f"{APP_LABEL}.approve_supplier_invoice"
POST_SUPPLIER_INVOICE = f"{APP_LABEL}.post_supplier_invoice"
REVERSE_SUPPLIER_INVOICE = f"{APP_LABEL}.reverse_supplier_invoice"

ALL_PERMISSIONS: tuple[str, ...] = (
    VIEW_SUPPLIER,
    MANAGE_SUPPLIERS,
    VIEW_SUPPLIER_COST,
    VIEW_SUPPLIER_ITEM,
    MANAGE_SUPPLIER_ITEMS,
    VIEW_PURCHASE_REQUEST,
    CREATE_PURCHASE_REQUEST,
    APPROVE_PURCHASE_REQUEST,
    VIEW_QUOTATION,
    MANAGE_QUOTATIONS,
    AWARD_QUOTATION,
    VIEW_PURCHASE_ORDER,
    CREATE_PURCHASE_ORDER,
    APPROVE_PURCHASE_ORDER,
    ISSUE_PURCHASE_ORDER,
    CANCEL_PURCHASE_ORDER,
    VIEW_GOODS_RECEIPT,
    CREATE_GOODS_RECEIPT,
    INSPECT_GOODS_RECEIPT,
    POST_GOODS_RECEIPT,
    REVERSE_GOODS_RECEIPT,
    VIEW_SUPPLIER_INVOICE,
    CREATE_SUPPLIER_INVOICE,
    APPROVE_SUPPLIER_INVOICE,
    POST_SUPPLIER_INVOICE,
    REVERSE_SUPPLIER_INVOICE,
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
    VIEW_QUOTATION: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_QUOTATIONS: PermissionScope.ORGANIZATION_MASTER_DATA,
    AWARD_QUOTATION: PermissionScope.ORGANIZATION_MASTER_DATA,
    # An order names a branch warehouse and is answered at that branch.
    VIEW_PURCHASE_ORDER: PermissionScope.BRANCH,
    CREATE_PURCHASE_ORDER: PermissionScope.BRANCH,
    APPROVE_PURCHASE_ORDER: PermissionScope.BRANCH,
    ISSUE_PURCHASE_ORDER: PermissionScope.BRANCH,
    CANCEL_PURCHASE_ORDER: PermissionScope.BRANCH,
    # Custody of one warehouse's contents.
    VIEW_GOODS_RECEIPT: PermissionScope.WAREHOUSE,
    CREATE_GOODS_RECEIPT: PermissionScope.WAREHOUSE,
    INSPECT_GOODS_RECEIPT: PermissionScope.WAREHOUSE,
    POST_GOODS_RECEIPT: PermissionScope.WAREHOUSE,
    REVERSE_GOODS_RECEIPT: PermissionScope.WAREHOUSE,
    # Money the organization owes. `ORGANIZATION_AUTHORITY` rather than
    # `ORGANIZATION_MASTER_DATA`: reaching an organization through a branch
    # membership is enough to maintain the shared supplier list, and is not
    # enough to commit the organization to paying somebody.
    VIEW_SUPPLIER_INVOICE: PermissionScope.ORGANIZATION_AUTHORITY,
    CREATE_SUPPLIER_INVOICE: PermissionScope.ORGANIZATION_AUTHORITY,
    APPROVE_SUPPLIER_INVOICE: PermissionScope.ORGANIZATION_AUTHORITY,
    POST_SUPPLIER_INVOICE: PermissionScope.ORGANIZATION_AUTHORITY,
    REVERSE_SUPPLIER_INVOICE: PermissionScope.ORGANIZATION_AUTHORITY,
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
        VIEW_QUOTATION,
        MANAGE_QUOTATIONS,
        AWARD_QUOTATION,
        VIEW_PURCHASE_ORDER,
        CREATE_PURCHASE_ORDER,
        # Sends the agreed order out, and deliberately cannot approve it:
        # whoever chose the supplier does not also authorise the spend.
        ISSUE_PURCHASE_ORDER,
        # Reads deliveries; confirms none. Whoever chose the supplier does
        # not also certify that what they sent was acceptable.
        VIEW_GOODS_RECEIPT,
        # Reads what was billed against the orders it raised, and agrees to
        # none of it. Whoever chose the supplier does not approve their bill.
        VIEW_SUPPLIER_INVOICE,
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
        VIEW_QUOTATION,
        MANAGE_QUOTATIONS,
        AWARD_QUOTATION,
        VIEW_PURCHASE_ORDER,
        CREATE_PURCHASE_ORDER,
        APPROVE_PURCHASE_ORDER,
        ISSUE_PURCHASE_ORDER,
        CANCEL_PURCHASE_ORDER,
        VIEW_GOODS_RECEIPT,
        CREATE_GOODS_RECEIPT,
        INSPECT_GOODS_RECEIPT,
        POST_GOODS_RECEIPT,
        REVERSE_GOODS_RECEIPT,
        # Held, but organization-scoped: a branch manager with no organization
        # membership reaches no invoice, which is the scope doing its job
        # rather than the permission being pointless.
        VIEW_SUPPLIER_INVOICE,
        CREATE_SUPPLIER_INVOICE,
    }
)

#: Answers for the figures. Reads the master and the money; maintains neither —
#: inventing a supplier is a purchasing act, not an accounting one.
_ACCOUNTING_MANAGER = frozenset(
    {
        VIEW_SUPPLIER,
        VIEW_SUPPLIER_COST,
        VIEW_SUPPLIER_ITEM,
        VIEW_QUOTATION,
        VIEW_PURCHASE_REQUEST,
        VIEW_PURCHASE_ORDER,
        VIEW_GOODS_RECEIPT,
        # Undoing a posted receipt is an accounting act, not a warehouse
        # one: it reverses a journal as well as stock.
        REVERSE_GOODS_RECEIPT,
        # Authorises the spend without being able to raise the order.
        APPROVE_PURCHASE_ORDER,
        CANCEL_PURCHASE_ORDER,
        # Approves what a branch asks for without being able to ask for it,
        # which is the separation the whole document exists to record.
        APPROVE_PURCHASE_REQUEST,
        # The money side of a purchase, end to end — and deliberately without
        # `CREATE_SUPPLIER_INVOICE`. Whoever agrees a claim is real should not
        # be the person who typed it in.
        VIEW_SUPPLIER_INVOICE,
        APPROVE_SUPPLIER_INVOICE,
        POST_SUPPLIER_INVOICE,
        REVERSE_SUPPLIER_INVOICE,
    }
)
_ACCOUNTANT = frozenset(
    {
        VIEW_SUPPLIER,
        VIEW_SUPPLIER_COST,
        VIEW_SUPPLIER_ITEM,
        VIEW_PURCHASE_REQUEST,
        VIEW_QUOTATION,
        VIEW_PURCHASE_ORDER,
        VIEW_GOODS_RECEIPT,
        # Enters what arrives in the post, and agrees to none of it. The
        # maker-checker split the purchase request already uses, applied to
        # the document that actually commits money.
        VIEW_SUPPLIER_INVOICE,
        CREATE_SUPPLIER_INVOICE,
    }
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
        # Sees what is on order so a delivery can be checked against it.
        VIEW_PURCHASE_ORDER,
        VIEW_GOODS_RECEIPT,
        CREATE_GOODS_RECEIPT,
        INSPECT_GOODS_RECEIPT,
        POST_GOODS_RECEIPT,
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
