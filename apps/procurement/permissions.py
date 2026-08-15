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

#: Task 2.13. Warehouse-scoped, all four, because a return moves stock and
#: inventory already scopes custody that way (PRC-060) — the same reasoning
#: that put the receipt's permissions there rather than on the organization.
#:
#: Posting and reversing are separate, and the split matches the receipt's:
#: a storekeeper sends goods back, and undoing a posted movement is elevated
#: and belongs to whoever answers for the warehouse's figures.
VIEW_SUPPLIER_RETURN = f"{APP_LABEL}.view_supplierreturn"
CREATE_SUPPLIER_RETURN = f"{APP_LABEL}.create_supplier_return"
POST_SUPPLIER_RETURN = f"{APP_LABEL}.post_supplier_return"
REVERSE_SUPPLIER_RETURN = f"{APP_LABEL}.reverse_supplier_return"
#: Task 2.10. Five, because entering a claim, agreeing it is real, putting it
#: in the ledger and taking it back out are four different acts by up to four
#: different people — the separation the whole approval step exists to record.
#:
#: All five are **organization**-scoped and, unlike the supplier master,
#: require real organization authority rather than mere reach: an invoice is
#: money the organization owes, and a branch membership is custody of a store
#: rather than authority over a debt (PRC-060).
#: Task 2.11. Matching decides what covers what; it reaches no ledger, so
#: `match_supplier_invoice` is the working permission and `cancel` is the
#: separate one, because withdrawing an agreed answer somebody may already
#: have acted on is a different act from proposing one. Both are
#: organization-scoped per Task 2.0 §13.
VIEW_PURCHASE_MATCH = f"{APP_LABEL}.view_purchasematch"
MATCH_SUPPLIER_INVOICE = f"{APP_LABEL}.match_supplier_invoice"
CANCEL_PURCHASE_MATCH = f"{APP_LABEL}.cancel_purchase_match"
VIEW_SUPPLIER_INVOICE = f"{APP_LABEL}.view_supplierinvoice"
CREATE_SUPPLIER_INVOICE = f"{APP_LABEL}.create_supplier_invoice"
APPROVE_SUPPLIER_INVOICE = f"{APP_LABEL}.approve_supplier_invoice"
POST_SUPPLIER_INVOICE = f"{APP_LABEL}.post_supplier_invoice"
REVERSE_SUPPLIER_INVOICE = f"{APP_LABEL}.reverse_supplier_invoice"
#: Task 2.14. Organization-scoped, all four, because a credit note is money —
#: it moves the payable and settles a claim — and money is not stored in a
#: warehouse (PRC-060). §13 named `post_supplier_credit_note` alone; the three
#: companions follow the invoice's amendment precedent, and the maker-checker
#: split follows it too: whoever agrees a credit is real should not be the
#: person who typed it in.
VIEW_SUPPLIER_CREDIT_NOTE = f"{APP_LABEL}.view_suppliercreditnote"
CREATE_SUPPLIER_CREDIT_NOTE = f"{APP_LABEL}.create_supplier_credit_note"
POST_SUPPLIER_CREDIT_NOTE = f"{APP_LABEL}.post_supplier_credit_note"
REVERSE_SUPPLIER_CREDIT_NOTE = f"{APP_LABEL}.reverse_supplier_credit_note"
#: Task 2.15. Organization-scoped, all four — money leaves (PRC-060) — with
#: the invoice's maker-checker split: whoever lets the money go should not be
#: the person who typed the payment in. §13 named `post_supplier_payment`
#: alone; the three companions follow the standing amendment precedent.
VIEW_SUPPLIER_PAYMENT = f"{APP_LABEL}.view_supplierpayment"
CREATE_SUPPLIER_PAYMENT = f"{APP_LABEL}.create_supplier_payment"
POST_SUPPLIER_PAYMENT = f"{APP_LABEL}.post_supplier_payment"
REVERSE_SUPPLIER_PAYMENT = f"{APP_LABEL}.reverse_supplier_payment"
#: Task 2.16. One permission for the whole report family (§13), organization
#: scoped: a report aggregates money across branches, and cost columns are
#: separately gated by `view_supplier_cost` exactly as the screens gate them.
VIEW_PROCUREMENT_REPORT = f"{APP_LABEL}.view_procurement_report"

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
    VIEW_SUPPLIER_RETURN,
    CREATE_SUPPLIER_RETURN,
    POST_SUPPLIER_RETURN,
    REVERSE_SUPPLIER_RETURN,
    VIEW_SUPPLIER_INVOICE,
    CREATE_SUPPLIER_INVOICE,
    APPROVE_SUPPLIER_INVOICE,
    POST_SUPPLIER_INVOICE,
    REVERSE_SUPPLIER_INVOICE,
    VIEW_PURCHASE_MATCH,
    MATCH_SUPPLIER_INVOICE,
    CANCEL_PURCHASE_MATCH,
    VIEW_SUPPLIER_CREDIT_NOTE,
    CREATE_SUPPLIER_CREDIT_NOTE,
    POST_SUPPLIER_CREDIT_NOTE,
    REVERSE_SUPPLIER_CREDIT_NOTE,
    VIEW_SUPPLIER_PAYMENT,
    CREATE_SUPPLIER_PAYMENT,
    POST_SUPPLIER_PAYMENT,
    REVERSE_SUPPLIER_PAYMENT,
    VIEW_PROCUREMENT_REPORT,
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
    VIEW_SUPPLIER_RETURN: PermissionScope.WAREHOUSE,
    CREATE_SUPPLIER_RETURN: PermissionScope.WAREHOUSE,
    POST_SUPPLIER_RETURN: PermissionScope.WAREHOUSE,
    REVERSE_SUPPLIER_RETURN: PermissionScope.WAREHOUSE,
    # Money the organization owes. `ORGANIZATION_AUTHORITY` rather than
    # `ORGANIZATION_MASTER_DATA`: reaching an organization through a branch
    # membership is enough to maintain the shared supplier list, and is not
    # enough to commit the organization to paying somebody.
    VIEW_SUPPLIER_INVOICE: PermissionScope.ORGANIZATION_AUTHORITY,
    CREATE_SUPPLIER_INVOICE: PermissionScope.ORGANIZATION_AUTHORITY,
    APPROVE_SUPPLIER_INVOICE: PermissionScope.ORGANIZATION_AUTHORITY,
    POST_SUPPLIER_INVOICE: PermissionScope.ORGANIZATION_AUTHORITY,
    REVERSE_SUPPLIER_INVOICE: PermissionScope.ORGANIZATION_AUTHORITY,
    # Matching reconciles a debt against deliveries. It posts nothing, but it
    # decides what a later posting will be built on, so it sits with the
    # money permissions rather than with warehouse custody (PRC-060).
    VIEW_PURCHASE_MATCH: PermissionScope.ORGANIZATION_AUTHORITY,
    MATCH_SUPPLIER_INVOICE: PermissionScope.ORGANIZATION_AUTHORITY,
    CANCEL_PURCHASE_MATCH: PermissionScope.ORGANIZATION_AUTHORITY,
    # A credit note moves the payable. Money, so the invoice's scope.
    VIEW_SUPPLIER_CREDIT_NOTE: PermissionScope.ORGANIZATION_AUTHORITY,
    CREATE_SUPPLIER_CREDIT_NOTE: PermissionScope.ORGANIZATION_AUTHORITY,
    POST_SUPPLIER_CREDIT_NOTE: PermissionScope.ORGANIZATION_AUTHORITY,
    REVERSE_SUPPLIER_CREDIT_NOTE: PermissionScope.ORGANIZATION_AUTHORITY,
    # A payment moves cash out. Money leaves (PRC-060).
    VIEW_SUPPLIER_PAYMENT: PermissionScope.ORGANIZATION_AUTHORITY,
    CREATE_SUPPLIER_PAYMENT: PermissionScope.ORGANIZATION_AUTHORITY,
    POST_SUPPLIER_PAYMENT: PermissionScope.ORGANIZATION_AUTHORITY,
    REVERSE_SUPPLIER_PAYMENT: PermissionScope.ORGANIZATION_AUTHORITY,
    # Reports aggregate money across branches.
    VIEW_PROCUREMENT_REPORT: PermissionScope.ORGANIZATION_AUTHORITY,
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
        # Spend and price-variance reports are the buyer's working tools.
        VIEW_PROCUREMENT_REPORT,
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
        VIEW_SUPPLIER_RETURN,
        CREATE_SUPPLIER_RETURN,
        POST_SUPPLIER_RETURN,
        REVERSE_SUPPLIER_RETURN,
        # Held, but organization-scoped: a branch manager with no organization
        # membership reaches no invoice, which is the scope doing its job
        # rather than the permission being pointless.
        VIEW_SUPPLIER_INVOICE,
        CREATE_SUPPLIER_INVOICE,
        VIEW_PURCHASE_MATCH,
        # Records the supplier's answer to a return; posting it is the
        # accounting side's act, the same split the invoice draws.
        VIEW_SUPPLIER_CREDIT_NOTE,
        CREATE_SUPPLIER_CREDIT_NOTE,
        # Prepares a payment; letting the money go is not a branch act.
        VIEW_SUPPLIER_PAYMENT,
        CREATE_SUPPLIER_PAYMENT,
        VIEW_PROCUREMENT_REPORT,
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
        VIEW_SUPPLIER_RETURN,
        CREATE_SUPPLIER_RETURN,
        POST_SUPPLIER_RETURN,
        REVERSE_SUPPLIER_RETURN,
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
        # Reads every match and can withdraw one. Whoever will post from a
        # match is the person who decides it should stop being the answer.
        VIEW_PURCHASE_MATCH,
        MATCH_SUPPLIER_INVOICE,
        CANCEL_PURCHASE_MATCH,
        # Recognises the supplier's credit and can take it back — and
        # deliberately cannot record one, the invoice's maker-checker split.
        VIEW_SUPPLIER_CREDIT_NOTE,
        POST_SUPPLIER_CREDIT_NOTE,
        REVERSE_SUPPLIER_CREDIT_NOTE,
        # Lets the money go, and cannot type the payment in.
        VIEW_SUPPLIER_PAYMENT,
        POST_SUPPLIER_PAYMENT,
        REVERSE_SUPPLIER_PAYMENT,
        VIEW_PROCUREMENT_REPORT,
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
        # Does the reconciling. Matching is clerical work over evidence and
        # reaches no ledger, so it belongs here rather than with approval —
        # and withdrawing an agreed match deliberately does not.
        VIEW_PURCHASE_MATCH,
        MATCH_SUPPLIER_INVOICE,
        # Enters the supplier's credit note from the post, and posts none of
        # it — the same separation the invoice draws.
        VIEW_SUPPLIER_CREDIT_NOTE,
        CREATE_SUPPLIER_CREDIT_NOTE,
        # Prepares the payment run; the accounting manager releases it.
        VIEW_SUPPLIER_PAYMENT,
        CREATE_SUPPLIER_PAYMENT,
        VIEW_PROCUREMENT_REPORT,
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
        VIEW_SUPPLIER_RETURN,
        CREATE_SUPPLIER_RETURN,
        POST_SUPPLIER_RETURN,
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
