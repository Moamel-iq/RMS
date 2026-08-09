"""
Property tests for the moving weighted average.

Example-based tests check the cases somebody thought of. These check the ones
nobody did. The arithmetic functions are pure, so Hypothesis can hammer them
without a database and without a transaction per case.

The three invariants under test are the whole valuation contract:

    quantity == 0  =>  value == 0
    quantity  > 0  =>  value >= 0
    a sequence of receipts and issues that empties a position leaves nothing
    behind — no residual value, no residual average
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import assume, given
from hypothesis import strategies as st

from apps.core.money import quantize_money
from apps.core.quantity import quantize_quantity
from apps.inventory.ledger import apply_inbound, apply_outbound

ZERO = Decimal("0")

#: Realistic ranges. Quantities to 3dp, unit costs to 6dp, both bounded well
#: below the column widths so the test explores arithmetic rather than
#: overflow — overflow has its own, separate guard.
quantities = st.decimals(
    min_value=Decimal("0.001"), max_value=Decimal("100000"), places=3, allow_nan=False
)
unit_costs = st.decimals(
    min_value=Decimal("0"), max_value=Decimal("1000000"), places=6, allow_nan=False
)


@given(quantity=quantities, unit_cost=unit_costs)
def test_an_inbound_never_produces_negative_value(quantity: Decimal, unit_cost: Decimal) -> None:
    step = apply_inbound(
        quantity=quantity, unit_cost=unit_cost, before_quantity=ZERO, before_value=ZERO
    )
    assert step.quantity_after >= ZERO
    assert step.value_after >= ZERO


@given(quantity=quantities, unit_cost=unit_costs)
def test_a_full_depletion_always_lands_on_exactly_zero(
    quantity: Decimal, unit_cost: Decimal
) -> None:
    """
    The ADR-018 §4 guarantee, over the whole space rather than one example.

    Whatever the cost, taking everything out leaves no value and no average —
    never a residual that no later movement could clear.
    """
    inbound = apply_inbound(
        quantity=quantity, unit_cost=unit_cost, before_quantity=ZERO, before_value=ZERO
    )
    outbound = apply_outbound(
        quantity=inbound.quantity_after,
        before_quantity=inbound.quantity_after,
        before_value=inbound.value_after,
    )
    assert outbound.quantity_after == ZERO
    assert outbound.value_after == ZERO
    assert outbound.average_after == ZERO
    # And the value taken out is exactly the value that was in.
    assert -outbound.value_delta == inbound.value_after


@given(
    first_quantity=quantities,
    first_cost=unit_costs,
    second_quantity=quantities,
    second_cost=unit_costs,
)
def test_two_receipts_blend_to_the_total_value_over_the_total_quantity(
    first_quantity: Decimal,
    first_cost: Decimal,
    second_quantity: Decimal,
    second_cost: Decimal,
) -> None:
    """
    The average is derived from the totals, and the totals are the sum of what
    was actually received. Deriving the totals from the average instead would
    accumulate every receipt's rounding error into the stored value.
    """
    first = apply_inbound(
        quantity=first_quantity,
        unit_cost=first_cost,
        before_quantity=ZERO,
        before_value=ZERO,
    )
    second = apply_inbound(
        quantity=second_quantity,
        unit_cost=second_cost,
        before_quantity=first.quantity_after,
        before_value=first.value_after,
    )

    assert second.quantity_after == quantize_quantity(first_quantity + second_quantity)
    assert second.value_after == quantize_money(
        quantize_money(first_quantity * first_cost) + quantize_money(second_quantity * second_cost)
    )
    if second.quantity_after > ZERO:
        assert second.value_after >= ZERO


@given(
    received=quantities,
    unit_cost=unit_costs,
    issued=quantities,
)
def test_a_partial_issue_leaves_a_non_negative_position(
    received: Decimal, unit_cost: Decimal, issued: Decimal
) -> None:
    """Any issue that does not exhaust the balance leaves value at or above zero."""
    assume(issued < received)
    inbound = apply_inbound(
        quantity=received, unit_cost=unit_cost, before_quantity=ZERO, before_value=ZERO
    )
    outbound = apply_outbound(
        quantity=issued,
        before_quantity=inbound.quantity_after,
        before_value=inbound.value_after,
    )
    assume(outbound.quantity_after > ZERO)

    assert outbound.quantity_after > ZERO
    assert outbound.value_after >= ZERO
    assert outbound.average_after >= ZERO


@given(quantity=quantities, unit_cost=unit_costs)
def test_zero_quantity_implies_zero_value_after_any_single_step(
    quantity: Decimal, unit_cost: Decimal
) -> None:
    inbound = apply_inbound(
        quantity=quantity, unit_cost=unit_cost, before_quantity=ZERO, before_value=ZERO
    )
    for step in (
        inbound,
        apply_outbound(
            quantity=inbound.quantity_after,
            before_quantity=inbound.quantity_after,
            before_value=inbound.value_after,
        ),
    ):
        if step.quantity_after == ZERO:
            assert step.value_after == ZERO
            assert step.average_after == ZERO
