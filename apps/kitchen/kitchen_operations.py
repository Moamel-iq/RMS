"""
Kitchen-native reads over Inventory documents that already exist.

Three Arabic operational areas — الصرف للمطبخ, المرتجع من المطبخ, الهالك — and
**no new document model for any of them**. A kitchen issue is an Inventory
transfer into the kitchen warehouse; a kitchen return is an Inventory transfer
back out; kitchen waste is an Inventory Waste document at that warehouse. Each
already has its own lifecycle, its own permissions, its own reason codes, its
own lots and locations and its own accounting, and duplicating any of that
would produce a second set of books that agrees with the first only until
somebody uses the other screen.

So this module reads. It filters Inventory rows to one kitchen warehouse and
presents them in the kitchen's own vocabulary. Every write still goes through
the Inventory services the Inventory screens use, with the Inventory
permissions attached.

## The distinction the whole of Task 3.8 rests on

**Custody transfer is not consumption.**

Moving rice from the store to the kitchen changes who holds it. Nothing has
been consumed yet — the rice is still rice, still on the books, still counted.
It is consumed when a production batch cooks it (`PRODUCTION_OUT`) or when
somebody issues it out of the kitchen for use (`ISSUE`).

The charter's original formula added the transfer *and* the production usage,
which counts the same kilogram twice: once when it changed hands and once when
it was cooked. A variance report built on that shows a permanent structural
overage no kitchen can ever explain, and the natural response to an
unexplainable variance is to stop reading the report. Spec §11.1 records the
correction; this module is where the distinction first becomes visible, and the
column headings say **custody** rather than consumption for exactly that reason.

## Normal loss is not abnormal waste either

Nothing here reads yield. The gap between expected and actual output is normal
production loss, it is absorbed into the produced item's unit cost, and it
lives in `productivity.py`. What this module calls الهالك is an Inventory Waste
document: a deliberate act with a reason code, a quantity, a value and a
journal. Adding the two together would let a kitchen hide spoilage inside a
yield figure, which is the one thing the separation exists to prevent.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import QuerySet

from apps.inventory.models import (
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryMovementDocument,
    StockTransfer,
    StockTransferStatus,
    Warehouse,
)

if TYPE_CHECKING:
    from apps.users.models import User

ZERO = Decimal("0")


@dataclass(frozen=True)
class OperationFilters:
    """What narrows a kitchen operations read."""

    warehouse_id: int | None = None
    item_id: int | None = None
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    status: str = ""
    reason_code_id: int | None = None


def readable_kitchen_warehouses(user: User) -> QuerySet[Warehouse]:
    """
    The warehouses these screens may be scoped to.

    The same set the production screens use, because a kitchen store is a
    kitchen store: somebody who may read what was cooked there may read what
    was carried in and out of it, and somebody who may not, may not.
    """
    from apps.kitchen.selectors import readable_production_warehouses

    return readable_production_warehouses(user)


# ---------------------------------------------------------------------------
# الصرف للمطبخ — custody in
# ---------------------------------------------------------------------------


def custody_in(user: User, filters: OperationFilters) -> QuerySet[StockTransfer]:
    """
    Transfers **into** the selected kitchen warehouse.

    Custody, not consumption. The goods have changed hands and nothing has been
    used; the screen says so in words rather than leaving the reader to infer
    it from a column heading.
    """
    rows = StockTransfer.objects.filter(
        destination_warehouse__in=readable_kitchen_warehouses(user)
    ).select_related(
        "organization", "source_warehouse", "destination_warehouse", "source_warehouse__branch"
    )
    if filters.warehouse_id:
        rows = rows.filter(destination_warehouse_id=filters.warehouse_id)
    return _narrow_transfers(rows, filters)


# ---------------------------------------------------------------------------
# المرتجع من المطبخ — custody out
# ---------------------------------------------------------------------------


def custody_out(user: User, filters: OperationFilters) -> QuerySet[StockTransfer]:
    """
    Transfers **out of** the selected kitchen warehouse, back to a store.

    Also custody. This is emphatically **not** a reversal of `PRODUCTION_OUT`:
    material that was never cooked going back to the store is one event, and a
    posted batch that consumed too much is corrected by reversing the batch.
    Treating a return as negative production consumption would subtract the
    same kilogram twice, which is the mirror of the double count the charter's
    formula made in the other direction.
    """
    rows = StockTransfer.objects.filter(
        source_warehouse__in=readable_kitchen_warehouses(user)
    ).select_related(
        "organization",
        "source_warehouse",
        "destination_warehouse",
        "destination_warehouse__branch",
    )
    if filters.warehouse_id:
        rows = rows.filter(source_warehouse_id=filters.warehouse_id)
    return _narrow_transfers(rows, filters)


def _narrow_transfers(
    rows: QuerySet[StockTransfer], filters: OperationFilters
) -> QuerySet[StockTransfer]:
    if filters.date_from:
        rows = rows.filter(business_date__gte=filters.date_from)
    if filters.date_to:
        rows = rows.filter(business_date__lte=filters.date_to)
    if filters.status:
        rows = rows.filter(status=filters.status)
    if filters.item_id:
        rows = rows.filter(lines__item_id=filters.item_id).distinct()
    return rows.order_by("-business_date", "-pk")


def open_custody(user: User, filters: OperationFilters) -> QuerySet[StockTransfer]:
    """Transfers standing in transit — dispatched and not yet fully received."""
    return custody_in(user, filters).filter(
        status__in=[
            StockTransferStatus.DISPATCHED,
            StockTransferStatus.PARTIALLY_RECEIVED,
        ]
    )


# ---------------------------------------------------------------------------
# الهالك — abnormal waste
# ---------------------------------------------------------------------------


def kitchen_waste(user: User, filters: OperationFilters) -> QuerySet[InventoryMovementDocument]:
    """
    Inventory Waste documents raised at a kitchen warehouse.

    Abnormal loss, and kept apart from normal yield loss on purpose (spec
    §9/§11.3). This one has a reason code, a lot, a location, a value and a
    journal; normal yield loss has none of those and is absorbed into the
    produced item's unit cost. A report that added them would let spoilage hide
    inside a yield figure.

    `POSTED` and `REVERSED` only: a draft waste document is somebody thinking
    about it, and thinking about it has destroyed nothing.
    """
    rows = InventoryMovementDocument.objects.filter(
        document_type=InventoryDocumentType.WASTE,
        warehouse__in=readable_kitchen_warehouses(user),
    ).exclude(status=InventoryDocumentStatus.DRAFT)
    if filters.warehouse_id:
        rows = rows.filter(warehouse_id=filters.warehouse_id)
    if filters.date_from:
        rows = rows.filter(business_date__gte=filters.date_from)
    if filters.date_to:
        rows = rows.filter(business_date__lte=filters.date_to)
    if filters.status:
        rows = rows.filter(status=filters.status)
    if filters.item_id:
        rows = rows.filter(lines__item_id=filters.item_id).distinct()
    if filters.reason_code_id:
        rows = rows.filter(lines__reason_code_id=filters.reason_code_id).distinct()
    return rows.select_related("organization", "branch", "warehouse", "posted_by").order_by(
        "-business_date", "-pk"
    )


@dataclass(frozen=True)
class WasteTotal:
    """One item, summed across the waste documents in scope."""

    item_code: str
    item_name: str
    base_unit_code: str
    quantity: Decimal
    value: Decimal | None


def waste_totals(user: User, filters: OperationFilters, *, with_value: bool) -> list[WasteTotal]:
    """
    Waste summed per item, with money only where the caller may read it.

    `with_value` is passed in rather than resolved here so the permission check
    happens once, in the view, where the caller is known — and so a service
    cannot accidentally become a second place that decides who sees money.
    """
    totals: dict[int, WasteTotal] = {}
    documents = kitchen_waste(user, filters).filter(status=InventoryDocumentStatus.POSTED)
    for document in documents.prefetch_related("lines__item__base_unit"):
        for line in document.lines.all():
            existing = totals.get(line.item_id)
            quantity = (existing.quantity if existing else ZERO) + line.base_quantity
            value = None
            if with_value:
                previous = existing.value if existing and existing.value is not None else ZERO
                value = previous + (line.total_value or ZERO)
            totals[line.item_id] = WasteTotal(
                item_code=line.item.code,
                item_name=line.item.name,
                base_unit_code=line.item.base_unit.code,
                quantity=quantity,
                value=value,
            )
    return sorted(totals.values(), key=lambda row: row.item_code)


__all__ = [
    "OperationFilters",
    "WasteTotal",
    "custody_in",
    "custody_out",
    "kitchen_waste",
    "open_custody",
    "readable_kitchen_warehouses",
    "waste_totals",
]
