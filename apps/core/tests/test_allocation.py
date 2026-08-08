"""
Proportional allocation.

The single invariant that matters: the parts sum exactly to the whole, for
every input. Everything else is a detail of how the residual is placed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from hypothesis import assume, given
from hypothesis import strategies as st

from apps.core.allocation import allocate_by_rate, allocate_proportionally
from apps.core.money import quantize_money


class TestTheGuarantee:
    def test_thirds_of_a_thousand_still_sum_to_a_thousand(self) -> None:
        """
        The case naive allocation gets wrong: 1000/3 three ways rounds to
        333.333 each, summing to 999.999. The missing 0.001 must land
        somewhere deliberate.
        """
        parts = allocate_proportionally("1000", ["1", "1", "1"])
        assert sum(parts) == Decimal("1000.000")
        assert parts == [Decimal("333.334"), Decimal("333.333"), Decimal("333.333")]

    def test_sevenths_sum_exactly(self) -> None:
        parts = allocate_proportionally("100", ["1"] * 7)
        assert sum(parts) == Decimal("100.000")

    def test_uneven_weights_sum_exactly(self) -> None:
        parts = allocate_proportionally("1000", ["3", "5", "7", "11"])
        assert sum(parts) == Decimal("1000.000")

    def test_allocation_is_proportional(self) -> None:
        parts = allocate_proportionally("1000", ["1", "3"])
        assert parts == [Decimal("250.000"), Decimal("750.000")]


class TestDeterminism:
    def test_the_same_input_always_gives_the_same_output(self) -> None:
        first = allocate_proportionally("1000", ["1", "1", "1"])
        second = allocate_proportionally("1000", ["1", "1", "1"])
        assert first == second

    def test_ties_break_on_line_order(self) -> None:
        """
        Three equal weights, one spare quantum. It goes to the first line,
        not an arbitrary one.
        """
        parts = allocate_proportionally("1000", ["1", "1", "1"])
        assert parts[0] > parts[1]
        assert parts[1] == parts[2]

    def test_reordering_lines_moves_the_residual_predictably(self) -> None:
        parts = allocate_proportionally("100", ["1", "2", "2"])
        reordered = allocate_proportionally("100", ["2", "2", "1"])
        assert sum(parts) == sum(reordered) == Decimal("100.000")


class TestSignHandling:
    def test_a_credit_note_allocates_the_mirror_of_its_invoice(self) -> None:
        """A reversal must undo the original exactly, line for line."""
        invoice = allocate_proportionally("1000", ["1", "1", "1"])
        credit = allocate_proportionally("-1000", ["1", "1", "1"])
        assert credit == [-part for part in invoice]
        assert sum(invoice) + sum(credit) == Decimal("0.000")

    def test_negative_totals_sum_exactly(self) -> None:
        parts = allocate_proportionally("-1000", ["3", "5", "7"])
        assert sum(parts) == Decimal("-1000.000")


class TestEdgeCases:
    def test_zero_total_allocates_zero_everywhere(self) -> None:
        parts = allocate_proportionally("0", ["1", "2", "3"])
        assert parts == [Decimal("0.000")] * 3

    def test_single_line_receives_everything(self) -> None:
        assert allocate_proportionally("1234.567", ["1"]) == [Decimal("1234.567")]

    def test_a_zero_weight_line_normally_receives_nothing(self) -> None:
        parts = allocate_proportionally("1000", ["1", "0", "1"])
        assert parts[1] == Decimal("0.000")
        assert sum(parts) == Decimal("1000.000")

    def test_amount_smaller_than_the_line_count(self) -> None:
        """0.002 IQD across five lines. Two lines get a quantum, three get none."""
        parts = allocate_proportionally("0.002", ["1"] * 5)
        assert sum(parts) == Decimal("0.002")
        assert sorted(parts, reverse=True)[:2] == [Decimal("0.001"), Decimal("0.001")]

    def test_no_lines_is_refused(self) -> None:
        with pytest.raises(ValidationError) as exc:
            allocate_proportionally("100", [])
        assert exc.value.code == "no_lines_to_allocate"

    def test_all_zero_weights_is_refused(self) -> None:
        """There is no proportional answer; failing beats inventing one."""
        with pytest.raises(ValidationError) as exc:
            allocate_proportionally("100", ["0", "0"])
        assert exc.value.code == "zero_allocation_weights"

    def test_negative_weight_is_refused(self) -> None:
        with pytest.raises(ValidationError) as exc:
            allocate_proportionally("100", ["1", "-1"])
        assert exc.value.code == "negative_allocation_weight"

    def test_float_weights_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            allocate_proportionally("100", [1.5, 2.5])


class TestRealCases:
    def test_delivery_application_commission(self) -> None:
        """
        A 17.5% commission on a 47,350 IQD order spread over three items.
        The parts must reconcile to the commission the application charges.
        """
        commission = quantize_money(Decimal("47350") * Decimal("0.175"))
        parts = allocate_by_rate("47350", ["21000", "17350", "9000"], rate="0.175")
        assert sum(parts) == commission
        assert commission == Decimal("8286.250")

    def test_document_discount_spread_over_lines(self) -> None:
        lines = ["12500", "7250", "3300"]
        parts = allocate_proportionally("2000", lines)
        assert sum(parts) == Decimal("2000.000")

    def test_landed_freight_spread_by_weight(self) -> None:
        """Freight of 75,000 IQD split across receipt lines by kilogram."""
        parts = allocate_proportionally("75000", ["30", "12.5", "7.5"])
        assert sum(parts) == Decimal("75000.000")

    def test_rate_is_applied_to_the_total_not_line_by_line(self) -> None:
        """
        Rating each line separately then rounding gives a different answer
        from rating the total. The total is authoritative.
        """
        lines = ["333.333", "333.333", "333.334"]
        via_total = allocate_by_rate("1000", lines, rate="0.175")
        line_by_line = sum(quantize_money(Decimal(line) * Decimal("0.175")) for line in lines)
        assert sum(via_total) == quantize_money(Decimal("1000") * Decimal("0.175"))
        assert sum(via_total) >= line_by_line - Decimal("0.002")


class TestAllocationProperties:
    @given(
        total=st.decimals(
            min_value=Decimal("-1000000"),
            max_value=Decimal("1000000"),
            allow_nan=False,
            allow_infinity=False,
            places=3,
        ),
        weights=st.lists(
            st.decimals(
                min_value=Decimal("0"),
                max_value=Decimal("100000"),
                allow_nan=False,
                allow_infinity=False,
                places=3,
            ),
            min_size=1,
            max_size=12,
        ),
    )
    def test_parts_always_sum_to_the_whole(self, total: Decimal, weights: list[Decimal]) -> None:
        """The invariant. If this ever fails, money has been created or lost."""
        # assume(), not pytest.skip(): skip inside @given abandons the whole
        # property, so the invariant would silently never be exercised.
        assume(sum(weights) != 0)
        parts = allocate_proportionally(total, weights)
        assert sum(parts) == quantize_money(total)

    @given(
        total=st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal("1000000"),
            allow_nan=False,
            allow_infinity=False,
            places=3,
        ),
        weights=st.lists(
            st.decimals(
                min_value=Decimal("0.001"),
                max_value=Decimal("10000"),
                allow_nan=False,
                allow_infinity=False,
                places=3,
            ),
            min_size=1,
            max_size=10,
        ),
    )
    def test_no_part_exceeds_the_whole_and_none_is_negative(
        self, total: Decimal, weights: list[Decimal]
    ) -> None:
        parts = allocate_proportionally(total, weights)
        assert all(part >= 0 for part in parts)
        assert all(part <= quantize_money(total) for part in parts)

    @given(
        total=st.decimals(
            min_value=Decimal("0.001"),
            max_value=Decimal("100000"),
            allow_nan=False,
            allow_infinity=False,
            places=3,
        ),
        weights=st.lists(
            st.decimals(
                min_value=Decimal("0.001"),
                max_value=Decimal("1000"),
                allow_nan=False,
                allow_infinity=False,
                places=3,
            ),
            min_size=2,
            max_size=8,
        ),
    )
    def test_each_part_is_within_one_quantum_of_its_exact_share(
        self, total: Decimal, weights: list[Decimal]
    ) -> None:
        """Largest remainder never moves a line by more than 0.001."""
        parts = allocate_proportionally(total, weights)
        weight_total = sum(weights)
        for part, weight in zip(parts, weights, strict=True):
            exact = quantize_money(total) * weight / weight_total
            assert abs(part - exact) <= Decimal("0.001")

    @given(
        total=st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal("100000"),
            allow_nan=False,
            allow_infinity=False,
            places=3,
        ),
        weights=st.lists(
            st.decimals(
                min_value=Decimal("0.001"),
                max_value=Decimal("1000"),
                allow_nan=False,
                allow_infinity=False,
                places=3,
            ),
            min_size=1,
            max_size=8,
        ),
    )
    def test_reversal_is_the_exact_mirror(self, total: Decimal, weights: list[Decimal]) -> None:
        """Posted records are corrected by reversal, so this must hold."""
        forward = allocate_proportionally(total, weights)
        backward = allocate_proportionally(-total, weights)
        assert [-part for part in forward] == backward
