"""
Proportional allocation of a monetary amount across lines.

Used wherever one amount must be split: a document-level discount, an
application commission, a shared expense, a landed cost spread over receipt
lines.

Two guarantees:

  1. The parts sum exactly to the whole.

         sum(part.amount for part in allocate(...)) == quantize_money(total)

     Naive allocation does not hold that. Three lines splitting 1000.000 by
     thirds each give 333.333, summing to 999.999. The missing 0.001 has to
     land somewhere deliberate, or it becomes a reconciliation failure that
     someone chases months later.

  2. The same economic input always produces the same result.

     Correctness does NOT depend on the order the caller happens to pass
     lines in, nor on a queryset's implicit ordering. Every line carries an
     explicit `sequence`, and residual priority is decided by:

         remainder DESC, then sequence ASC

     A missing, duplicate, or invalid sequence is rejected rather than
     silently tolerated. Persisted lines pass their document line sequence;
     unpersisted callers must assign deterministic sequences before calling.

Method: largest remainder (Hamilton). Compute exact shares at high precision,
floor each to a whole quantum of 0.001 IQD, then hand the residual out one
quantum at a time by the priority above.

Exactness is structural, not approximate: the residual is derived by
subtracting the floors from the target, so whatever rounding happened while
computing shares cannot change the sum.

Residuals from an external settlement or cash rounding are NOT this module's
business. Those are a real gain or loss and post to an explicit cash rounding
account — see `apps.core.money.apply_cash_settlement_rounding`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.core.money import MONEY_QUANTUM, ensure_money_decimal, quantize_money

#: Working precision for the share computation. Far above the 28-digit default
#: so the floor of each exact share is never decided by a rounding artefact.
_WORKING_PRECISION = 60


@dataclass(frozen=True)
class AllocationItem:
    """
    One line participating in an allocation.

    `sequence` is the deterministic tie-break key and is required. For a
    persisted document line it is that line's own sequence number. For lines
    not yet persisted the caller assigns them, and must do so deterministically
    — the same economic input has to yield the same sequences every run.
    """

    sequence: int
    weight: Decimal


@dataclass(frozen=True)
class AllocationResult:
    """The amount allocated to one line, identified by its sequence."""

    sequence: int
    amount: Decimal


def _validate_sequences(items: Sequence[AllocationItem]) -> None:
    seen: set[int] = set()
    for position, item in enumerate(items):
        sequence = item.sequence

        # bool is a subclass of int; True arriving as a sequence is a bug.
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValidationError(
                _("Allocation item at position %(position)s has a non-integer sequence."),
                code="invalid_allocation_sequence",
                params={"position": position},
            )
        if sequence < 0:
            raise ValidationError(
                _("Allocation sequence must not be negative: %(sequence)s"),
                code="invalid_allocation_sequence",
                params={"sequence": sequence},
            )
        if sequence in seen:
            raise ValidationError(
                _("Duplicate allocation sequence: %(sequence)s"),
                code="duplicate_allocation_sequence",
                params={"sequence": sequence},
            )
        seen.add(sequence)


def allocate(total: object, items: Sequence[AllocationItem]) -> list[AllocationResult]:
    """
    Split `total` across `items` in proportion to their weights.

    Returns one result per item, ordered by sequence ascending, each amount
    quantized to 0.001 IQD and together summing exactly to
    `quantize_money(total)`.

    Weights must be non-negative and must not all be zero.
    """
    if len(items) == 0:
        raise ValidationError(
            _("Cannot allocate an amount across zero lines."),
            code="no_lines_to_allocate",
        )

    _validate_sequences(items)

    amount = quantize_money(total, field="allocated total")

    weights = [
        ensure_money_decimal(item.weight, field=f"weight[sequence={item.sequence}]")
        for item in items
    ]
    for item, weight in zip(items, weights, strict=True):
        if weight < 0:
            raise ValidationError(
                _("Allocation weight for sequence %(sequence)s is negative: %(value)s"),
                code="negative_allocation_weight",
                params={"sequence": item.sequence, "value": str(weight)},
            )

    weight_total = sum(weights, Decimal("0"))
    if weight_total == 0:
        raise ValidationError(
            _("Cannot allocate proportionally when every weight is zero."),
            code="zero_allocation_weights",
        )

    # Sort by sequence up front so nothing downstream depends on the order the
    # caller passed, including the order results come back in.
    ordered = sorted(zip(items, weights, strict=True), key=lambda pair: pair[0].sequence)

    if amount == 0:
        zero = Decimal("0").quantize(MONEY_QUANTUM)
        return [AllocationResult(item.sequence, zero) for item, _weight in ordered]

    # Work on the magnitude and restore the sign at the end, so a credit note
    # allocates the mirror image of the invoice it reverses.
    sign = -1 if amount < 0 else 1
    magnitude = abs(amount)

    # Everything below counts in whole quanta of 0.001 IQD, as integers.
    target_units = int((magnitude / MONEY_QUANTUM).to_integral_value())

    floors: list[int] = []
    remainders: list[Decimal] = []
    with localcontext() as context:
        context.prec = _WORKING_PRECISION
        for _item, weight in ordered:
            exact_units = (Decimal(target_units) * weight) / weight_total
            floor_units = int(exact_units.to_integral_value(rounding="ROUND_FLOOR"))
            floors.append(floor_units)
            remainders.append(exact_units - floor_units)

    residual = target_units - sum(floors)
    if residual < 0 or residual > len(ordered):
        # Unreachable at 60 digits of working precision; a guard rather than a
        # silent miscount if that assumption is ever wrong.
        raise ValidationError(
            _("Allocation residual %(residual)s is out of range."),
            code="allocation_residual_out_of_range",
            params={"residual": residual},
        )

    # remainder DESC, then sequence ASC. The sequence is the tie-break, so two
    # runs over the same economic input can never differ.
    priority = sorted(
        range(len(ordered)),
        key=lambda index: (-remainders[index], ordered[index][0].sequence),
    )
    for index in priority[:residual]:
        floors[index] += 1

    results = [
        AllocationResult(item.sequence, quantize_money(sign * units * MONEY_QUANTUM))
        for (item, _weight), units in zip(ordered, floors, strict=True)
    ]

    # The guarantee this module exists for. Checked, not assumed.
    if sum((result.amount for result in results), Decimal("0")) != amount:
        raise ValidationError(
            _("Allocation does not sum to the source amount."),
            code="allocation_does_not_balance",
        )

    return results


def allocate_by_rate(
    total: object, items: Sequence[AllocationItem], *, rate: object
) -> list[AllocationResult]:
    """
    Allocate `total * rate` across lines — a commission or a discount.

    The rate is applied to the total first and the product allocated once,
    rather than applying the rate line by line. Rating each line separately
    rounds every line independently, and those roundings do not add back up to
    the rate applied to the total.
    """
    amount = ensure_money_decimal(total, field="total") * ensure_money_decimal(rate, field="rate")
    return allocate(amount, items)
