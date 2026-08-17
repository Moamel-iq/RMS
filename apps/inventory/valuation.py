"""
The bulk, read-only valuation query costing reads.

**Read-only, always.** Nothing here writes, posts, reprices or repairs. It
reads `StockMovement` exactly as `apps/inventory/reports.py` does under
`ReportMode.POSTED_AS_OF` — the stored `quantity_after` / `value_after` of the
last included movement *is* the position, because the kernel already replayed
that arithmetic and repeating it would be a second opinion about a settled
figure.

## Why this exists beside `reports.stock_valuation`

`stock_valuation` answers a *screen's* question and cannot answer a costing
engine's. Four differences, each of them load-bearing:

1. **Scope.** It narrows by `readable_warehouses(user)`, which is inventory's
   own custody scope. Recipe costing is authorized by `kitchen.view_recipe_cost`
   over the recipe's organization; the caller may legitimately hold that and no
   inventory membership at all. Costing therefore resolves its warehouse in its
   own layer and passes the object here. **This module performs no
   authorization and must never be reached from a view without one above it.**
2. **Grain.** It returns one row per lot, for display. Costing needs one
   figure per item: quantities summed, values summed, and the average derived
   from those two totals (ADR-018 §4). Averaging lot averages is a different
   and wrong number whenever the lots hold different quantities.
3. **Reproducibility.** Its historical window is a *date* predicate. Two calls
   a millisecond apart can therefore include different movements, and there is
   nothing to store on a snapshot that says which. This module resolves the
   date to a **posted-sequence high-water mark once**, and every position it
   then reads is constrained to that same integer.
4. **Availability.** A screen renders a blank cell. Costing must distinguish
   "valued at zero" from "not valued", because the first is a cost and the
   second forbids an authoritative snapshot.

Nothing about inventory's valuation policy, posting behaviour, models or
migrations changes. This is one more way to read what is already there.

## The cutoff, and why an integer is the honest evidence

`StockMovement.posted_sequence` is the total order the valuation kernel
computed in, unique per organization. `_next_posted_sequence` allocates it
under a row lock held to commit, so numbers are handed out serially: there is
no committed sequence *N* with an uncommitted *N−1* beside it. The maximum
committed sequence at or before a date is therefore a genuine high-water mark
with no holes beneath it, and constraining every read to `posted_sequence <= H`
gives the same answer however many times it runs and whatever commits in
between.

That is what makes §F's rule enforceable rather than aspirational: a receipt
racing a cost card takes a sequence *above* the mark and is wholly excluded, or
committed before it and is wholly included. There is no arrangement in which
one line sees it and another does not.

The date predicate resolving to that mark is `posted_at__date <= D`, character
for character what `reports._movement_cutoff` uses. Costing does not invent a
second POSTED_AS_OF.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from django.db.models import Max

from apps.core.money import quantize_money, quantize_unit_price
from apps.core.quantity import quantize_quantity
from apps.inventory.models import StockMovement, Warehouse
from apps.inventory.reports import ReportMode
from apps.organizations.models import Organization

ZERO = Decimal("0")


class ValuationState(StrEnum):
    """
    Whether a position can answer "what is one base unit worth".

    Three states rather than a nullable cost, because the two unavailable
    shapes are genuinely different facts and a report that merged them would
    be unable to say which one a buyer has to fix.
    """

    #: A position exists with a positive quantity. `unit_cost` is real, and may
    #: legitimately be zero — see `ItemValuation`.
    AVAILABLE = "AVAILABLE"
    #: No movement has ever touched this (warehouse, item) at or before the
    #: cutoff. Nothing was bought, so nothing has a cost.
    NO_POSITION = "NO_POSITION"
    #: Movements exist but the position is empty. An average over zero
    #: quantity is a division by zero, not a cost of zero.
    ZERO_QUANTITY = "ZERO_QUANTITY"


@dataclass(frozen=True)
class ValuationCutoff:
    """
    The exact ledger state a valuation read was taken against.

    Stored on a cost snapshot verbatim. `posted_sequence` is the reproducibility
    evidence: given the organization and this integer, the same positions can be
    re-derived years later, whatever has posted since.

    `posted_sequence == 0` is meaningful and not an error — it says nothing had
    been posted in this organization at or before `as_of_date`, so every item
    is `NO_POSITION`.
    """

    organization_id: int
    as_of_date: datetime.date
    posted_sequence: int
    mode: ReportMode = ReportMode.POSTED_AS_OF


@dataclass(frozen=True)
class ItemValuation:
    """
    One item's whole position in one warehouse, at one cutoff.

    `quantity` and `value` are the **sums across every lot**, and `unit_cost` is
    `value / quantity` derived from those sums. That is the ADR-018 arithmetic:
    the warehouse average is a value-weighted figure, and averaging the lots'
    own averages would answer a different question — one nobody asked and which
    disagrees whenever the lots hold unequal quantities.
    """

    warehouse_id: int
    item_id: int
    state: ValuationState
    quantity: Decimal
    value: Decimal
    unit_cost: Decimal
    lot_count: int
    #: The highest sequence that contributed. Below the cutoff whenever the
    #: item has been quiet, and useful when reading a snapshot back.
    last_posted_sequence: int

    @property
    def is_available(self) -> bool:
        return self.state is ValuationState.AVAILABLE


def posted_cutoff(*, organization: Organization, as_of_date: datetime.date) -> ValuationCutoff:
    """
    Resolve a date to the organization's posted-sequence high-water mark.

    Called **once** per cost calculation. Every position that calculation reads
    is then constrained to the integer this returns, so the whole card observes
    one ledger state — see the module docstring.

    `as_of_date` has no default. A costing read that quietly meant *today*
    would be right during development and wrong the first time somebody
    re-ran a July card in September.
    """
    highest = StockMovement.objects.filter(
        organization=organization, posted_at__date__lte=as_of_date
    ).aggregate(highest=Max("posted_sequence"))["highest"]
    return ValuationCutoff(
        organization_id=organization.pk,
        as_of_date=as_of_date,
        posted_sequence=int(highest or 0),
    )


def valuation_at_cutoff(
    *,
    warehouse: Warehouse,
    item_ids: list[int],
    cutoff: ValuationCutoff,
) -> dict[int, ItemValuation]:
    """
    Every requested item's position in one warehouse, at one cutoff.

    **One query for the whole set**, which is the other half of §F: a costing
    engine that asked per ingredient could see stock before a receipt on one
    line and after it on the next. Constraining to a captured sequence already
    forbids that; reading in bulk also makes it fast enough that nobody is
    tempted to cache a unit cost somewhere it can go stale.

    Returns a mapping keyed by item id, with an entry for **every** id asked
    for — an item with no position gets a `NO_POSITION` row rather than being
    absent, so a caller cannot mistake a missing key for a zero cost.

    Raises `ValueError` if the warehouse belongs to another organization than
    the cutoff. That is a programming error rather than a user's: the caller
    is responsible for resolving the warehouse in scope first, and a mismatch
    here would silently value a recipe against a ledger that is not its own.
    """
    if warehouse.branch.organization_id != cutoff.organization_id:
        raise ValueError(
            f"Warehouse {warehouse.pk} is not in organization {cutoff.organization_id}."
        )

    wanted = sorted(set(item_ids))
    results: dict[int, ItemValuation] = {
        item_id: ItemValuation(
            warehouse_id=warehouse.pk,
            item_id=item_id,
            state=ValuationState.NO_POSITION,
            quantity=ZERO,
            value=ZERO,
            unit_cost=ZERO,
            lot_count=0,
            last_posted_sequence=0,
        )
        for item_id in wanted
    }
    if not wanted or cutoff.posted_sequence <= 0:
        return results

    # The last movement per (item, lot) within the cutoff. `DISTINCT ON` with a
    # matching leading ORDER BY is Postgres's own last-row-per-group, and it
    # reads the kernel's stored running totals rather than re-folding them.
    latest = (
        StockMovement.objects.filter(
            warehouse=warehouse,
            item_id__in=wanted,
            posted_sequence__lte=cutoff.posted_sequence,
        )
        .order_by("item_id", "lot_id", "-posted_sequence")
        .distinct("item_id", "lot_id")
        .values("item_id", "lot_id", "quantity_after", "value_after", "posted_sequence")
    )

    totals: dict[int, list[Decimal | int]] = {}
    for row in latest:
        item_id = int(row["item_id"])
        bucket = totals.setdefault(item_id, [ZERO, ZERO, 0, 0])
        bucket[0] = Decimal(bucket[0]) + row["quantity_after"]
        bucket[1] = Decimal(bucket[1]) + row["value_after"]
        bucket[2] = int(bucket[2]) + 1
        bucket[3] = max(int(bucket[3]), int(row["posted_sequence"]))

    for item_id, (quantity, value, lots, highest) in totals.items():
        total_quantity = quantize_quantity(quantity)
        total_value = quantize_money(value)
        if total_quantity <= ZERO:
            # Movements happened and the shelf is empty. Not a cost of zero:
            # `value / 0` has no answer, and inventing one would understate
            # every recipe that names this item.
            results[item_id] = ItemValuation(
                warehouse_id=warehouse.pk,
                item_id=item_id,
                state=ValuationState.ZERO_QUANTITY,
                quantity=total_quantity,
                value=total_value,
                unit_cost=ZERO,
                lot_count=int(lots),
                last_posted_sequence=int(highest),
            )
            continue
        results[item_id] = ItemValuation(
            warehouse_id=warehouse.pk,
            item_id=item_id,
            state=ValuationState.AVAILABLE,
            quantity=total_quantity,
            value=total_value,
            # Derived from the two totals, never from the lots' own averages.
            # A quantity with zero value is a real zero-cost position — free
            # samples, a fully written-down lot — and stays available.
            unit_cost=quantize_unit_price(total_value / total_quantity),
            lot_count=int(lots),
            last_posted_sequence=int(highest),
        )

    return results
