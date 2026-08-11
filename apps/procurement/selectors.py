"""
Procurement reads. Nothing here writes.

Every selector starts from the caller, not from an identifier. A function that
took an id and returned an object would have to be checked by whoever called
it, and the first caller to forget is the leak — so out-of-scope objects are
simply not in the queryset.
"""

from __future__ import annotations

import datetime
from typing import cast

from django.db.models import F, Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.inventory.models import InventoryItem
from apps.inventory.selectors import reachable_organization_ids
from apps.organizations.authorization import OutOfScope, accessible_warehouses
from apps.organizations.selectors import accessible_branches
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderVersion,
    PurchaseRequest,
    PurchaseRequestLine,
    Supplier,
    SupplierItem,
    SupplierQuotation,
    SupplierQuotationLine,
)
from apps.users.models import User


def visible_suppliers(user: User) -> QuerySet[Supplier]:
    """
    Every supplier in an organization the caller reaches — **archived ones
    included**.

    Archived suppliers stay visible on purpose. Their code is still reserved,
    every posted document still points at them, and a screen that hid them
    would make a taken code look free and leave no way to reactivate one
    archived by mistake.

    Reuses `reachable_organization_ids` rather than restating it. Reaching an
    organization means holding organization authority over it or a membership
    at any of its branches, and a second definition here would eventually
    disagree with the first.
    """
    return Supplier.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization")


def resolve_supplier(user: User, supplier_id: int) -> Supplier:
    """
    Turn a submitted supplier id into one the caller may reach.

    Resolved **with** the caller, never fetched and then checked: there is no
    moment where an out-of-scope supplier exists in a local variable. Out of
    scope answers 404 with the same wording a missing row gets, because a 403
    would confirm the supplier exists and ids are sequential.
    """
    supplier = visible_suppliers(user).filter(pk=supplier_id).first()
    if supplier is None:
        raise OutOfScope(_("Supplier %(id)s does not exist.") % {"id": supplier_id})
    return supplier


def visible_supplier_items(user: User) -> QuerySet[SupplierItem]:
    """
    Every catalogue row in an organization the caller reaches, archived
    included.

    Archived and expired rows stay visible for the reason archived suppliers
    do: a quotation raised last March referenced terms that are no longer
    current, and a screen that hid them would make that quotation unreadable.
    """
    return SupplierItem.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization", "supplier", "item", "item__base_unit", "package_unit")


def resolve_supplier_item(user: User, supplier_item_id: int) -> SupplierItem:
    """
    Turn a submitted catalogue id into one the caller may reach.

    Resolved **with** the caller, never fetched and then checked.
    """
    row = visible_supplier_items(user).filter(pk=supplier_item_id).first()
    if row is None:
        raise OutOfScope(_("Supplier item %(id)s does not exist.") % {"id": supplier_item_id})
    return row


def catalogue_effective_on(
    user: User, *, item: InventoryItem, on: datetime.date
) -> QuerySet[SupplierItem]:
    """
    Who can supply this item on a given date, cheapest quoted first.

    Ordered by price only as a **presentation** convenience. Nothing selects a
    supplier from this list automatically — PRC-016 — and a null price sorts
    last rather than first, because "no price on file" is not "free".
    """
    return (
        visible_supplier_items(user)
        .filter(item=item, is_active=True, effective_from__lte=on)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on))
        .order_by(F("last_quoted_price").asc(nulls_last=True), "supplier__code")
    )


def preferred_supplier_item(
    user: User, *, item: InventoryItem, on: datetime.date
) -> SupplierItem | None:
    """
    Where this item is normally bought, or None if nobody has said.

    At most one row can answer this — a partial unique index guarantees it —
    so `.first()` here is not a choice between candidates.
    """
    return catalogue_effective_on(user, item=item, on=on).filter(is_preferred=True).first()


def visible_purchase_requests(user: User) -> QuerySet[PurchaseRequest]:
    """
    Requests at branches the caller reaches, in every status.

    Branch-scoped rather than organization-scoped: a request names a branch
    warehouse, and a manager at one branch has no business reading another
    branch's shopping list.
    """
    return PurchaseRequest.objects.filter(branch__in=accessible_branches(user)).select_related(
        "organization", "branch", "warehouse", "requested_by"
    )


def resolve_purchase_request(user: User, request_id: int) -> PurchaseRequest:
    """Turn a submitted request id into one the caller may reach, or 404."""
    found = visible_purchase_requests(user).filter(pk=request_id).first()
    if found is None:
        raise OutOfScope(_("Purchase request %(id)s does not exist.") % {"id": request_id})
    return found


def resolve_request_line(
    user: User, *, request: PurchaseRequest, line_id: int
) -> PurchaseRequestLine:
    """
    A line, resolved **under its own request**.

    Passing the parent is not decoration: without it, a line id from another
    branch's request would resolve here and the route would act on it.
    """
    line = PurchaseRequestLine.objects.filter(pk=line_id, request=request).first()
    if line is None:
        raise OutOfScope(_("Request line %(id)s does not exist.") % {"id": line_id})
    return line


def visible_quotations(user: User) -> QuerySet[SupplierQuotation]:
    """Quotations in an organization the caller reaches, in every status."""
    return SupplierQuotation.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization", "supplier", "request", "recorded_by")


def resolve_quotation(user: User, quotation_id: int) -> SupplierQuotation:
    """Turn a submitted quotation id into one the caller may reach, or 404."""
    found = visible_quotations(user).filter(pk=quotation_id).first()
    if found is None:
        raise OutOfScope(_("Quotation %(id)s does not exist.") % {"id": quotation_id})
    return found


def resolve_quotation_line(
    user: User, *, quotation: SupplierQuotation, line_id: int
) -> SupplierQuotationLine:
    """A line, resolved under its own quotation — never by id alone."""
    line = SupplierQuotationLine.objects.filter(pk=line_id, quotation=quotation).first()
    if line is None:
        raise OutOfScope(_("Quotation line %(id)s does not exist.") % {"id": line_id})
    return line


def visible_purchase_orders(user: User) -> QuerySet[PurchaseOrder]:
    """Orders at branches the caller reaches, in every status."""
    return PurchaseOrder.objects.filter(branch__in=accessible_branches(user)).select_related(
        "organization", "branch", "supplier", "warehouse", "created_by"
    )


def resolve_purchase_order(user: User, order_id: int) -> PurchaseOrder:
    """Turn a submitted order id into one the caller may reach, or 404."""
    found = visible_purchase_orders(user).filter(pk=order_id).first()
    if found is None:
        raise OutOfScope(_("Purchase order %(id)s does not exist.") % {"id": order_id})
    return found


def resolve_order_line(user: User, *, order: PurchaseOrder, line_id: int) -> PurchaseOrderLine:
    """A line, resolved under its own order — never by id alone."""
    line = PurchaseOrderLine.objects.filter(pk=line_id, order=order).first()
    if line is None:
        raise OutOfScope(_("Order line %(id)s does not exist.") % {"id": line_id})
    return line


def order_version_history(user: User, *, order: PurchaseOrder) -> list[dict[str, object]]:
    """
    Every version of an order, newest first, each paired with what changed.

    The differences are computed here rather than stored. A stored diff is a
    third copy of the same facts — the version snapshot and the live row
    already hold them — and the copy is what goes stale when a later migration
    renames a field.

    The newest entry compares the live order against the most recent snapshot;
    each older entry compares one snapshot against the one before it.
    """
    versions = list(
        PurchaseOrderVersion.objects.filter(order=order)
        .select_related("revised_by")
        .order_by("-version")
    )
    if not versions:
        return []

    live = {
        "version": order.version,
        "header": _live_header(order),
        "lines": _live_lines(order),
        "reason": "",
        "revised_by": None,
        "revised_at": order.revised_at,
        "is_current": True,
    }

    entries: list[dict[str, object]] = []
    newer: dict[str, object] = live
    for snapshot_row in versions:
        older = {
            "version": snapshot_row.version,
            "header": snapshot_row.header,
            "lines": snapshot_row.lines,
            "reason": snapshot_row.reason,
            "revised_by": snapshot_row.revised_by,
            "revised_at": snapshot_row.revised_at,
            "is_current": False,
        }
        entries.append(
            {
                **newer,
                "changes": _difference(older, newer),
                "superseded_reason": snapshot_row.reason,
            }
        )
        newer = older
    entries.append({**newer, "changes": []})
    return entries


def _live_header(order: PurchaseOrder) -> dict[str, object]:
    return {
        "number": order.number,
        "status": order.status,
        "supplier_code": order.supplier.code,
        "warehouse_code": order.warehouse.code,
        "location_code": order.location.code if order.location else None,
        "ordered_on": order.ordered_on.isoformat(),
        "expected_on": order.expected_on.isoformat() if order.expected_on else None,
        "payment_terms_days": order.payment_terms_days,
        "supplier_reference": order.supplier_reference,
        "notes": order.notes,
        "total_amount": format(order.total_amount, "f"),
    }


def _live_lines(order: PurchaseOrder) -> list[dict[str, object]]:
    return [
        {
            "line_uid": str(line.line_uid),
            "sequence": line.sequence,
            "item_code": line.item.code,
            "ordered_quantity": format(line.ordered_quantity, "f"),
            "ordered_base_quantity": format(line.ordered_base_quantity, "f"),
            "unit_price": format(line.unit_price, "f"),
            "line_total": format(line.line_total, "f"),
        }
        for line in order.lines.select_related("item").order_by("sequence")
    ]


def _difference(older: dict[str, object], newer: dict[str, object]) -> list[str]:
    """
    What changed between two versions, in words a buyer can read.

    Compared field by field rather than by dumping both sides: a diff that
    says "the header changed" is a diff nobody can act on.
    """
    changes: list[str] = []
    old_header = cast(dict[str, object], older["header"])
    new_header = cast(dict[str, object], newer["header"])
    for field, old_value in old_header.items():
        new_value = new_header.get(field)
        if old_value != new_value:
            changes.append(f"{field}: {old_value} → {new_value}")

    old_rows = cast(list[dict[str, object]], older["lines"])
    new_rows = cast(list[dict[str, object]], newer["lines"])
    old_lines = {row["line_uid"]: row for row in old_rows}
    new_lines = {row["line_uid"]: row for row in new_rows}
    for uid, old_row in old_lines.items():
        new_row = new_lines.get(uid)
        if new_row is None:
            changes.append(f"{old_row['item_code']}: removed")
            continue
        for field in ("ordered_quantity", "unit_price", "line_total"):
            if old_row[field] != new_row[field]:
                changes.append(
                    f"{old_row['item_code']} {field}: {old_row[field]} → {new_row[field]}"
                )
    for uid, new_row in new_lines.items():
        if uid not in old_lines:
            changes.append(f"{new_row['item_code']}: added")
    return changes


def visible_goods_receipts(user: User) -> QuerySet[GoodsReceipt]:
    """
    Receipts in warehouses the caller may reach, in every status.

    Warehouse-scoped rather than branch-scoped: a receipt puts goods into one
    store, and inventory already narrows custody that way. A storekeeper with
    `SELECTED` warehouse scope sees deliveries into their own store and not
    into the one next door.
    """
    return GoodsReceipt.objects.filter(warehouse__in=accessible_warehouses(user)).select_related(
        "organization", "branch", "supplier", "warehouse", "order", "created_by"
    )


def resolve_goods_receipt(user: User, receipt_id: int) -> GoodsReceipt:
    """Turn a submitted receipt id into one the caller may reach, or 404."""
    found = visible_goods_receipts(user).filter(pk=receipt_id).first()
    if found is None:
        raise OutOfScope(_("Goods receipt %(id)s does not exist.") % {"id": receipt_id})
    return found


def resolve_receipt_line(user: User, *, receipt: GoodsReceipt, line_id: int) -> GoodsReceiptLine:
    """A line, resolved under its own receipt — never by id alone."""
    line = GoodsReceiptLine.objects.filter(pk=line_id, receipt=receipt).first()
    if line is None:
        raise OutOfScope(_("Receipt line %(id)s does not exist.") % {"id": line_id})
    return line


def outstanding_order_lines(order: PurchaseOrder) -> list[dict[str, object]]:
    """
    What a purchase order still expects, line by line.

    Derived from posted receipts every time it is asked for. A stored
    "remaining" column would be a second figure that drifts the first time a
    receipt is reversed.
    """
    from apps.procurement.services import received_base_quantity

    return [
        {
            "line": line,
            "ordered": line.ordered_base_quantity,
            "received": received_base_quantity(line),
            "outstanding": line.ordered_base_quantity - received_base_quantity(line),
        }
        for line in order.lines.select_related("item", "package_unit").order_by("sequence")
    ]
