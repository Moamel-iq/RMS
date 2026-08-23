"""
The inventory overview, as one scoped read.

A dashboard is the one screen where a redaction mistake is invisible: a figure
that should have been hidden looks exactly like a figure that should not, and
nobody notices until a storekeeper quotes the stock value back. So valuation
follows the same rule the reports use — `include_valuation` is decided once by
the caller from `inventory.view_valuation`, and the keys are **omitted**
rather than blanked. A template that renders `{{ x }}` for a missing key prints
nothing; a template that renders a zero prints a lie.

Scope comes from `readable_warehouses`, never from a branch id in the request.
The counts below are deliberately restricted to what that scope reaches, so
two storekeepers in different branches see two different dashboards from the
same code path.

Everything here is a read. Nothing in this module writes, posts, or caches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import F, Sum

from apps.inventory.models import ItemType, StockBalance, StockMovement
from apps.inventory.selectors import readable_warehouses, visible_items
from apps.users.models import User

ZERO = Decimal("0")

#: How many rows the "largest balances" table shows. A dashboard is scanned,
#: so the table is a sample that earns a click, not a replacement for the
#: stock valuation report.
TOP_ROWS = 8


@dataclass(frozen=True)
class TypeSlice:
    """One item type's share of the stock, for the composition chart."""

    label: str
    value: Decimal
    share: Decimal


@dataclass(frozen=True)
class BalanceRow:
    """One line of the largest-balances table."""

    code: str
    name: str
    unit: str
    quantity: Decimal
    warehouse: str
    value: Decimal | None = None
    unit_cost: Decimal | None = None


@dataclass(frozen=True)
class InventoryOverview:
    """
    Everything the overview screen renders, already scoped and redacted.

    `total_value`, `type_slices` and the `value` on each row are `None` or
    empty for a caller without valuation rights. The template asks
    `{% if overview.total_value is not None %}`, so a missing figure removes
    its card instead of showing a zero.
    """

    warehouse_count: int
    active_item_count: int
    stocked_item_count: int
    movement_count: int
    unstocked_item_count: int
    rows: list[BalanceRow] = field(default_factory=list)
    total_value: Decimal | None = None
    type_slices: list[TypeSlice] = field(default_factory=list)

    @property
    def conic_stops(self) -> str:
        """
        The donut's paint, as conic-gradient stops.

        Computed here rather than in the template because cumulative addition
        is arithmetic, and template arithmetic is where a 99.2 + 0.8 quietly
        becomes 99.9. Colour names cycle the same `--series-*` tokens the
        legend uses, so the wedge and its swatch cannot disagree.
        """
        stops: list[str] = []
        cursor = Decimal("0")
        for index, piece in enumerate(self.type_slices):
            share = piece.share
            end = cursor + share
            if index == len(self.type_slices) - 1:
                end = Decimal("100")  # absorb quantisation dust in the last wedge
            stops.append(f"var(--series-{index % 6 + 1}) {cursor}% {end}%")
            cursor = end
        return ", ".join(stops)

    @property
    def stocked_share(self) -> Decimal:
        """Percentage of active items that carry a balance, 0 dp for display."""
        if not self.active_item_count:
            return ZERO
        return (Decimal(self.stocked_item_count) * 100 / Decimal(self.active_item_count)).quantize(
            Decimal("1")
        )


def inventory_overview(user: User, *, include_valuation: bool) -> InventoryOverview:
    """
    Build the overview for everything `user` can read.

    `include_valuation` is the caller's decision, not this function's: the view
    holds the request and therefore the permission, and passing it in keeps the
    redaction testable without a request object.
    """
    warehouses = readable_warehouses(user)
    warehouse_ids = list(warehouses.values_list("pk", flat=True))

    items = visible_items(user)
    active_item_count = items.filter(is_active=True).count()

    balances = StockBalance.objects.filter(warehouse_id__in=warehouse_ids)
    # A row that has gone to zero is still a row; it is not stock on hand.
    on_hand = balances.filter(quantity__gt=0)

    stocked_item_count = on_hand.values("item_id").distinct().count()
    movement_count = StockMovement.objects.filter(warehouse_id__in=warehouse_ids).count()

    top = on_hand.select_related("item", "item__base_unit", "warehouse").order_by(
        "-value", "-quantity"
    )[:TOP_ROWS]
    rows: list[BalanceRow] = []
    for balance in top:
        unit_cost: Decimal | None = None
        if include_valuation and balance.quantity:
            unit_cost = balance.value / balance.quantity
        rows.append(
            BalanceRow(
                code=balance.item.code,
                name=balance.item.name_ar,
                unit=balance.item.base_unit.code,
                quantity=balance.quantity,
                warehouse=balance.warehouse.code,
                value=balance.value if include_valuation else None,
                unit_cost=unit_cost,
            )
        )

    overview = InventoryOverview(
        warehouse_count=len(warehouse_ids),
        active_item_count=active_item_count,
        stocked_item_count=stocked_item_count,
        movement_count=movement_count,
        unstocked_item_count=max(active_item_count - stocked_item_count, 0),
        rows=rows,
    )
    if not include_valuation:
        return overview

    total = on_hand.aggregate(total=Sum("value"))["total"] or ZERO
    labels = dict(ItemType.choices)
    grouped = (
        on_hand.values(kind=F("item__item_type")).annotate(value=Sum("value")).order_by("-value")
    )
    slices = [
        TypeSlice(
            label=str(labels.get(row["kind"], row["kind"])),
            value=row["value"] or ZERO,
            share=(
                ((row["value"] or ZERO) * 100 / total).quantize(Decimal("0.1")) if total else ZERO
            ),
        )
        for row in grouped
        if row["value"]
    ]
    return InventoryOverview(
        warehouse_count=overview.warehouse_count,
        active_item_count=overview.active_item_count,
        stocked_item_count=overview.stocked_item_count,
        movement_count=overview.movement_count,
        unstocked_item_count=overview.unstocked_item_count,
        rows=overview.rows,
        total_value=total,
        type_slices=slices,
    )
