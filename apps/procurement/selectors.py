"""
Procurement reads. Nothing here writes.

Every selector starts from the caller, not from an identifier. A function that
took an id and returned an object would have to be checked by whoever called
it, and the first caller to forget is the leak — so out-of-scope objects are
simply not in the queryset.
"""

from __future__ import annotations

import datetime

from django.db.models import F, Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.inventory.models import InventoryItem
from apps.inventory.selectors import reachable_organization_ids
from apps.organizations.authorization import OutOfScope
from apps.organizations.selectors import accessible_branches
from apps.procurement.models import (
    PurchaseRequest,
    PurchaseRequestLine,
    Supplier,
    SupplierItem,
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
