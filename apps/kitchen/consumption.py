"""
What a kitchen store actually consumed, and the partition that makes it provable.

Three reads live here, and the first one is the foundation the other two stand
on: a **classification** of every posted movement at a kitchen warehouse into
exactly one bucket. Not a filter, not a sum over a hand-picked list of movement
types — a partition, where every movement lands in one bucket and no movement
lands in two or in none.

## Why a partition rather than a formula

A formula can be wrong quietly. `PRODUCTION_OUT + ISSUE + WASTE` looks correct
until somebody adds a movement type, and then the report is short by exactly
the movements nobody thought about, with nothing anywhere saying so.

A partition cannot be wrong quietly, because it carries its own proof. For each
`(warehouse, item, lot)` key over the window:

```
closing quantity − opening quantity  =  Σ (every bucket's signed total)
```

The left side comes from the kernel's own `quantity_before` / `quantity_after`
columns; the right side is built by adding up the buckets. If a movement fell
through the classifier into nothing, the right side is short and the identity
fails loudly (RCP-104). That is the difference between a table of good
intentions and a checkable claim.

## The distinction the whole task rests on

**Custody transfer is not consumption.**

Moving rice from the store to the kitchen changes who holds it. Nothing has
been used: the rice is still rice, still on the books, still countable. It is
consumed when a batch cooks it (`PRODUCTION_OUT`) or when somebody issues it
out for use (`ISSUE`).

The mirror error is just as bad and less obvious. A custody transfer carrying
material *back* to the store is not negative production consumption either.
Subtracting it from a posted batch's `PRODUCTION_OUT` would credit the same
kilogram twice — once through the transfer's own ledger effect and once again
in the report — and would restate a batch whose input value has already been
locked equal to its output value to the fils (RCP-034).

If a posted batch's inputs were genuinely wrong, the accounting-safe correction
is to **reverse the batch, correct the draft, and repost** (ADR-025 §7). Not a
later document that quietly edits history.

## Waste is classified by what was lost

Wasting 3 kg of raw onions is ingredient loss. Wasting 3 kg of *cooked mandi
rice* is the loss of a produced item whose ingredients already left stock
through that batch's `PRODUCTION_OUT`; adding it to ingredient consumption
would charge the rice, spice and oil a second time (RCP-105). The classifier
tells them apart by asking whether the item is any recipe's `output_item` in
that organization — a data question with a closed answer, never the document's
display text.

## Corrections stay corrections

`COUNT_GAIN`, `COUNT_LOSS` and `MANUAL_ADJUSTMENT` get their own buckets and
are excluded from consumption (RCP-106). A count difference is *the unexplained
thing a variance report exists to surface*, not an explanation of it. Folding
count losses into consumption would make actual consumption move to meet
theoretical consumption and drive the variance towards zero — arithmetically
self-fulfilling and operationally worthless.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.core.quantity import quantize_calculation
from apps.inventory.models import (
    MovementType,
    StockMovement,
    Warehouse,
)
from apps.kitchen.models import (
    ProductionBatch,
    ProductionBatchActualLine,
    ProductionBatchAllocation,
    ProductionBatchLine,
    ProductionBatchStatus,
    Recipe,
)
from apps.kitchen.productivity import ProductionFilters

if TYPE_CHECKING:
    from django.utils.functional import Promise

    from apps.users.models import User

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# The buckets
# ---------------------------------------------------------------------------


class MovementBucket(StrEnum):
    """
    Where one posted movement belongs. Closed, exhaustive, and mutually
    exclusive over `MovementType`.

    **Fifteen values — the approved vocabulary, and no more.** `WASTE` splits in
    two by what was lost and `MANUAL_ADJUSTMENT` splits in two by whether any
    quantity moved; every other `MovementType` maps to exactly one member.

    An earlier version of this enum carried seventeen, having added
    `SUPPLIER_RETURN_OUT` and `TRANSIT_SHORTAGE_LOSS` because `RETURN_OUT` and
    `TRANSFER_SHORTAGE` appeared to have no home. They do have homes, and the
    two extra members were solving the wrong problem: what those movements
    needed was **drill-down detail**, not a seat in the public vocabulary that
    every report, every export and every future consumer would then have to
    understand.

    They now live in `MovementSubcategory`, which is internal. A supplier return
    is a genuine reversal of a supply receipt, so it sits under
    `ECONOMIC_RETURN_OR_REVERSAL` and nets against **supply** rather than against
    consumption. A transfer shortage is stock that left this store's custody and
    never arrived, so it sits under `CUSTODY_TRANSFER_OUT` — custody, exactly
    like the dispatch it closes.

    Widening a public enum is the kind of change that looks free and is not: the
    fifteen are what ADR-026, the CSV headers and the API contract all name.
    """

    #: The starting point, not a flow.
    OPENING = "OPENING"
    #: Goods arriving from outside — a purchase receipt or a bare inventory
    #: receipt. Supply, never consumption.
    SUPPLY_RECEIPT = "SUPPLY_RECEIPT"
    #: A transfer **into** the kitchen store. Custody changed; nothing was
    #: used. Never added to consumption.
    CUSTODY_TRANSFER_IN = "CUSTODY_TRANSFER_IN"
    #: A transfer **out of** the kitchen store, including material carried back
    #: to the main store. Also custody, and never subtracted from a posted
    #: batch's production consumption.
    CUSTODY_TRANSFER_OUT = "CUSTODY_TRANSFER_OUT"
    #: `PRODUCTION_OUT` — ingredients a batch actually cooked. Consumption.
    PRODUCTION_CONSUMPTION = "PRODUCTION_CONSUMPTION"
    #: `PRODUCTION_IN` — the batch's own product arriving. Never consumption;
    #: counting it would net the batch to nothing.
    PRODUCTION_OUTPUT = "PRODUCTION_OUTPUT"
    #: An ordinary `ISSUE` out of the kitchen store: consumed without a batch.
    DIRECT_ECONOMIC_ISSUE = "DIRECT_ECONOMIC_ISSUE"
    #: `RETURN_IN` — unused material coming back from a prior issue, valued at
    #: that issue's own cost so the pair nets to zero. This one **does** reduce
    #: direct economic consumption, because it genuinely reverses it.
    ECONOMIC_RETURN_OR_REVERSAL = "ECONOMIC_RETURN_OR_REVERSAL"
    #: Waste of an ingredient. Abnormal loss, reported beside consumption.
    RAW_MATERIAL_WASTE = "RAW_MATERIAL_WASTE"
    #: Waste of something a recipe produced. Never expanded back into raw
    #: ingredients: they already left through the batch (RCP-105).
    PRODUCED_OUTPUT_WASTE = "PRODUCED_OUTPUT_WASTE"
    COUNT_GAIN = "COUNT_GAIN"
    COUNT_LOSS = "COUNT_LOSS"
    #: A `MANUAL_ADJUSTMENT` that moved value and no quantity — a revaluation.
    #: It cannot be consumption because nothing left.
    VALUE_ONLY_ADJUSTMENT = "VALUE_ONLY_ADJUSTMENT"
    #: A `MANUAL_ADJUSTMENT` that moved quantity. A correction, and corrections
    #: stay corrections (RCP-106).
    OTHER_QUANTITY_CORRECTION = "OTHER_QUANTITY_CORRECTION"
    #: A movement cancelling another exactly. Its own bucket, and it also
    #: carries `reverses_bucket` so the netting knows what it undid.
    REVERSAL = "REVERSAL"


class MovementSubcategory(StrEnum):
    """
    Drill-down detail **inside** a public bucket. Never a reporting dimension.

    Two of these exist so the arithmetic can be precise where a bucket holds
    more than one kind of event, and so a screen can say which kind it found
    without the public vocabulary growing to accommodate it.

    `ECONOMIC_RETURN_OR_REVERSAL` is the bucket that needs it. A `RETURN_IN`
    reverses an **issue** and must reduce direct economic consumption; a
    `RETURN_OUT` reverses a **receipt** and must reduce supply instead. Netting
    both against consumption — which is what a single undifferentiated bucket
    would force — would let a supplier return look like the kitchen having used
    less.
    """

    #: The ordinary case: the bucket needs no further distinction.
    NONE = ""
    #: `RETURN_IN` — unused material back from a prior issue. Reduces direct
    #: economic consumption, because it genuinely reverses it.
    ISSUE_RETURN_IN = "ISSUE_RETURN_IN"
    #: `RETURN_OUT` — goods leaving the business back to the supplier. Reduces
    #: **supply**, never consumption. Procurement's report owns the event; this
    #: records only that the stock left.
    SUPPLIER_RETURN_OUT = "SUPPLIER_RETURN_OUT"
    #: `TRANSFER_SHORTAGE` — dispatched stock that never arrived, written off
    #: out of in-transit. Custody that ended in a loss, not consumption.
    TRANSIT_SHORTAGE_LOSS = "TRANSIT_SHORTAGE_LOSS"


#: Arabic for each bucket. A screen reads this; the classifier never does.
BUCKET_LABELS: dict[MovementBucket, Promise] = {
    MovementBucket.OPENING: _("رصيد افتتاحي"),
    MovementBucket.SUPPLY_RECEIPT: _("توريد وارد"),
    MovementBucket.CUSTODY_TRANSFER_IN: _("عهدة واردة"),
    MovementBucket.CUSTODY_TRANSFER_OUT: _("عهدة صادرة"),
    MovementBucket.PRODUCTION_CONSUMPTION: _("استهلاك إنتاج"),
    MovementBucket.PRODUCTION_OUTPUT: _("ناتج إنتاج"),
    MovementBucket.DIRECT_ECONOMIC_ISSUE: _("صرف اقتصادي مباشر"),
    MovementBucket.ECONOMIC_RETURN_OR_REVERSAL: _("إرجاع من صرف"),
    MovementBucket.RAW_MATERIAL_WASTE: _("هالك مواد أولية"),
    MovementBucket.PRODUCED_OUTPUT_WASTE: _("هالك ناتج مُصنَّع"),
    MovementBucket.COUNT_GAIN: _("فائض جرد"),
    MovementBucket.COUNT_LOSS: _("عجز جرد"),
    MovementBucket.VALUE_ONLY_ADJUSTMENT: _("تسوية قيمة فقط"),
    MovementBucket.OTHER_QUANTITY_CORRECTION: _("تصحيح كمية"),
    MovementBucket.REVERSAL: _("عكس حركة"),
}

#: Arabic for each internal subcategory, for a drill-down column.
SUBCATEGORY_LABELS: dict[MovementSubcategory, Promise] = {
    MovementSubcategory.ISSUE_RETURN_IN: _("إرجاع من صرف"),
    MovementSubcategory.SUPPLIER_RETURN_OUT: _("إرجاع إلى المورد"),
    MovementSubcategory.TRANSIT_SHORTAGE_LOSS: _("عجز في الطريق"),
}

#: The buckets that are consumption of an ingredient by the kitchen. Everything
#: else is beside consumption, never inside it.
CONSUMPTION_BUCKETS = frozenset(
    {MovementBucket.PRODUCTION_CONSUMPTION, MovementBucket.DIRECT_ECONOMIC_ISSUE}
)

#: Abnormal loss. Shown in an economic-outflow subtotal beside consumption and
#: **never merged into it** — a kitchen that could hide spoilage inside
#: production usage is a kitchen with no spoilage report.
LOSS_BUCKETS = frozenset({MovementBucket.RAW_MATERIAL_WASTE, MovementBucket.PRODUCED_OUTPUT_WASTE})

CUSTODY_BUCKETS = frozenset(
    {MovementBucket.CUSTODY_TRANSFER_IN, MovementBucket.CUSTODY_TRANSFER_OUT}
)

CORRECTION_BUCKETS = frozenset(
    {
        MovementBucket.COUNT_GAIN,
        MovementBucket.COUNT_LOSS,
        MovementBucket.VALUE_ONLY_ADJUSTMENT,
        MovementBucket.OTHER_QUANTITY_CORRECTION,
    }
)

#: The straight one-to-one half of the classification. `WASTE`,
#: `MANUAL_ADJUSTMENT` and `REVERSAL` are absent because each needs a fact the
#: movement type alone does not carry, and a default here would be a guess.
_DIRECT_BUCKETS: dict[str, MovementBucket] = {
    MovementType.OPENING: MovementBucket.OPENING,
    MovementType.RECEIPT: MovementBucket.SUPPLY_RECEIPT,
    MovementType.TRANSFER_IN: MovementBucket.CUSTODY_TRANSFER_IN,
    MovementType.TRANSFER_OUT: MovementBucket.CUSTODY_TRANSFER_OUT,
    MovementType.PRODUCTION_OUT: MovementBucket.PRODUCTION_CONSUMPTION,
    MovementType.PRODUCTION_IN: MovementBucket.PRODUCTION_OUTPUT,
    MovementType.ISSUE: MovementBucket.DIRECT_ECONOMIC_ISSUE,
    MovementType.RETURN_IN: MovementBucket.ECONOMIC_RETURN_OR_REVERSAL,
    # A supplier return reverses a receipt, not a use, so it shares the return
    # bucket and is told apart by its subcategory below.
    MovementType.RETURN_OUT: MovementBucket.ECONOMIC_RETURN_OR_REVERSAL,
    # Dispatched stock that never arrived: custody that ended badly.
    MovementType.TRANSFER_SHORTAGE: MovementBucket.CUSTODY_TRANSFER_OUT,
    MovementType.COUNT_GAIN: MovementBucket.COUNT_GAIN,
    MovementType.COUNT_LOSS: MovementBucket.COUNT_LOSS,
}


#: Which movement types carry a subcategory, and which. Absent means `NONE`.
_SUBCATEGORIES: dict[str, MovementSubcategory] = {
    MovementType.RETURN_IN: MovementSubcategory.ISSUE_RETURN_IN,
    MovementType.RETURN_OUT: MovementSubcategory.SUPPLIER_RETURN_OUT,
    MovementType.TRANSFER_SHORTAGE: MovementSubcategory.TRANSIT_SHORTAGE_LOSS,
}


@dataclass(frozen=True)
class ClassifiedMovement:
    """One posted movement and the single bucket it belongs to."""

    movement: StockMovement
    bucket: MovementBucket
    #: For a `REVERSAL` only: what the cancelled movement was classified as, so
    #: the netting can subtract a reversed production issue from production
    #: consumption rather than from nothing. `None` everywhere else, and also
    #: `None` for a reversal whose original is not readable — which is a
    #: finding, not a default.
    reverses_bucket: MovementBucket | None = None
    #: Drill-down detail inside `bucket`. `NONE` for the ordinary case.
    subcategory: MovementSubcategory = MovementSubcategory.NONE

    @property
    def signed_quantity(self) -> Decimal:
        """The kernel's own signed base quantity. Negative for anything leaving."""
        return self.movement.base_quantity

    @property
    def signed_value(self) -> Decimal:
        return self.movement.inventory_value

    @property
    def nets_against(self) -> MovementBucket:
        """
        The bucket this movement's quantity should be counted under.

        A reversal counts against what it undid; everything else counts
        against itself. This is the one place the two readings of "which
        bucket" — *what kind of event is this* and *what does it change* — are
        allowed to differ, and keeping them as separate attributes is what lets
        the screen show a reversal as a reversal while the arithmetic treats it
        as the negative of a production issue.
        """
        if self.bucket is MovementBucket.REVERSAL and self.reverses_bucket is not None:
            return self.reverses_bucket
        return self.bucket


def produced_output_item_ids(organization_id: int) -> frozenset[int]:
    """
    Every item some recipe in this organization declares as its output.

    The test for "is this waste the loss of a produced thing?" (RCP-105).
    Asked of the recipe master rather than of production history on purpose: an
    item is a produced output because a recipe says it is made, not because it
    happens to have been made yet. Archived recipes count — an item stops being
    a produced good when the recipe stops existing, not when it stops selling.
    """
    return frozenset(
        Recipe.objects.filter(organization_id=organization_id)
        .exclude(output_item__isnull=True)
        .values_list("output_item_id", flat=True)
    )


def classify_kitchen_movement(
    movement: StockMovement, *, produced_item_ids: Iterable[int] | None = None
) -> ClassifiedMovement:
    """
    The one authoritative classifier. Every movement, exactly one bucket.

    `produced_item_ids` is passed in by bulk callers so the recipe-output set is
    resolved once per report rather than once per row; passing nothing makes
    this correct and slow rather than fast and wrong.

    Raises `ValueError` for a movement type this function does not know. That
    is deliberate and it is the whole design: a new `MovementType` must be
    classified explicitly, and the alternative — an `else: OTHER` fallback —
    would silently absorb it into a bucket nobody chose and quietly break every
    report that reads that bucket.
    """
    kind = movement.movement_type

    direct = _DIRECT_BUCKETS.get(kind)
    if direct is not None:
        return ClassifiedMovement(
            movement=movement,
            bucket=direct,
            subcategory=_SUBCATEGORIES.get(kind, MovementSubcategory.NONE),
        )

    if kind == MovementType.WASTE:
        known = (
            frozenset(produced_item_ids)
            if produced_item_ids is not None
            else produced_output_item_ids(movement.organization_id)
        )
        bucket = (
            MovementBucket.PRODUCED_OUTPUT_WASTE
            if movement.item_id in known
            else MovementBucket.RAW_MATERIAL_WASTE
        )
        return ClassifiedMovement(movement=movement, bucket=bucket)

    if kind == MovementType.MANUAL_ADJUSTMENT:
        # A revaluation moves money and no goods. Calling that a quantity
        # correction would put a number in a quantity column that no quantity
        # ever produced.
        bucket = (
            MovementBucket.VALUE_ONLY_ADJUSTMENT
            if movement.base_quantity == ZERO
            else MovementBucket.OTHER_QUANTITY_CORRECTION
        )
        return ClassifiedMovement(movement=movement, bucket=bucket)

    if kind == MovementType.REVERSAL:
        original = movement.reverses
        reverses_bucket = (
            classify_kitchen_movement(original, produced_item_ids=produced_item_ids).bucket
            if original is not None
            else None
        )
        original_subcategory = (
            _SUBCATEGORIES.get(original.movement_type, MovementSubcategory.NONE)
            if original is not None
            else MovementSubcategory.NONE
        )
        return ClassifiedMovement(
            movement=movement,
            bucket=MovementBucket.REVERSAL,
            reverses_bucket=reverses_bucket,
            subcategory=original_subcategory,
        )

    raise ValueError(f"unclassified movement type {kind!r} on movement {movement.pk}")


# ---------------------------------------------------------------------------
# تدفق مخزن المطبخ — the warehouse flow
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowFilters:
    """What narrows a warehouse-flow read."""

    warehouse_id: int | None = None
    item_id: int | None = None
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    bucket: str = ""


@dataclass
class ItemFlow:
    """
    One item at **one warehouse** over the window, partitioned and proved.

    Keyed by warehouse as well as item, and that is not cosmetic. The ledger's
    stock key is `(warehouse, item, lot)`, so `quantity_before` and
    `quantity_after` are positions in one warehouse. Grouping two warehouses'
    movements under one item would compare a total built from both against an
    opening and closing belonging to whichever happened to sort first, and the
    identity would fail on correct data — which is worse than not checking,
    because it trains the reader to ignore the check.

    Mutable during accumulation and never handed out mid-build; the report
    functions return it only once every movement has been added.
    """

    warehouse_id: int
    warehouse_code: str
    item_id: int
    item_code: str
    item_name: str
    base_unit_code: str
    opening: Decimal = ZERO
    closing: Decimal = ZERO
    quantities: dict[MovementBucket, Decimal] = field(default_factory=dict)
    values: dict[MovementBucket, Decimal] = field(default_factory=dict)
    #: Drill-down inside a bucket. The public totals above stay whole; this is
    #: what lets `direct_economic_consumption` net an issue return without also
    #: netting a supplier return that happens to share its bucket.
    subcategories: dict[MovementSubcategory, Decimal] = field(default_factory=dict)
    movement_count: int = 0

    def quantity_of(self, bucket: MovementBucket) -> Decimal:
        return self.quantities.get(bucket, ZERO)

    def value_of(self, bucket: MovementBucket) -> Decimal:
        return self.values.get(bucket, ZERO)

    def subcategory_of(self, subcategory: MovementSubcategory) -> Decimal:
        return self.subcategories.get(subcategory, ZERO)

    @property
    def net_movement(self) -> Decimal:
        """The sum of every bucket. Built from the buckets, never from the rows."""
        return quantize_calculation(sum(self.quantities.values(), ZERO))

    @property
    def identity_difference(self) -> Decimal:
        """
        `(closing − opening) − Σ buckets`. Zero when the partition is exhaustive.

        Non-zero means a movement reached the ledger and reached no bucket,
        which is the one failure this whole module is built to make visible.
        """
        return quantize_calculation((self.closing - self.opening) - self.net_movement)

    @property
    def identity_holds(self) -> bool:
        return self.identity_difference == ZERO

    # -- the named subtotals, all positive magnitudes ----------------------

    @property
    def net_production_consumption(self) -> Decimal:
        """
        `PRODUCTION_OUT` less the exact reversal of `PRODUCTION_OUT`.

        Positive, because "consumed 40 kg" reads better than "−40". The stored
        quantities are signed, so a reversal of an outbound is inbound and the
        sum falls out with no special case.

        A custody transfer is **not** in here, in either direction.
        """
        return -quantize_calculation(self.quantity_of(MovementBucket.PRODUCTION_CONSUMPTION))

    @property
    def direct_economic_consumption(self) -> Decimal:
        """
        An ordinary issue, less its genuine return and its exact reversal.

        Nets only the `ISSUE_RETURN_IN` share of the return bucket. A supplier
        return sits in the same bucket and reverses a **receipt**; subtracting it
        here would make goods sent back to a supplier look like the kitchen
        having cooked less.
        """
        return -quantize_calculation(
            self.quantity_of(MovementBucket.DIRECT_ECONOMIC_ISSUE)
            + self.subcategory_of(MovementSubcategory.ISSUE_RETURN_IN)
        )

    @property
    def supplier_return_out(self) -> Decimal:
        """Goods sent back to the supplier. Reduces supply, never consumption."""
        return -quantize_calculation(self.subcategory_of(MovementSubcategory.SUPPLIER_RETURN_OUT))

    @property
    def transit_shortage_loss(self) -> Decimal:
        """Dispatched stock that never arrived. Custody that ended in a loss."""
        return -quantize_calculation(self.subcategory_of(MovementSubcategory.TRANSIT_SHORTAGE_LOSS))

    @property
    def total_consumption(self) -> Decimal:
        return quantize_calculation(
            self.net_production_consumption + self.direct_economic_consumption
        )

    @property
    def raw_material_waste(self) -> Decimal:
        return -quantize_calculation(self.quantity_of(MovementBucket.RAW_MATERIAL_WASTE))

    @property
    def produced_output_waste(self) -> Decimal:
        return -quantize_calculation(self.quantity_of(MovementBucket.PRODUCED_OUTPUT_WASTE))

    @property
    def economic_outflow(self) -> Decimal:
        """
        Consumption **plus** abnormal loss, as a subtotal and never as a merge.

        Both numbers stay separately readable above it. A reader who wants "how
        much left this store economically" gets one figure; a reader asking
        "how much did we cook" still gets an answer that spoilage cannot
        inflate.
        """
        return quantize_calculation(
            self.total_consumption + self.raw_material_waste + self.produced_output_waste
        )

    @property
    def custody_in(self) -> Decimal:
        return quantize_calculation(self.quantity_of(MovementBucket.CUSTODY_TRANSFER_IN))

    @property
    def custody_out(self) -> Decimal:
        return -quantize_calculation(self.quantity_of(MovementBucket.CUSTODY_TRANSFER_OUT))

    @property
    def production_output(self) -> Decimal:
        return quantize_calculation(self.quantity_of(MovementBucket.PRODUCTION_OUTPUT))

    @property
    def supply_receipt(self) -> Decimal:
        """
        Goods received from outside, **net of what went back to the supplier**.

        A supplier return is stored negative, so adding its subcategory total
        nets it out of supply — which is the term it actually reverses.
        """
        return quantize_calculation(
            self.quantity_of(MovementBucket.SUPPLY_RECEIPT)
            + self.subcategory_of(MovementSubcategory.SUPPLIER_RETURN_OUT)
        )

    @property
    def count_correction(self) -> Decimal:
        return quantize_calculation(
            self.quantity_of(MovementBucket.COUNT_GAIN)
            + self.quantity_of(MovementBucket.COUNT_LOSS)
            + self.quantity_of(MovementBucket.OTHER_QUANTITY_CORRECTION)
        )

    @property
    def value_only_correction(self) -> Decimal:
        """Money that moved with no quantity behind it. Never a consumption."""
        return quantize_calculation(self.value_of(MovementBucket.VALUE_ONLY_ADJUSTMENT))


@dataclass(frozen=True)
class WarehouseFlow:
    """The partition for one warehouse over one window, with its proof."""

    warehouse: Warehouse | None
    date_from: datetime.date | None
    date_to: datetime.date | None
    items: list[ItemFlow]
    classified_count: int

    @property
    def identity_holds(self) -> bool:
        """True only when every item's stock identity balances exactly."""
        return all(row.identity_holds for row in self.items)

    @property
    def unbalanced(self) -> list[ItemFlow]:
        return [row for row in self.items if not row.identity_holds]

    def totals_by_bucket(self) -> dict[MovementBucket, Decimal]:
        totals: dict[MovementBucket, Decimal] = {}
        for row in self.items:
            for bucket, quantity in row.quantities.items():
                totals[bucket] = totals.get(bucket, ZERO) + quantity
        return totals


def kitchen_warehouse_movements(user: User, filters: FlowFilters) -> QuerySet[StockMovement]:
    """
    Posted movements at the kitchen warehouses this caller may read.

    Scoped through the same selector the production screens use: somebody who
    may read what was cooked at a store may read what moved through it, and
    somebody who may not, may not.
    """
    from apps.kitchen.kitchen_operations import readable_kitchen_warehouses

    warehouses = readable_kitchen_warehouses(user)
    rows = StockMovement.objects.filter(warehouse__in=warehouses).select_related(
        "item", "item__base_unit", "lot", "warehouse", "entry", "reverses"
    )
    if filters.warehouse_id:
        rows = rows.filter(warehouse_id=filters.warehouse_id)
    if filters.item_id:
        rows = rows.filter(item_id=filters.item_id)
    if filters.date_from:
        rows = rows.filter(entry__business_date__gte=filters.date_from)
    if filters.date_to:
        rows = rows.filter(entry__business_date__lte=filters.date_to)
    # Posted sequence, not time: it is the order the kernel actually valued
    # them in, and two movements can share a timestamp.
    return rows.order_by("posted_sequence")


def classified_movements(user: User, filters: FlowFilters) -> list[ClassifiedMovement]:
    """Every movement in scope, each carrying its single bucket."""
    movements = list(kitchen_warehouse_movements(user, filters))
    if not movements:
        return []
    produced = produced_output_item_ids(movements[0].organization_id)
    rows = [
        classify_kitchen_movement(movement, produced_item_ids=produced) for movement in movements
    ]
    if filters.bucket:
        rows = [row for row in rows if row.bucket == filters.bucket]
    return rows


def kitchen_warehouse_flow(user: User, filters: FlowFilters) -> WarehouseFlow:
    """
    The partition, per item, with the stock identity computed alongside it.

    Opening and closing come from the kernel's own `quantity_before` and
    `quantity_after` columns on the first and last movement of each
    `(item, lot)` key in the window — the numbers the ledger actually wrote,
    not a number this module derives and then checks against itself.

    That is what makes the identity a real test. The left side of
    `closing − opening = Σ buckets` is the ledger's; the right side is this
    module's classification. They can only agree if the classification is
    exhaustive.
    """
    warehouse = (
        Warehouse.objects.filter(pk=filters.warehouse_id).first() if filters.warehouse_id else None
    )
    # A bucket filter narrows the *display*, so the identity is computed from
    # the unfiltered set. Proving a partition against a subset of itself would
    # be proving nothing.
    unfiltered = FlowFilters(
        warehouse_id=filters.warehouse_id,
        item_id=filters.item_id,
        date_from=filters.date_from,
        date_to=filters.date_to,
    )
    rows = classified_movements(user, unfiltered)

    flows: dict[tuple[int, int], ItemFlow] = {}
    #: The ledger's own stock key — `(warehouse, item, lot)` — mapped to the
    #: first movement's `quantity_before` and the last one's `quantity_after`.
    #: Keyed by warehouse as well, because that is what the kernel positions
    #: are per; see `ItemFlow`.
    edges: dict[tuple[int, int, int | None], list[Decimal]] = {}

    for row in rows:
        movement = row.movement
        group = (movement.warehouse_id, movement.item_id)
        flow = flows.get(group)
        if flow is None:
            item = movement.item
            flow = ItemFlow(
                warehouse_id=movement.warehouse_id,
                warehouse_code=movement.warehouse.code,
                item_id=movement.item_id,
                item_code=item.code,
                item_name=item.name_ar,
                base_unit_code=item.base_unit.code,
            )
            flows[group] = flow

        bucket = row.nets_against
        flow.quantities[bucket] = flow.quantity_of(bucket) + movement.base_quantity
        flow.values[bucket] = flow.value_of(bucket) + movement.inventory_value
        if row.subcategory is not MovementSubcategory.NONE:
            flow.subcategories[row.subcategory] = (
                flow.subcategory_of(row.subcategory) + movement.base_quantity
            )
        flow.movement_count += 1

        key = (movement.warehouse_id, movement.item_id, movement.lot_id)
        edge = edges.get(key)
        if edge is None:
            edges[key] = [movement.quantity_before, movement.quantity_after]
        else:
            # `posted_sequence` order, so the last one seen is the latest.
            edge[1] = movement.quantity_after

    for (warehouse_id, item_id, _lot_id), (opened, closed) in edges.items():
        flow = flows[(warehouse_id, item_id)]
        flow.opening += opened
        flow.closing += closed

    for flow in flows.values():
        flow.opening = quantize_calculation(flow.opening)
        flow.closing = quantize_calculation(flow.closing)

    return WarehouseFlow(
        warehouse=warehouse,
        date_from=filters.date_from,
        date_to=filters.date_to,
        items=sorted(flows.values(), key=lambda row: (row.warehouse_code, row.item_code)),
        classified_count=len(rows),
    )


def flow_totals_by_item(flow: WarehouseFlow) -> list[ItemFlow]:
    """
    The same partition, merged across warehouses, for a per-item report.

    Consumption sums across warehouses legitimately — "how much rice did this
    branch's kitchens use" is a real question. Opening and closing sum too, and
    so the identity survives the merge: it is the sum of per-warehouse
    identities, each of which already held.

    The merged rows carry `warehouse_id = 0` and an empty code, because they
    belong to no single warehouse and inventing one would be a lie a screen
    would print.
    """
    merged: dict[int, ItemFlow] = {}
    for row in flow.items:
        combined = merged.get(row.item_id)
        if combined is None:
            combined = ItemFlow(
                warehouse_id=0,
                warehouse_code="",
                item_id=row.item_id,
                item_code=row.item_code,
                item_name=row.item_name,
                base_unit_code=row.base_unit_code,
            )
            merged[row.item_id] = combined
        combined.opening = quantize_calculation(combined.opening + row.opening)
        combined.closing = quantize_calculation(combined.closing + row.closing)
        combined.movement_count += row.movement_count
        for bucket, quantity in row.quantities.items():
            combined.quantities[bucket] = combined.quantity_of(bucket) + quantity
        for bucket, value in row.values.items():
            combined.values[bucket] = combined.value_of(bucket) + value
        for subcategory, quantity in row.subcategories.items():
            combined.subcategories[subcategory] = combined.subcategory_of(subcategory) + quantity
    return sorted(merged.values(), key=lambda row: row.item_code)


# ---------------------------------------------------------------------------
# استهلاك دفعة الإنتاج — one batch's actual consumption
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchConsumptionAllocation:
    """One lot/location the batch actually drew from, with what it was charged."""

    lot_code: str
    location_code: str
    base_quantity: Decimal
    consumed_value: Decimal | None


@dataclass(frozen=True)
class BatchConsumptionRow:
    """
    One recorded actual line, with the movement evidence behind it.

    Everything §H1 requires preserved is a field here rather than something the
    template recomputes: identity, source line, component path, item, unit,
    quantity, allocations, posted value. A report that derived any of them
    would be a second opinion about a frozen fact.
    """

    actual_id: int
    kind: str
    is_substitute: bool
    substitute_reason: str
    source_line_path: str
    source_line_label: str
    source_recipe_line_id: int | None
    item_code: str
    item_name: str
    base_unit_code: str
    entered_quantity: Decimal
    entered_unit_code: str
    base_quantity: Decimal
    allocations: tuple[BatchConsumptionAllocation, ...]
    posted_value: Decimal | None


@dataclass(frozen=True)
class BatchActualConsumption:
    """
    What one posted batch actually used, and whether the evidence agrees.

    The two equality checks are fields rather than assertions because this is a
    **report**, not a guard: it says what it found, and `verify_kitchen`
    decides whether a mismatch is an error. A read that raised would take a
    screen down over a defect somebody needs the screen to diagnose.
    """

    batch: ProductionBatch
    rows: tuple[BatchConsumptionRow, ...]
    #: item_id -> (recorded actual base quantity, PRODUCTION_OUT magnitude)
    quantity_evidence: dict[int, tuple[Decimal, Decimal]]
    input_movement_value: Decimal | None
    is_reversed: bool

    @property
    def quantity_matches(self) -> bool:
        """Recorded actuals agree with the movements, per item, exactly."""
        return all(recorded == moved for recorded, moved in self.quantity_evidence.values())

    @property
    def quantity_differences(self) -> dict[int, Decimal]:
        return {
            item_id: quantize_calculation(recorded - moved)
            for item_id, (recorded, moved) in self.quantity_evidence.items()
            if recorded != moved
        }

    @property
    def value_matches(self) -> bool:
        """
        `Σ input movement values = input_value = output_value`, to the fils.

        Unreadable without cost permission, in which case the check is not
        performed rather than performed on a blank — `None` here means "not
        asked", never "did not balance".
        """
        if self.input_movement_value is None:
            return True
        return (
            self.input_movement_value == self.batch.input_value
            and self.batch.input_value == self.batch.output_value
        )


def _actual_posted_value(
    actual: ProductionBatchActualLine,
    charged: Sequence[ProductionBatchAllocation],
    outbound_by_key: dict[str, StockMovement],
) -> Decimal:
    """
    What the kernel actually charged one recorded actual row.

    Two paths, because posting takes two. Where the row was **allocated** to
    lots, each allocation carries the `consumed_value` the kernel wrote back to
    it, and their sum is the row's value. Where it was **not** — the ordinary
    case for an item that is neither lot- nor location-tracked — there are no
    allocations to sum, and summing them anyway yields zero.

    Returning that zero was the first version of this function and it was wrong
    in the worst available way: the batch total said 50,390 and every line said
    nothing, so a reader would conclude the lines had been valued at nothing
    rather than that the report had looked in the wrong place. The unallocated
    value lives on the movement keyed `production-actual:<uuid>`, which is where
    the posting put it.

    `abs`, because an outbound movement is stored negative and a consumption
    figure on a screen is positive.
    """
    if charged:
        return quantize_calculation(
            sum((allocation.consumed_value or ZERO for allocation in charged), ZERO)
        )
    movement = outbound_by_key.get(f"production-actual:{actual.public_id}")
    if movement is None:
        return ZERO
    return quantize_calculation(abs(movement.inventory_value))


def batch_actual_consumption(
    batch: ProductionBatch, *, include_cost: bool = False
) -> BatchActualConsumption:
    """
    One posted batch's actual consumption, read from what the posting moved.

    **Links do not appear in this arithmetic.** A `BatchDocumentLink` naming a
    later custody transfer or a later waste document explains something that
    happened near this batch; it does not reduce what the batch consumed and it
    does not touch the batch's value (ADR-026 §4). The batch's inputs are
    frozen, its input value equals its output value, and the only correction is
    to reverse and repost.

    A `REVERSED` batch still reports what it consumed — that is history and it
    happened. What it must not do is contribute to *net current-period* actual
    consumption once its reversal is in the window, and that is handled by the
    period read, where the reversal movements are classified and netted.
    """
    rows: list[BatchConsumptionRow] = []
    recorded: dict[int, Decimal] = {}

    # Every outbound this posting made, keyed the way the posting keyed it.
    # `production_posting` writes one `PRODUCTION_OUT` per allocation where a
    # row was allocated (`production-allocation:<uuid>`) and one per actual row
    # where it was not (`production-actual:<uuid>`), so both shapes are
    # reachable from the row without re-deriving anything.
    outbound_by_key: dict[str, StockMovement] = {}
    if batch.stock_entry_id:
        outbound_by_key = {
            movement.effect_key: movement
            for movement in StockMovement.objects.filter(
                entry_id=batch.stock_entry_id, movement_type=MovementType.PRODUCTION_OUT
            )
        }

    lines = batch.lines.select_related("item", "item__base_unit", "source_line").order_by(
        "line_order"
    )
    for line in lines:
        actuals = line.actuals.select_related(
            "item", "item__base_unit", "entered_unit", "substitute"
        ).order_by("entry_order")
        for actual in actuals:
            if actual.base_quantity <= ZERO:
                # Only positive rows are consumption. A zero row records that
                # the kitchen looked and used none, which is evidence but not a
                # quantity.
                continue
            # Fetched once into a list: the allocations feed both the rows and
            # the value total, and a second `.all()` would be a second query
            # returning the same thing.
            charged = list(
                actual.allocations.select_related("lot", "location").order_by("allocation_order")
            )
            allocations = tuple(
                BatchConsumptionAllocation(
                    lot_code=allocation.lot.code if allocation.lot is not None else "",
                    location_code=allocation.location.code
                    if allocation.location is not None
                    else "",
                    base_quantity=quantize_calculation(allocation.base_quantity),
                    consumed_value=allocation.consumed_value if include_cost else None,
                )
                for allocation in charged
            )
            posted_value = (
                _actual_posted_value(actual, charged, outbound_by_key) if include_cost else None
            )
            rows.append(
                BatchConsumptionRow(
                    actual_id=actual.pk,
                    kind=str(actual.get_kind_display()),
                    is_substitute=bool(actual.substitute_id),
                    substitute_reason=actual.reason,
                    source_line_path=line.component_path or "-",
                    source_line_label=line.component_label_path,
                    source_recipe_line_id=line.source_line_id,
                    item_code=actual.item.code,
                    item_name=actual.item.name_ar,
                    base_unit_code=actual.item.base_unit.code,
                    entered_quantity=actual.entered_quantity,
                    entered_unit_code=actual.entered_unit.code
                    if actual.entered_unit is not None
                    else "",
                    base_quantity=quantize_calculation(actual.base_quantity),
                    allocations=allocations,
                    posted_value=posted_value,
                )
            )
            recorded[actual.item_id] = recorded.get(actual.item_id, ZERO) + actual.base_quantity

    moved: dict[int, Decimal] = {}
    input_value = ZERO if include_cost else None
    if batch.stock_entry_id:
        outbound = StockMovement.objects.filter(
            entry_id=batch.stock_entry_id, movement_type=MovementType.PRODUCTION_OUT
        )
        for movement in outbound:
            moved[movement.item_id] = moved.get(movement.item_id, ZERO) + abs(
                movement.base_quantity
            )
            if input_value is not None:
                input_value += abs(movement.inventory_value)

    evidence = {
        item_id: (
            quantize_calculation(recorded.get(item_id, ZERO)),
            quantize_calculation(moved.get(item_id, ZERO)),
        )
        for item_id in set(recorded) | set(moved)
    }

    return BatchActualConsumption(
        batch=batch,
        rows=tuple(rows),
        quantity_evidence=evidence,
        input_movement_value=quantize_calculation(input_value) if input_value is not None else None,
        is_reversed=batch.status == ProductionBatchStatus.REVERSED,
    )


# ---------------------------------------------------------------------------
# الاستهلاك الفعلي — the period read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeriodConsumption:
    """
    One warehouse over one window, with every stream reported separately.

    Deliberately not a single "actual consumption" number. §H2 asks for ten
    figures kept apart, and the reason is that every one of them is a different
    conversation: production usage is the kitchen's, custody is the
    storekeeper's, waste is the manager's, and count corrections are the thing
    the variance report exists to surface rather than to absorb.
    """

    flow: WarehouseFlow

    @property
    def items(self) -> list[ItemFlow]:
        return self.flow.items

    @property
    def identity_holds(self) -> bool:
        return self.flow.identity_holds


def period_actual_consumption(user: User, filters: FlowFilters) -> PeriodConsumption:
    """
    Actual consumption for one kitchen warehouse over one date range.

    A thin wrapper over the partition on purpose: consumption is not a separate
    calculation with its own rules, it is a **reading** of the partition, and
    two functions computing it two ways would be two chances to disagree.
    """
    return PeriodConsumption(flow=kitchen_warehouse_flow(user, filters))


# ---------------------------------------------------------------------------
# متطلبات الإنتاج القياسية — standard against actual, per posted batch
# ---------------------------------------------------------------------------

#: What a variance cell says when the two sides are measured in dimensions that
#: do not convert. Returned instead of zero, because zero means "no variance"
#: and this means "the question does not have a number for an answer".
NOT_QUANTITATIVELY_COMPARABLE = "NOT_QUANTITATIVELY_COMPARABLE"

#: The subtler case, and the one that actually occurs. A requirement met with
#: **some** rows in the plan's own dimension and **some** in another has a
#: perfectly honest variance over the rows that match — and a reader who is shown
#: only that number is being misled by omission.
#:
#: The demo carries exactly this shape: 15 KG of rice planned, met with 11.25 KG
#: of rice, 2 KG of cooked rice, and 1.5 **litres** of an approved oil
#: substitute. `comparable_consumption` correctly sums the two kilogram rows to
#: 13.25 and correctly refuses to add the litres, so the variance is −1.75 KG.
#: That is arithmetically right. But "used 1.75 KG less than planned" is a
#: materially different statement from "used 1.75 KG less than planned, and also
#: put in 1.5 L of oil", and a manager acting on the first would not know a
#: substitution had happened at all.
#:
#: Task 3.6's `ConsumptionComparison` returns an empty `statement` whenever a
#: figure is comparable, so there is nowhere in it for this disclosure to live.
#: Rather than reopen a certified module for it, Task 3.8's own read carries the
#: excluded rows and this status beside the number.
PARTIALLY_COMPARABLE = "PARTIALLY_COMPARABLE_DIMENSIONS_EXCLUDED"


@dataclass(frozen=True)
class StandardRequirementRow:
    """One requirement of one posted batch, planned against what went in."""

    batch: ProductionBatch
    line_id: int
    component_path: str
    component_label_path: str
    source_line_id: int | None
    item_code: str
    item_name: str
    base_unit_code: str
    planned_base_quantity: Decimal
    actual_base_quantity: Decimal | None
    variance: Decimal | None
    #: `""` when every recorded row shares the plan's dimension,
    #: `PARTIALLY_COMPARABLE_DIMENSIONS_EXCLUDED` when the variance is real but
    #: incomplete, `NOT_QUANTITATIVELY_COMPARABLE` when there is no number.
    compatibility: str
    statement: str
    actual_posted_value: Decimal | None
    #: Actual rows in another dimension, which the variance above **excludes**.
    #: `("DEMO-OIL 1.500000 L",)`. Empty for the ordinary case.
    excluded_rows: tuple[str, ...] = ()

    @property
    def is_comparable(self) -> bool:
        return self.variance is not None

    @property
    def is_complete(self) -> bool:
        """
        Whether the variance accounts for **everything** recorded.

        A comparable variance with excluded rows is a true number about part of
        the evidence. Distinguishing that from a variance with nothing left out
        is the whole point of this property.
        """
        return self.variance is not None and not self.excluded_rows

    @property
    def excluded_display(self) -> str:
        return " + ".join(self.excluded_rows)

    @property
    def version_label(self) -> str:
        return f"v{self.batch.recipe_version.version_number}"


def _line_posted_value(line_id: int) -> Decimal:
    """
    What one requirement's actual rows were actually charged.

    Read from `ProductionBatchAllocation.consumed_value` — the figure the
    Inventory kernel wrote when it valued the outbound — never recomputed from
    today's moving average. Repricing a historical movement would let a
    purchase made last week restate what a batch cost last month.
    """
    from apps.kitchen.models import ProductionBatchAllocation

    return quantize_calculation(
        sum(
            (
                row.consumed_value or ZERO
                for row in ProductionBatchAllocation.objects.filter(actual__line_id=line_id)
            ),
            ZERO,
        )
    )


def _excluded_statement(excluded: tuple[str, ...]) -> str:
    """
    What to say when a real variance leaves recorded consumption out of itself.

    Empty when nothing was excluded, so the ordinary row stays silent.
    """
    if not excluded:
        return ""
    return str(
        _(
            "الانحراف أعلاه يشمل السطور المتوافقة في البُعد فقط. سُجّل أيضاً "
            "استهلاك ببُعد قياس مختلف وهو خارج الرقم: %(rows)s"
        )
        % {"rows": " + ".join(excluded)}
    )


def _excluded_dimension_rows(line: ProductionBatchLine) -> tuple[str, ...]:
    """
    Recorded actual rows whose dimension differs from the requirement's.

    These are the rows `comparable_consumption` deliberately leaves out of its
    sum. Reading them here rather than re-deriving the comparability rule keeps
    one definition of "same dimension": the unit's own `dimension`, exactly as
    Task 3.6 asks it.
    """
    target = line.item.base_unit
    excluded: list[str] = []
    for row in line.actuals.select_related("item", "item__base_unit").order_by("entry_order"):
        if row.base_quantity <= ZERO:
            continue
        unit = row.item.base_unit
        if unit.dimension == target.dimension:
            continue
        excluded.append(f"{row.item.code} {row.base_quantity:f} {unit.code}")
    return tuple(excluded)


def _compatibility_of(row: object, excluded: tuple[str, ...]) -> str:
    """`""`, partially comparable, or not comparable at all."""
    comparable = getattr(row, "is_comparable", False)
    if not comparable:
        return NOT_QUANTITATIVELY_COMPARABLE
    return PARTIALLY_COMPARABLE if excluded else ""


def production_standard_requirements(
    user: User, filters: ProductionFilters, *, include_cost: bool = False
) -> list[StandardRequirementRow]:
    """
    Planned requirements against posted actuals, across every batch in scope.

    Built on Task 3.6's `variance_rows`, which already answers this for one
    batch and already knows how to say *not comparable* rather than zero. A
    second implementation would be a second opinion about the same frozen
    lines.

    **This is a valid Phase 3 variance** and it is not the sales-based usage
    variance. Both sides refer to the same production batch: what the frozen
    recipe said the batch needed, and what the kitchen actually put in. No sold
    quantity is involved and none is implied.
    """
    from apps.kitchen.productivity import posted_batches, variance_rows

    rows: list[StandardRequirementRow] = []
    batches = posted_batches(user, filters).select_related("recipe", "recipe_version", "warehouse")
    for batch in batches:
        for row in variance_rows(batch):
            line = row.line
            excluded = _excluded_dimension_rows(line)
            rows.append(
                StandardRequirementRow(
                    batch=batch,
                    line_id=line.pk,
                    component_path=line.component_path or "-",
                    component_label_path=line.component_label_path,
                    source_line_id=line.source_line_id,
                    item_code=line.item_code,
                    item_name=line.item_name,
                    base_unit_code=line.base_unit_code,
                    planned_base_quantity=row.planned,
                    actual_base_quantity=row.comparable_actual,
                    variance=row.variance,
                    compatibility=_compatibility_of(row, excluded),
                    statement=row.statement or _excluded_statement(excluded),
                    actual_posted_value=_line_posted_value(line.pk) if include_cost else None,
                    excluded_rows=excluded,
                )
            )
    return rows


def production_standard_variance(
    user: User, filters: ProductionFilters, *, include_cost: bool = False
) -> list[StandardRequirementRow]:
    """
    The requirement rows that actually deviated, largest deviation first.

    Same rows, different question. `production_standard_requirements` answers
    "what did this batch plan and use"; this answers "where did it miss", so it
    drops the lines that came out exactly and orders the rest by how far they
    went.

    The non-comparable rows are **kept**, at the end, and that is the point of
    ordering them last rather than filtering them out: a substitution across
    dimensions is the one deviation a variance report must never hide behind a
    blank cell.
    """
    rows = production_standard_requirements(user, filters, include_cost=include_cost)
    deviating = [
        row
        for row in rows
        # A row that came out exactly but excluded a cross-dimension
        # substitution is still a deviation worth reading: the zero is only zero
        # over the rows the variance could account for.
        if row.variance is None or row.variance != ZERO or row.excluded_rows
    ]
    return sorted(
        # `variance is None` sorts False before True, so every comparable row
        # comes first by descending magnitude and the incomparable ones follow.
        deviating,
        key=lambda row: (row.variance is None, -abs(row.variance or ZERO), row.item_code),
    )


__all__ = [
    "BUCKET_LABELS",
    "CONSUMPTION_BUCKETS",
    "CORRECTION_BUCKETS",
    "CUSTODY_BUCKETS",
    "LOSS_BUCKETS",
    "NOT_QUANTITATIVELY_COMPARABLE",
    "PARTIALLY_COMPARABLE",
    "BatchActualConsumption",
    "BatchConsumptionAllocation",
    "BatchConsumptionRow",
    "ClassifiedMovement",
    "FlowFilters",
    "ItemFlow",
    "MovementBucket",
    "MovementSubcategory",
    "SUBCATEGORY_LABELS",
    "PeriodConsumption",
    "StandardRequirementRow",
    "WarehouseFlow",
    "batch_actual_consumption",
    "classified_movements",
    "classify_kitchen_movement",
    "flow_totals_by_item",
    "kitchen_warehouse_flow",
    "kitchen_warehouse_movements",
    "period_actual_consumption",
    "produced_output_item_ids",
    "production_standard_requirements",
    "production_standard_variance",
]
