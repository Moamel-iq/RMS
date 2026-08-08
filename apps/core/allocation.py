"""
Proportional allocation of a monetary amount across lines.

Used wherever one amount must be split: a document-level discount, an
application commission, a shared expense, a landed cost spread over receipt
lines.

The guarantee, and the only reason this module exists:

    sum(allocate_proportionally(total, weights)) == quantize_money(total)

exactly, for every input. Naive allocation — multiply each line by a rate and
round it — does not hold that. Three lines splitting 100.000 by thirds each
give 33.333, summing to 99.999. The missing 0.001 has to land somewhere
deliberate, or it silently becomes a reconciliation failure that someone
chases months later.

Method: largest remainder (Hamilton).

    1. Compute each line's exact share at high precision.
    2. Floor each to a whole quantum of 0.001 IQD.
    3. The residual — total minus the sum of the floors — is a whole number of
       quanta, always fewer than the number of lines.
    4. Give one extra quantum to the lines with the largest fractional
       remainders, until the residual is exhausted.
    5. Ties break on line order, so the same input always produces the same
       output. The caller must pass lines in a stable order — line sequence or
       primary key.

Exactness is structural, not approximate: the residual is derived by
subtraction from the target, so whatever rounding happened in step 1 cannot
change the total.

Residuals that come from an external settlement or cash rounding are NOT this
module's business. Those are a real gain or loss and post to an explicit cash
rounding account — see `apps.core.money.apply_cash_settlement_rounding`.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, localcontext

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.core.money import MONEY_QUANTUM, ensure_money_decimal, quantize_money

#: Working precision for the share computation. Far above the 28-digit default
#: so the floor of each exact share is never decided by a rounding artefact.
_WORKING_PRECISION = 60


def allocate_proportionally(
    total: object,
    weights: Sequence[object],
) -> list[Decimal]:
    """
    Split `total` across lines in proportion to `weights`.

    Returns one amount per weight, each quantized to 0.001 IQD, summing
    exactly to `quantize_money(total)`.

    Weights must be non-negative and must not all be zero. They are usually
    line net amounts, but any non-negative measure works — a landed cost split
    by weight or by volume, for instance.
    """
    if len(weights) == 0:
        raise ValidationError(
            _("Cannot allocate an amount across zero lines."),
            code="no_lines_to_allocate",
        )

    amount = quantize_money(total, field="allocated total")
    parsed_weights = [
        ensure_money_decimal(weight, field=f"weight[{index}]")
        for index, weight in enumerate(weights)
    ]

    for index, weight in enumerate(parsed_weights):
        if weight < 0:
            raise ValidationError(
                _("Allocation weight %(index)s is negative: %(value)s"),
                code="negative_allocation_weight",
                params={"index": index, "value": str(weight)},
            )

    weight_total = sum(parsed_weights, Decimal("0"))
    if weight_total == 0:
        raise ValidationError(
            _("Cannot allocate proportionally when every weight is zero."),
            code="zero_allocation_weights",
        )

    if amount == 0:
        return [Decimal("0").quantize(MONEY_QUANTUM) for _ in parsed_weights]

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
        for weight in parsed_weights:
            exact_units = (Decimal(target_units) * weight) / weight_total
            floor_units = int(exact_units.to_integral_value(rounding="ROUND_FLOOR"))
            floors.append(floor_units)
            remainders.append(exact_units - floor_units)

    residual = target_units - sum(floors)
    if residual < 0 or residual > len(parsed_weights):
        # Unreachable at 60 digits of working precision; a guard rather than a
        # silent miscount if that assumption is ever wrong.
        raise ValidationError(
            _("Allocation residual %(residual)s is out of range."),
            code="allocation_residual_out_of_range",
            params={"residual": residual},
        )

    # Largest remainder first; ties fall back to line order so the result is
    # reproducible.
    order = sorted(
        range(len(parsed_weights)),
        key=lambda index: (-remainders[index], index),
    )
    for index in order[:residual]:
        floors[index] += 1

    allocated = [quantize_money(sign * units * MONEY_QUANTUM) for units in floors]

    # The guarantee this module exists for. Checked, not assumed.
    if sum(allocated, Decimal("0")) != amount:
        raise ValidationError(
            _("Allocation does not sum to the source amount."),
            code="allocation_does_not_balance",
        )

    return allocated


def allocate_by_rate(
    total: object,
    weights: Sequence[object],
    *,
    rate: object,
) -> list[Decimal]:
    """
    Allocate `total * rate` across lines — a commission or a discount.

    The rate is applied to the total first and the product is allocated once,
    rather than applying the rate line by line. Rating each line separately
    would round every line independently, and those roundings do not add up to
    the rate applied to the total.
    """
    amount = ensure_money_decimal(total, field="total") * ensure_money_decimal(rate, field="rate")
    return allocate_proportionally(amount, weights)
