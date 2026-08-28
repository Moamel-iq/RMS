"""
Stock locations: where a thing sits, never what it cost.

ADR-018 §2 is the whole design. The warehouse owns value; a location owns
quantity. Nothing here reads or writes an average cost, a control account or a
journal, and a move between two bins in one store produces no `StockMovement`
at all — because nothing entered or left the warehouse and nothing was
revalued.

## The invariant, and how it is kept true rather than merely checked

    sum(located quantities) + unlocated == warehouse quantity

The unlocated remainder is **derived**, never stored. That is what makes the
invariant structural: there is no second number to drift.

Locations are optional, and "optional" has to survive an issue. If a warehouse
has 10 kg of rice all sitting in `BIN-A` and somebody posts an issue for 4 kg
without naming a bin, the warehouse drops to 6 while the bins still claim 10 —
the invariant would break through no fault of the caller.

`release_for_outbound` is the answer, and the ledger calls it on every
outbound movement. It takes the shortfall out of the **unlocated** pool first,
because stock nobody has put away is what a picker grabs first in a real store,
and only then out of locations in ascending code order. Deterministic, so two
concurrent issues cannot disagree about which bin emptied, and total, so the
invariant cannot be left false by any posting path.

A caller that *does* name a location gets exactly that location debited. The
automatic release is the fallback for the ordinary case, not a substitute for
intent.

## What this module refuses

- No valuation. `StockLocationBalance` has no value column and no code here
  computes one.
- No location freeze. A stock count freezes a whole warehouse
  (`FULL_WAREHOUSE + HARD_FREEZE`, ADR-021 §1); per-key freezing is a different
  mechanism and remains deferred.
- No picking strategy. FEFO and FIFO are strategies and ADR-018 keeps
  strategies behind a boundary; the release order here is an arbitrary
  deterministic tie-break, not a costing or rotation policy, and it is
  documented as such so nobody mistakes it for one.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.context import get_actor
from apps.core.models import AuditAction
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import (
    InventoryItem,
    InventoryLot,
    LocationMovementType,
    StockBalance,
    StockLocation,
    StockLocationBalance,
    StockLocationMovement,
    StockMovement,
    Warehouse,
)
from apps.inventory.services import canonical_code

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


@transaction.atomic
def create_location(
    *, warehouse: Warehouse, code: str, name: str, notes: str = ""
) -> StockLocation:
    """A new bin inside a warehouse."""
    if warehouse.is_system:
        raise ValidationError(
            _("Warehouse %(code)s is system-controlled and takes no locations."),
            code="location_in_system_warehouse",
            params={"code": warehouse.code},
        )
    location = StockLocation(
        warehouse=warehouse,
        code=canonical_code(code),
        name=name,
        notes=notes,
    )
    location.full_clean()
    location.save()
    record_audit_event(action=AuditAction.CREATED, target=location, new_state=snapshot(location))
    return location


@transaction.atomic
def update_location(
    *,
    location: StockLocation,
    name: str,
    notes: str = "",
    is_active: bool = True,
) -> StockLocation:
    """
    Rename or deactivate. The code never changes.

    A bin's code is painted on the shelf and referenced by every movement that
    ever touched it; renaming it would silently re-label history.
    """
    locked = StockLocation.objects.select_for_update().get(pk=location.pk)
    before = snapshot(locked)
    if not is_active and _located_quantity(locked) != ZERO:
        raise ValidationError(
            _("Location %(code)s still holds stock."),
            code="location_not_empty",
            params={"code": locked.code},
        )
    locked.name = name
    locked.notes = notes
    locked.is_active = is_active
    locked.full_clean()
    locked.save(update_fields=["name", "notes", "is_active", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED, target=locked, previous_state=before, new_state=snapshot(locked)
    )
    return locked


def _located_quantity(location: StockLocation) -> Decimal:
    total = location.balances.aggregate(total=Sum("quantity"))["total"]
    return total or ZERO


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def _lock_location_key(warehouse_id: int, item_id: int, lot_id: int | None) -> None:
    """
    One advisory lock per `(warehouse, item, lot)` across all its locations.

    The warehouse, not the location, because the invariant is about the whole
    warehouse position: two concurrent put-aways into different bins of the
    same item both have to see the same unlocated remainder, and a per-bin lock
    would let them both take it.

    Advisory rather than a row lock because the balance row may not exist yet —
    `SELECT ... FOR UPDATE` on a missing row locks nothing, so two first
    put-aways would both insert.
    """
    key = f"loc:{warehouse_id}:{item_id}:{lot_id or 0}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def warehouse_quantity(
    warehouse: Warehouse, item: InventoryItem, lot: InventoryLot | None
) -> Decimal:
    balance = StockBalance.objects.filter(warehouse=warehouse, item=item, lot=lot).first()
    return balance.quantity if balance else ZERO


def located_total(warehouse: Warehouse, item: InventoryItem, lot: InventoryLot | None) -> Decimal:
    total = StockLocationBalance.objects.filter(warehouse=warehouse, item=item, lot=lot).aggregate(
        total=Sum("quantity")
    )["total"]
    return total or ZERO


def unlocated_quantity(
    warehouse: Warehouse, item: InventoryItem, lot: InventoryLot | None
) -> Decimal:
    """
    What the warehouse holds that no bin claims. Derived, never stored.

    Storing it would create a second number that can disagree with the
    warehouse total; deriving it makes disagreement impossible.
    """
    return quantize_quantity(
        warehouse_quantity(warehouse, item, lot) - located_total(warehouse, item, lot)
    )


# ---------------------------------------------------------------------------
# Movements
# ---------------------------------------------------------------------------


def _apply(
    *,
    location: StockLocation,
    item: InventoryItem,
    lot: InventoryLot | None,
    delta: Decimal,
    movement_type: str,
    stock_movement: StockMovement | None,
    effective_at: datetime.datetime | None,
    reference: str,
) -> StockLocationMovement:
    """Write one location effect and its new balance. Caller holds the lock."""
    balance, _created = StockLocationBalance.objects.get_or_create(
        location=location,
        item=item,
        lot=lot,
        defaults={"warehouse": location.warehouse, "quantity": ZERO},
    )
    after = quantize_quantity(balance.quantity + delta)
    if after < ZERO:
        raise ValidationError(
            _("Location %(code)s holds %(held)s and cannot release %(asked)s."),
            code="location_insufficient_stock",
            params={"code": location.code, "held": balance.quantity, "asked": -delta},
        )
    balance.quantity = after
    balance.save(update_fields=["quantity", "updated_at"])

    return StockLocationMovement.objects.create(
        location=location,
        warehouse=location.warehouse,
        item=item,
        lot=lot,
        movement_type=movement_type,
        base_quantity=delta,
        quantity_after=after,
        stock_movement=stock_movement,
        effective_at=effective_at or timezone.now(),
        posted_by=get_actor(),
        reference=reference,
    )


@transaction.atomic
def put_away(
    *,
    location: StockLocation,
    item: InventoryItem,
    lot: InventoryLot | None = None,
    quantity: Decimal,
    reference: str = "",
    stock_movement: StockMovement | None = None,
) -> StockLocationMovement:
    """
    Move stock from the unlocated pool into a bin.

    Refused when the warehouse does not hold that much unlocated: a bin cannot
    receive goods the warehouse has not got, and allowing it would make
    `sum(located)` exceed the warehouse total — the invariant, broken.
    """
    quantity = quantize_quantity(quantity)
    if quantity <= ZERO:
        raise ValidationError(
            _("Put-away needs a positive quantity."), code="quantity_not_positive"
        )
    if not location.is_active:
        raise ValidationError(
            _("Location %(code)s is archived."),
            code="location_inactive",
            params={"code": location.code},
        )

    _lock_location_key(location.warehouse_id, item.pk, lot.pk if lot else None)
    available = unlocated_quantity(location.warehouse, item, lot)
    if quantity > available:
        raise ValidationError(
            _("Only %(available)s of %(item)s is unlocated in %(warehouse)s."),
            code="location_put_away_exceeds_unlocated",
            params={
                "available": available,
                "item": item.code,
                "warehouse": location.warehouse.code,
            },
        )
    return _apply(
        location=location,
        item=item,
        lot=lot,
        delta=quantity,
        movement_type=LocationMovementType.PUT_AWAY,
        stock_movement=stock_movement,
        effective_at=None,
        reference=reference,
    )


@transaction.atomic
def pick(
    *,
    location: StockLocation,
    item: InventoryItem,
    lot: InventoryLot | None = None,
    quantity: Decimal,
    reference: str = "",
    stock_movement: StockMovement | None = None,
) -> StockLocationMovement:
    """Return stock from a bin to the unlocated pool. The warehouse total is unchanged."""
    quantity = quantize_quantity(quantity)
    if quantity <= ZERO:
        raise ValidationError(_("Picking needs a positive quantity."), code="quantity_not_positive")
    _lock_location_key(location.warehouse_id, item.pk, lot.pk if lot else None)
    return _apply(
        location=location,
        item=item,
        lot=lot,
        delta=-quantity,
        movement_type=LocationMovementType.PICK,
        stock_movement=stock_movement,
        effective_at=None,
        reference=reference,
    )


@transaction.atomic
def move_between_locations(
    *,
    source: StockLocation,
    destination: StockLocation,
    item: InventoryItem,
    lot: InventoryLot | None = None,
    quantity: Decimal,
    reference: str = "",
) -> tuple[StockLocationMovement, StockLocationMovement]:
    """
    Move quantity between two bins of one warehouse.

    Produces two location movements and **no** `StockMovement`, because the
    warehouse position did not change and nothing was revalued. This is the
    case that proves locations and value are genuinely separate.
    """
    if source.pk == destination.pk:
        raise ValidationError(
            _("Source and destination are the same location."), code="location_same"
        )
    if source.warehouse_id != destination.warehouse_id:
        raise ValidationError(
            _("A location move stays inside one warehouse. Use a stock transfer."),
            code="location_move_crosses_warehouses",
        )
    if not destination.is_active:
        raise ValidationError(
            _("Location %(code)s is archived."),
            code="location_inactive",
            params={"code": destination.code},
        )
    quantity = quantize_quantity(quantity)
    if quantity <= ZERO:
        raise ValidationError(_("A move needs a positive quantity."), code="quantity_not_positive")

    _lock_location_key(source.warehouse_id, item.pk, lot.pk if lot else None)
    out = _apply(
        location=source,
        item=item,
        lot=lot,
        delta=-quantity,
        movement_type=LocationMovementType.TRANSFER_OUT,
        stock_movement=None,
        effective_at=None,
        reference=reference,
    )
    into = _apply(
        location=destination,
        item=item,
        lot=lot,
        delta=quantity,
        movement_type=LocationMovementType.TRANSFER_IN,
        stock_movement=None,
        effective_at=None,
        reference=reference,
    )
    return out, into


# ---------------------------------------------------------------------------
# The ledger hook
# ---------------------------------------------------------------------------


def release_for_outbound(
    *,
    warehouse_id: int,
    item_id: int,
    lot_id: int | None,
    quantity_after: Decimal,
    stock_movement: StockMovement | None = None,
) -> list[StockLocationMovement]:
    """
    Keep `sum(located) <= warehouse quantity` true after an outbound movement.

    Called by the ledger on every posting. A no-op — and one cheap query — for
    the overwhelming majority of warehouses, which use no locations at all.

    When a warehouse *does* use bins and stock leaves without anybody naming
    one, the shortfall comes out of locations in ascending code order. That
    order is an arbitrary deterministic tie-break so two concurrent issues
    cannot disagree about which bin emptied. **It is not a rotation policy:**
    FEFO and FIFO are strategies, ADR-018 keeps strategies behind a boundary,
    and nothing here should be read as choosing one.

    The unlocated pool absorbs the loss first, because stock nobody put away is
    what a picker takes first in a real store.
    """
    located = (
        StockLocationBalance.objects.filter(
            warehouse_id=warehouse_id, item_id=item_id, lot_id=lot_id
        )
        .exclude(quantity=ZERO)
        .select_related("location")
        .order_by("location__code")
    )
    balances = list(located)
    if not balances:
        return []

    total_located = quantize_quantity(sum((row.quantity for row in balances), ZERO))
    shortfall = quantize_quantity(total_located - quantity_after)
    if shortfall <= ZERO:
        return []

    released: list[StockLocationMovement] = []
    remaining = shortfall
    for row in balances:
        if remaining <= ZERO:
            break
        take = min(row.quantity, remaining)
        released.append(
            _apply(
                location=row.location,
                item=row.item,
                lot=row.lot,
                delta=-take,
                movement_type=LocationMovementType.PICK,
                stock_movement=stock_movement,
                effective_at=None,
                reference="auto-release",
            )
        )
        remaining = quantize_quantity(remaining - take)
    return released
