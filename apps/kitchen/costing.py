"""
Standard recipe costing: direct material cost for one exact version, one
warehouse, one date.

**Derived, never stored.** Nothing here writes to `Recipe` or `RecipeVersion`,
and neither model gained a cost field — a stored "current cost" would be a copy
of the ledger's moving average that starts drifting the moment the next receipt
posts (RCP-009, RCP-023). The only rows Task 3.3 persists are the append-only
snapshots in `apps/kitchen/snapshots.py`, and those are an explicit act.

## What a cost is a function of

Four things, all of them named by the caller and none of them defaulted:

    the exact RecipeVersion · the warehouse · the as-of date · POSTED_AS_OF

Miss any one and the answer is a different number that looks like the same
number. There is no `today`, no "the current version", no organization-wide
average and no other branch's warehouse. `cost_recipe_on_date` is the only
entry point that resolves a version, and it resolves it the one certified way
— `resolve_recipe_version` — and then costs *that exact row*.

## The arithmetic, and where rounding is allowed to happen

```
effective leaf quantity  = leaf.base_quantity x Π(multipliers on the path)
raw extension            = effective quantity x warehouse unit cost
total material cost      = quantize_money( Σ raw extensions )
allocated line extension = allocate(total, weights = raw extensions)
```

Three deliberate choices in that block:

* **The multiplier product is never quantized on the way down** (RCP-073,
  ADR-006). A gram of saffron three levels deep would otherwise be rounded
  three times before anybody multiplied it by a price.
* **The leaf quantity is quantized exactly once**, at `CALCULATION_PLACES` —
  the same precision `RecipeLine.base_quantity` itself carries. That is the
  storage boundary for this figure, and doing it here rather than after the
  multiplication means the number on the cost card is the number the extension
  was computed from. A snapshot that stored a rounded quantity beside an
  extension derived from an unrounded one could never be re-verified.
* **The document total is quantized once and then allocated back to the
  lines** (`CLAUDE.md`, ADR-012). Rating each line, rounding it, and summing is
  the forbidden shape: forty lines each rounded down is a recipe that cost less
  than it cost. `apps/core/allocation.allocate` distributes the residue by
  remainder DESC then sequence ASC, so `Σ lines == total` exactly and the class
  totals derived from the allocated figures add up to the same total.

`cost_per_output_unit` and `cost_per_serving` are **rates**, not posted
amounts, and quantize to `UNIT_PRICE_PLACES` (RCP-086). The serving
*allocation* is a different question with a different answer — see
`ServingCost`.

## What this module refuses to do

* Re-resolve a nested child by date. A `RecipeComponent` names one exact frozen
  `component_version`; costing follows that foreign key and nothing else, after
  it was superseded as much as before (RCP-072, RCP-081, spec §26.4). That walk
  is `apps/kitchen/expansion.py`, shared with Task 3.4's production drafting so
  the two cannot drift into disagreeing about what a recipe contains.
* Expand a stocked sub-recipe. Its `output_item` is an inventory item with a
  book value that already contains its ingredients; expanding it too would
  charge the parent twice (RCP-071).
* Apply `loss_rate` or `cooking_yield`. Those are informational and the costing
  input is the **gross** approved quantity (RCP-018, RCP-060). Multiplying by
  them would double-count a loss the gross figure already expresses.
* Read `RecipeLineSubstitute`. A substitute is what the batch *may* use;
  standard cost prices what the version *says* (RCP-022).
* Invent a price for an unvalued item. There is no fallback, no last purchase
  price, no supplier quotation and no zero. Missing valuation is reported and
  it forbids a snapshot.
* Touch money it has no business in. No selling price, no margin, no
  percentage of price, no commission, no labour, no overhead.

## Preview versus authoritative

A `DRAFT` or `SUBMITTED` version may be costed as a **preview** so the kitchen,
storekeeper and accountant can read the card they are being asked to sign. It
carries `is_authoritative = False`, cannot become a snapshot, and is never a
historical answer. `REJECTED` is not costable as a record at all. That
distinction supports the review without weakening RCP-015: only an approved
structure is authoritative.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.allocation import AllocationItem, allocate
from apps.core.money import MONEY_QUANTUM, quantize_money, quantize_unit_price
from apps.core.quantity import FACTOR_PLACES, quantize_calculation
from apps.inventory.models import InventoryItem, Warehouse
from apps.inventory.valuation import (
    ItemValuation,
    ValuationCutoff,
    ValuationState,
    posted_cutoff,
    valuation_at_cutoff,
)
from apps.kitchen.expansion import ExpandedLeaf, LeafKind, expand_recipe_version
from apps.kitchen.models import (
    Recipe,
    RecipeLine,
    RecipeLineCostClass,
    RecipeServing,
    RecipeVersion,
    RecipeVersionStatus,
)
from apps.organizations.models import Branch

if TYPE_CHECKING:
    from django.utils.functional import _StrPromise

ZERO = Decimal("0")
ONE = Decimal("1")

#: Bumped whenever the arithmetic above changes in a way that would make two
#: snapshots of the same version, warehouse and date disagree. Stored on every
#: snapshot so a reader can tell "the ledger moved" from "the formula moved",
#: and so the verifier can refuse to re-check a snapshot it does not understand.
CALCULATION_VERSION = "RCP-COST-2"

#: The working precision `_compact_allocation` shares with
#: `apps/core/allocation.allocate`. Far above the 28-digit default so the
#: floor of each exact share is never decided by a rounding artefact — and
#: identical to the allocator's, because the two must agree exactly.
_ALLOCATION_PRECISION = 60

#: How many example serving rows a **screen** may enumerate. A presentation
#: limit and nothing else.
#:
#: It decides no business calculation. Every serving definition receives its
#: exact allocation whatever its count, because the allocation is computed
#: **analytically** rather than by building one item per serving — see
#: `_compact_allocation`. A 50,000-portion scenario gets the same exact answer
#: as a 10-portion one and costs the same to compute.
MAX_ENUMERATED_SERVINGS = 5_000


def _refuse(
    message: str | _StrPromise, code: str, field_name: str | None = None
) -> ValidationError:
    """A domain refusal with a stable code — 422 at the API, never a 500."""
    if field_name is None:
        return ValidationError(message, code=code)
    return ValidationError({field_name: ValidationError(message, code=code)})


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class CostLineKind(StrEnum):
    """Where a costed leaf came from."""

    #: A `RecipeLine` on the version being costed.
    DIRECT = "DIRECT"
    #: A `RecipeLine` on a nested non-stocked child, reached through components.
    COMPONENT = "COMPONENT"


class ServingAllocationState(StrEnum):
    """
    Whether a serving scenario divides the output into whole servings at all.

    There is deliberately **no** "too large to allocate" state. Size is not a
    reason to stop answering: the allocation is analytic, so a 50,000-portion
    scenario is the same amount of arithmetic as a 10-portion one.
    """

    ALLOCATED = "ALLOCATED"
    #: The serving is larger than the whole output, so the batch makes no
    #: complete one. The whole output is leftover and carries the whole cost.
    #: A real state — a 5 kg platter from a 4 kg batch — not a refusal.
    NO_WHOLE_SERVING = "NO_WHOLE_SERVING"


@dataclass(frozen=True)
class MissingValuation:
    """
    One leaf whose item cannot be valued, named well enough to act on.

    Carries the component path as well as the item, because "the spice blend's
    cardamom" and "the dish's cardamom" are different rows to fix even when
    they name the same item.
    """

    item_code: str
    item_name: str
    warehouse_code: str
    component_path: str
    recipe_code: str
    version_number: int
    state: ValuationState
    code: str = "recipe_cost_item_not_valued"


@dataclass(frozen=True)
class PlateCost:
    """
    What one plate of this recipe costs, and the divisor that produced it.

    **The primary `RecipeServing` is the plate basis.** No model in this
    repository carries a `portions_per_batch` column — the Task 3.0 sketch
    proposed one on `Recipe` and Task 3.1 did not build it — and inventing one
    now would be a second, mutable statement of a figure the serving already
    holds exactly. RCP-084 guarantees exactly one primary per version with a
    partial unique index, so the divisor is unambiguous.

    ```
    portions_per_batch = expected output / primary serving base quantity
    plate cost         = total material cost x primary serving factor
    ```

    The two formulas in §6 are algebraically the same thing, because
    `factor_of_batch` **is** the primary serving's share of the output basis.
    The multiplication form is the one computed, and deliberately: it uses the
    version's own frozen twelve-place factor, so `plate_cost` equals the primary
    serving's `cost_per_serving` **exactly** rather than usually, and a snapshot
    can reproduce it from stored columns without re-deriving a division. A test
    asserts that equality rather than trusting the algebra.

    A rate, not a posted amount, so it quantizes once to `UNIT_PRICE_PLACES`.
    """

    serving: RecipeServing
    portions_per_batch: Decimal
    plate_cost: Decimal

    @property
    def portions_display(self) -> str:
        return f"{self.portions_per_batch:f}"

    @property
    def plate_cost_display(self) -> str:
        return f"{self.plate_cost:f}"


@dataclass(frozen=True)
class PlateCostUnavailable:
    """
    Why a card has no plate cost, said rather than left blank.

    Only reachable on a **preview**: an authoritative version has passed
    submission, and `_serving_problems` refuses a version with servings and no
    primary. That invariant is verified by a test rather than assumed, and this
    type exists so a draft the kitchen is still writing gets an honest answer
    instead of an invented divisor.
    """

    code: str
    message: str


@dataclass(frozen=True)
class RecipeCostLine:
    """
    One economic path from the version being costed to one inventory item.

    **Not one row per item.** The same item reached through two different
    components, or named directly *and* inside a blend, is two lines — the card
    exists to be traced, and collapsing the paths would hide where the cost
    came from. Its unit cost is fetched once for the whole card, so the two
    lines are priced identically (§J).

    `path` is the tuple of component `line_order`s from the root, empty for the
    version's own lines. It is the sort key, and `path_display` is the same
    thing for a screen.
    """

    path: tuple[int, ...]
    kind: CostLineKind
    source_version: RecipeVersion
    source_recipe: Recipe
    recipe_line: RecipeLine
    item: InventoryItem
    cost_class: str
    cumulative_multiplier: Decimal
    effective_quantity: Decimal
    valuation: ItemValuation
    unit_cost: Decimal
    raw_extension: Decimal
    #: Filled by the allocation once the document total is known. Their sum is
    #: the total, exactly.
    allocated_extension: Decimal = ZERO
    line_number: int = 0

    @property
    def path_display(self) -> str:
        """`2.1`, or an empty string for a line the version owns itself."""
        return ".".join(str(step) for step in self.path)

    @property
    def multiplier_display(self) -> str:
        """The cumulative factor as a technical identity: period, never comma."""
        return f"{self.cumulative_multiplier.normalize():f}"

    @property
    def quantity_display(self) -> str:
        """
        The effective quantity at its stored precision, LTR with a period.

        Not `{{ value }}`: Django localises a Decimal, so under Arabic this
        would render `1,500000` and a comma in a re-enterable quantity is
        ambiguous (`CLAUDE.md`). Not `|quantity` either - that filter is three
        places, and a recipe quantity lives at six.
        """
        return f"{self.effective_quantity:f}"

    @property
    def unit_cost_display(self) -> str:
        """The warehouse average, at the unit-price precision, LTR."""
        return f"{self.unit_cost:f}"

    @property
    def is_valued(self) -> bool:
        return self.valuation.is_available


@dataclass(frozen=True)
class ServingCost:
    """
    What one serving costs, answered twice because two questions were asked.

    * `cost_per_serving` is RCP-086's **rate**: the recipe total times the
      serving's share of the output basis, quantized once to `UNIT_PRICE_PLACES`
      because it is a unit cost and not a posted amount. It is the figure a
      menu-pricing screen shows.
    * The **allocation** divides the exact recipe total across the whole
      servings the output actually makes, plus the leftover output when the
      basis does not divide evenly. Its parts sum to the recipe total to the
      fils, because the residue is distributed rather than lost (RCP-087). This
      is the figure that has to reconcile.

    They differ by at most a fils per serving and that difference is the point:
    a rate rounds, an allocation does not.

    ## The allocation is stored compactly, and that is not an approximation

    Every whole serving carries equal weight, so the certified allocator gives
    all of them one of exactly **two** amounts: `normal_cost_per_serving`, and
    that plus one fils for the `elevated_count` servings that take a share of
    the residue. Recording the two amounts and the two counts is therefore the
    *whole* distribution, not a summary of it — the per-serving list is
    reconstructible from it and adds nothing.

    That is what lets a 50,000-portion scenario get the same exact answer as a
    10-portion one, in the same constant amount of work and constant storage. A
    test holds the compact form against `apps/core/allocation.allocate` itself
    for every small case, so this is a *derivation* of the certified semantics
    rather than a second opinion about them.

    Serving definitions are **alternatives**, never simultaneous. Two ways of
    portioning one output each allocate the *whole* total; adding two scenarios
    together would double the recipe.
    """

    serving: RecipeServing
    factor_of_batch: Decimal
    cost_per_serving: Decimal
    whole_count: int
    remainder_quantity: Decimal
    state: ServingAllocationState
    allocated_total: Decimal
    #: What most whole servings carry.
    normal_cost_per_serving: Decimal
    normal_serving_count: int
    #: What the servings that absorb a fils of the residue carry. Equal to
    #: `normal_cost_per_serving` when the residue happened to be zero.
    elevated_cost_per_serving: Decimal
    elevated_serving_count: int
    #: The output left over after the whole servings, and what it costs. It is
    #: output the batch paid for, so it carries cost rather than vanishing.
    remainder_cost: Decimal

    @property
    def factor_display(self) -> str:
        """The share of the output basis, at full stored precision, LTR."""
        quantum = Decimal(1).scaleb(-FACTOR_PLACES)
        return f"{self.factor_of_batch.quantize(quantum):f}"

    @property
    def cost_per_serving_display(self) -> str:
        """A rate, not a posted amount - six places, period, never localised."""
        return f"{self.cost_per_serving:f}"

    @property
    def remainder_display(self) -> str:
        return f"{self.remainder_quantity:f}"

    @property
    def minimum_allocated(self) -> Decimal:
        """The least any whole serving carries. Kept for the screens."""
        return self.normal_cost_per_serving

    @property
    def maximum_allocated(self) -> Decimal:
        """The most any whole serving carries — at most a fils more."""
        return self.elevated_cost_per_serving

    @property
    def is_enumerable(self) -> bool:
        """
        Whether a **screen** may list the servings one by one.

        A presentation question only. The allocation above is exact either way.
        """
        return self.whole_count <= MAX_ENUMERATED_SERVINGS

    def reconstructs_to(self) -> Decimal:
        """
        The distribution added back up, from the compact form alone.

        The check the compact representation exists to survive: if this ever
        stops equalling the recipe total, the summary has stopped describing
        the allocation it claims to describe.
        """
        return quantize_money(
            Decimal(self.normal_serving_count) * self.normal_cost_per_serving
            + Decimal(self.elevated_serving_count) * self.elevated_cost_per_serving
            + self.remainder_cost
        )


@dataclass(frozen=True)
class RecipeCostCard:
    """
    One complete answer, and everything needed to explain it later.

    Immutable. A caller that wants a different date asks again; mutating a card
    in place would let a screen show a total that no longer matches its lines.

    **No field here is called `profit`, `margin`, `selling_price` or
    `contribution`.** Task 3.3 calculates direct material cost. Price is Phase
    4's, set by the business, and RCP-089 forbids deriving one from a factor.
    """

    recipe: Recipe
    version: RecipeVersion
    warehouse: Warehouse
    branch: Branch
    as_of_date: datetime.date
    cutoff: ValuationCutoff
    is_authoritative: bool
    calculation_version: str
    version_status: str
    output_quantity: Decimal
    output_unit_code: str
    lines: list[RecipeCostLine]
    missing: list[MissingValuation]
    servings: list[ServingCost]
    total_material_cost: Decimal
    class_totals: dict[str, Decimal]
    cost_per_output_unit: Decimal
    #: What one plate costs, on the primary serving's basis. `None` only on a
    #: preview of a draft that has no primary serving yet — see `plate_problem`.
    plate: PlateCost | None = None
    plate_problem: PlateCostUnavailable | None = None

    @property
    def is_complete(self) -> bool:
        """
        Every leaf valued **and** a plate basis present. Both preconditions for
        a snapshot, because a costing record that could not explain its own
        plate cost later would be a record with a hole in it.
        """
        return not self.missing and self.plate is not None

    @property
    def plate_cost(self) -> Decimal | None:
        return self.plate.plate_cost if self.plate is not None else None

    @property
    def portions_per_batch(self) -> Decimal | None:
        return self.plate.portions_per_batch if self.plate is not None else None

    @property
    def primary_serving(self) -> RecipeServing | None:
        return self.plate.serving if self.plate is not None else None

    @property
    def valuation_mode(self) -> str:
        return str(self.cutoff.mode)

    @property
    def food_total(self) -> Decimal:
        return self.class_totals.get(RecipeLineCostClass.FOOD, ZERO)

    @property
    def packaging_total(self) -> Decimal:
        return self.class_totals.get(RecipeLineCostClass.PACKAGING, ZERO)

    @property
    def accompaniment_total(self) -> Decimal:
        return self.class_totals.get(RecipeLineCostClass.ACCOMPANIMENT, ZERO)

    @property
    def component_lines(self) -> list[RecipeCostLine]:
        return [line for line in self.lines if line.kind is CostLineKind.COMPONENT]

    @property
    def direct_lines(self) -> list[RecipeCostLine]:
        return [line for line in self.lines if line.kind is CostLineKind.DIRECT]

    @property
    def output_quantity_display(self) -> str:
        return f"{self.output_quantity:f}"

    @property
    def cost_per_output_unit_display(self) -> str:
        """A rate. Six places, LTR, and never a localised comma."""
        return f"{self.cost_per_output_unit:f}"

    def primary_serving_cost(self) -> ServingCost | None:
        """
        The primary serving's own scenario row.

        Its `cost_per_serving` equals `plate_cost` exactly — same total, same
        frozen factor — and a test asserts that rather than trusting it.
        """
        for serving in self.servings:
            if serving.serving.is_primary:
                return serving
        return None


# ---------------------------------------------------------------------------
# Walking the tree
# ---------------------------------------------------------------------------
#
# The walk itself lives in `apps/kitchen/expansion.py`, because Task 3.4 drafts
# production batches from the same graph and two copies of one traversal are
# two systems that will eventually disagree about what a recipe contains.
#
# What stays here is the part that is genuinely costing's: turning a leaf fact
# into a priced line. `CostLineKind` is kept as this module's own vocabulary and
# mapped from the engine's, so a stored snapshot's `source_kind` column does not
# change meaning because a shared module renamed an enum.


_KIND_FROM_LEAF = {
    LeafKind.DIRECT: CostLineKind.DIRECT,
    LeafKind.COMPONENT: CostLineKind.COMPONENT,
}


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def _branch_of(warehouse: Warehouse) -> Branch:
    return warehouse.branch


def _class_totals(lines: list[RecipeCostLine]) -> dict[str, Decimal]:
    """
    Each cost class's share, summed from the **allocated** figures.

    Derived rather than independently rounded, which is what makes
    `food + packaging + accompaniment == total` an arithmetic fact instead of a
    hope. Every declared class appears, at zero when unused: a report that
    dropped an empty class would change shape between two recipes.
    """
    totals = {str(value): ZERO for value in RecipeLineCostClass.values}
    for line in lines:
        totals[str(line.cost_class)] = totals.get(str(line.cost_class), ZERO) + (
            line.allocated_extension
        )
    return totals


def _allocate_lines(lines: list[RecipeCostLine], total: Decimal) -> list[RecipeCostLine]:
    """
    Push the rounded document total back onto the lines it came from.

    Weights are the raw extensions at full precision, so a line's share of the
    rounded total is its share of the unrounded one. Sequence is the line's own
    deterministic position, which is why `_ordered` runs first.

    A card whose every extension is zero — free stock, a fully written-down
    position — allocates zero to each line directly. The allocator refuses an
    all-zero weight set, correctly: there is no proportion to divide by. That is
    a real state here rather than a caller error, so it is handled instead of
    raised.
    """
    if not lines:
        return []
    if all(line.raw_extension == ZERO for line in lines):
        zero = ZERO.quantize(MONEY_QUANTUM)
        return [replace(line, allocated_extension=zero) for line in lines]

    results = allocate(
        total,
        [AllocationItem(sequence=line.line_number, weight=line.raw_extension) for line in lines],
    )
    by_sequence = {result.sequence: result.amount for result in results}
    return [replace(line, allocated_extension=by_sequence[line.line_number]) for line in lines]


@dataclass(frozen=True)
class _Allocation:
    """The whole distribution, in five numbers. See `_compact_allocation`."""

    normal_amount: Decimal
    normal_count: int
    elevated_amount: Decimal
    elevated_count: int
    leftover_amount: Decimal


def _compact_allocation(
    *, total: Decimal, whole: int, serving_quantity: Decimal, leftover: Decimal
) -> _Allocation:
    """
    The certified largest-remainder allocation, computed without building it.

    `apps/core/allocation.allocate` works in whole quanta of 0.001 IQD: it takes
    each item's exact share, floors it, and hands the residue out one quantum at
    a time by remainder DESC then sequence ASC. Nothing here changes that — this
    reproduces it, and a test holds the two against each other for every small
    case.

    The reproduction is possible because **every whole serving has the same
    weight**. Identical weights give identical exact shares, so all `whole`
    servings floor to the same integer and carry the same fractional remainder.
    The residue can therefore only produce two amounts: the floor, and the floor
    plus one fils for however many servings the residue reaches. Two amounts and
    two counts *are* the distribution; the per-serving list adds no information.

    The leftover output is one more item with its own weight, so it may floor
    differently and may or may not out-rank the servings for the residue. Its
    sequence is `whole + 1`, which is what breaks a tie against them — the
    servings win, exactly as they would if the list had been built.

    Constant work and constant storage, so 50,000 portions cost what 10 cost.
    """
    zero = ZERO.quantize(MONEY_QUANTUM)
    if whole <= 0 or total <= ZERO:
        # Nothing to spread across servings: the whole total, if any, belongs to
        # the leftover output. `allocate` would say the same with one item.
        return _Allocation(zero, 0, zero, 0, quantize_money(total))

    weight_total = Decimal(whole) * serving_quantity + leftover
    target_units = int((total / MONEY_QUANTUM).to_integral_value())

    with localcontext() as context:
        # The same 60 digits `allocate` uses, so the floors are decided by the
        # arithmetic and never by a rounding artefact.
        context.prec = _ALLOCATION_PRECISION
        exact_serving = (Decimal(target_units) * serving_quantity) / weight_total
        serving_floor = int(exact_serving.to_integral_value(rounding="ROUND_FLOOR"))
        serving_fraction = exact_serving - serving_floor

        if leftover > ZERO:
            exact_leftover = (Decimal(target_units) * leftover) / weight_total
            leftover_floor = int(exact_leftover.to_integral_value(rounding="ROUND_FLOOR"))
            leftover_fraction = exact_leftover - leftover_floor
        else:
            leftover_floor = 0
            leftover_fraction = Decimal(-1)  # never wins the residue

    residual = target_units - (whole * serving_floor + leftover_floor)

    # Remainder DESC, then sequence ASC. The servings hold sequences 1..whole
    # and the leftover holds whole + 1, so an exact tie goes to the servings.
    leftover_bump = 0
    if leftover_fraction > serving_fraction and residual > 0:
        leftover_bump = 1
        residual -= 1
    elevated = min(max(residual, 0), whole)
    residual -= elevated
    if residual > 0 and leftover > ZERO and not leftover_bump:
        leftover_bump = 1

    quantum = MONEY_QUANTUM
    return _Allocation(
        normal_amount=quantize_money(Decimal(serving_floor) * quantum),
        normal_count=whole - elevated,
        elevated_amount=quantize_money(Decimal(serving_floor + 1) * quantum),
        elevated_count=elevated,
        leftover_amount=quantize_money(Decimal(leftover_floor + leftover_bump) * quantum),
    )


def _serving_costs(
    *, version: RecipeVersion, total: Decimal, output_quantity: Decimal
) -> list[ServingCost]:
    """
    One scenario per active serving definition, each allocating the whole total.

    The count is `output ÷ serving base quantity`, and it is rarely a whole
    number: 50 kg of rice at 350 g a portion is 142 portions and 300 g left
    over. That leftover carries cost — it is output, and the batch paid for it
    — so it takes an allocation weight of its own. Dropping it would make the
    scenario sum to less than the recipe, and inflating the 142 portions to
    absorb it would overstate what one portion cost.

    **Every count allocates, however large.** The distribution is computed
    analytically by `_compact_allocation`, so a 50,000-portion scenario is the
    same arithmetic as a 10-portion one and produces the same exact answer. Size
    is a presentation question, not a business one.

    `rounding_policy` is not consulted here. It governs planning counts and
    **never touches money** (RCP-085): rounding 40.7 portions down to 40 is
    sensible, and letting that rounding move cost would make the sum of the
    serving costs disagree with the batch.
    """
    rows: list[ServingCost] = []
    servings = RecipeServing.objects.filter(version=version, is_active=True).order_by(
        "display_order", "code"
    )
    for serving in servings:
        rate = quantize_unit_price(total * serving.factor_of_batch)
        exact_count = output_quantity / serving.base_quantity
        whole = int(exact_count.to_integral_value(rounding="ROUND_FLOOR"))
        leftover = output_quantity - (Decimal(whole) * serving.base_quantity)

        split = _compact_allocation(
            total=total,
            whole=whole,
            serving_quantity=serving.base_quantity,
            leftover=leftover,
        )
        rows.append(
            ServingCost(
                serving=serving,
                factor_of_batch=serving.factor_of_batch,
                cost_per_serving=rate,
                whole_count=whole,
                remainder_quantity=quantize_calculation(leftover),
                state=(
                    ServingAllocationState.ALLOCATED
                    if whole >= 1
                    else ServingAllocationState.NO_WHOLE_SERVING
                ),
                allocated_total=quantize_money(
                    Decimal(split.normal_count) * split.normal_amount
                    + Decimal(split.elevated_count) * split.elevated_amount
                    + split.leftover_amount
                ),
                normal_cost_per_serving=split.normal_amount,
                normal_serving_count=split.normal_count,
                elevated_cost_per_serving=split.elevated_amount,
                elevated_serving_count=split.elevated_count,
                remainder_cost=split.leftover_amount,
            )
        )
    return rows


def _plate_cost(
    *, version: RecipeVersion, total: Decimal, output_quantity: Decimal
) -> tuple[PlateCost | None, PlateCostUnavailable | None]:
    """
    What one plate costs, from the primary serving. See `PlateCost`.

    Returns the reason rather than raising when there is no basis: a draft the
    kitchen is still writing should get an honest card with one honest gap in
    it, not a refusal that hides the rest of the figures.
    """
    primary = RecipeServing.objects.filter(version=version, is_primary=True, is_active=True).first()
    if primary is None:
        return None, PlateCostUnavailable(
            code="recipe_cost_no_primary_serving",
            message=str(_("لا توجد حصة أساسية لهذه النسخة، فلا يوجد أساس لكلفة الطبق.")),
        )
    if primary.base_quantity <= ZERO or output_quantity <= ZERO:
        return None, PlateCostUnavailable(
            code="recipe_cost_plate_basis_is_not_positive",
            message=str(_("كمية الحصة الأساسية أو ناتج النسخة غير موجب.")),
        )
    return (
        PlateCost(
            serving=primary,
            portions_per_batch=quantize_calculation(output_quantity / primary.base_quantity),
            # The frozen factor, not a fresh division: it makes this equal the
            # primary serving's own rate exactly. See `PlateCost`.
            plate_cost=quantize_unit_price(total * primary.factor_of_batch),
        ),
        None,
    )


def _build_card(
    *,
    version: RecipeVersion,
    warehouse: Warehouse,
    as_of_date: datetime.date,
    is_authoritative: bool,
) -> RecipeCostCard:
    """
    The whole calculation, inside one transaction and against one cutoff.

    The cutoff is captured **once** and every valuation is constrained to it, so
    a receipt racing this read is wholly included or wholly excluded — never one
    line before it and the next line after (§F). The transaction is what makes
    the recipe structure a consistent read too: a version cannot be superseded
    half-way through its own cost card.
    """
    recipe = version.recipe
    branch = _branch_of(warehouse)
    if branch.organization_id != recipe.organization_id:
        raise _refuse(
            _("المخزن يتبع مؤسسة أخرى."),
            "recipe_cost_wrong_warehouse",
            field_name="warehouse",
        )

    with transaction.atomic():
        cutoff = posted_cutoff(organization=recipe.organization, as_of_date=as_of_date)
        leaves = expand_recipe_version(version)
        valuations = valuation_at_cutoff(
            warehouse=warehouse,
            item_ids=[leaf.line.item_id for leaf in leaves],
            cutoff=cutoff,
        )

        priced: list[RecipeCostLine] = []
        leaf: ExpandedLeaf
        missing: list[MissingValuation] = []
        for number, leaf in enumerate(leaves, start=1):
            valuation = valuations[leaf.line.item_id]
            # One quantize, here, at the storage boundary for this figure.
            quantity = quantize_calculation(leaf.line.base_quantity * leaf.cumulative_multiplier)
            unit_cost = valuation.unit_cost if valuation.is_available else ZERO
            extension = quantity * unit_cost if valuation.is_available else ZERO
            line = RecipeCostLine(
                path=leaf.path,
                kind=_KIND_FROM_LEAF[leaf.kind],
                source_version=leaf.version,
                source_recipe=leaf.recipe,
                recipe_line=leaf.line,
                item=leaf.line.item,
                cost_class=str(leaf.line.cost_class),
                cumulative_multiplier=leaf.cumulative_multiplier,
                effective_quantity=quantity,
                valuation=valuation,
                unit_cost=unit_cost,
                raw_extension=extension,
                line_number=number,
            )
            priced.append(line)
            if not valuation.is_available:
                missing.append(
                    MissingValuation(
                        item_code=leaf.line.item.code,
                        item_name=leaf.line.item.name_ar,
                        warehouse_code=warehouse.code,
                        component_path=line.path_display,
                        recipe_code=leaf.recipe.code,
                        version_number=leaf.version.version_number,
                        state=valuation.state,
                    )
                )

        total = quantize_money(sum((line.raw_extension for line in priced), ZERO))
        allocated = _allocate_lines(priced, total)
        output_quantity = version.expected_output_quantity
        servings = _serving_costs(version=version, total=total, output_quantity=output_quantity)
        plate, plate_problem = _plate_cost(
            version=version, total=total, output_quantity=output_quantity
        )

    return RecipeCostCard(
        recipe=recipe,
        version=version,
        warehouse=warehouse,
        branch=branch,
        as_of_date=as_of_date,
        cutoff=cutoff,
        is_authoritative=is_authoritative,
        calculation_version=CALCULATION_VERSION,
        version_status=str(version.status),
        output_quantity=output_quantity,
        output_unit_code=version.output_unit.code,
        lines=allocated,
        missing=missing,
        servings=servings,
        total_material_cost=total,
        class_totals=_class_totals(allocated),
        cost_per_output_unit=(
            quantize_unit_price(total / output_quantity) if output_quantity > ZERO else ZERO
        ),
        plate=plate,
        plate_problem=plate_problem,
    )


# ---------------------------------------------------------------------------
# The three public reads
# ---------------------------------------------------------------------------

#: A cost card that may be relied on: the structure passed the four-party
#: control and is frozen. `SUPERSEDED` belongs here — it still answers for its
#: own historical dates, which is what effective dating is for.
COSTABLE_VERSION_STATUSES: frozenset[str] = frozenset(
    {
        RecipeVersionStatus.APPROVED,
        RecipeVersionStatus.ACTIVE,
        RecipeVersionStatus.SUPERSEDED,
    }
)

#: A version somebody is still writing or still arguing about. Costable as a
#: **preview** so the reviewers can read the card they are signing, and never
#: as a record.
PREVIEWABLE_VERSION_STATUSES: frozenset[str] = frozenset(
    {RecipeVersionStatus.DRAFT, RecipeVersionStatus.SUBMITTED}
)


def preview_recipe_cost(
    *,
    version: RecipeVersion,
    warehouse: Warehouse,
    as_of_date: datetime.date,
) -> RecipeCostCard:
    """
    A non-authoritative costing of a version still in review.

    Exists because the accountant's signature on `KM-RCP-004` is a signature on
    the *costing evidence*, and asking for it while refusing to show the figures
    would be asking for a signature on nothing. The card says
    `is_authoritative = False`, no snapshot can be built from it, and no
    historical resolver will ever return it.

    Refuses anything already frozen, in either direction: an `APPROVED` version
    has an authoritative answer and should be asked for it, and a `REJECTED` one
    has no answer at all.
    """
    if str(version.status) not in PREVIEWABLE_VERSION_STATUSES:
        raise _refuse(
            _("المعاينة للمسودات والنسخ قيد المراجعة فقط."),
            "recipe_cost_version_not_previewable",
            field_name="version",
        )
    return _build_card(
        version=version,
        warehouse=warehouse,
        as_of_date=as_of_date,
        is_authoritative=False,
    )


def cost_recipe_version(
    *,
    version: RecipeVersion,
    warehouse: Warehouse,
    as_of_date: datetime.date,
) -> RecipeCostCard:
    """
    The authoritative cost of one **exact** version, at one warehouse and date.

    No resolver anywhere in this path — not for the parent, which the caller
    named, and not for any nested child, which its parent named and froze. The
    version is costed as it is, whatever has been approved since.

    `REJECTED`, `DRAFT` and `SUBMITTED` are refused with
    `recipe_cost_version_not_authoritative`. A refusal somebody signed and a
    draft nobody has read are not costing records, and calling either one
    authoritative would put a number nobody approved into a menu decision.
    """
    if str(version.status) not in COSTABLE_VERSION_STATUSES:
        raise _refuse(
            _("لا يمكن اعتماد كلفة نسخة غير معتمدة."),
            "recipe_cost_version_not_authoritative",
            field_name="version",
        )
    return _build_card(
        version=version,
        warehouse=warehouse,
        as_of_date=as_of_date,
        is_authoritative=True,
    )


def cost_recipe_on_date(
    *,
    recipe: Recipe,
    branch: Branch,
    warehouse: Warehouse,
    on_date: datetime.date,
) -> RecipeCostCard:
    """
    What this recipe cost at this branch on this date — both halves date-driven.

    Version first, then costs (RCP-026). `resolve_recipe_version` answers which
    structure was in force, and **the same date** then values it. Using today's
    averages against July's version, or July's version against today's
    warehouse, would each be a different number wearing this one's name.

    The warehouse must belong to the branch and the branch to the recipe's
    organization. Both are refused as `recipe_cost_wrong_warehouse` /
    `recipe_version_foreign_branch` rather than quietly costed, because a
    warehouse in the wrong branch holds the wrong stock and the answer would be
    confidently wrong rather than obviously absent.

    Returns nothing for a date no version covers — the resolver raises
    `recipe_version_not_effective`, which is the honest answer to "what did it
    cost before it existed".
    """
    if warehouse.branch_id != branch.pk:
        raise _refuse(
            _("المخزن لا يتبع هذا الفرع."),
            "recipe_cost_wrong_warehouse",
            field_name="warehouse",
        )
    from apps.kitchen.lifecycle import resolve_recipe_version

    version = resolve_recipe_version(recipe=recipe, branch=branch, on_date=on_date)
    return cost_recipe_version(version=version, warehouse=warehouse, as_of_date=on_date)
