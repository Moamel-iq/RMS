"""
Proportional allocation.

Two invariants: the parts sum exactly to the whole, and the same economic
input always produces the same result regardless of the order it arrives in.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from hypothesis import assume, given
from hypothesis import strategies as st

from apps.core.allocation import AllocationItem, AllocationResult, allocate, allocate_by_rate
from apps.core.money import quantize_money


def items(*weights: str, start: int = 1) -> list[AllocationItem]:
    """Build items with explicit, deterministic sequences."""
    return [
        AllocationItem(sequence=start + offset, weight=Decimal(weight))
        for offset, weight in enumerate(weights)
    ]


def amounts(results: list[AllocationResult]) -> list[Decimal]:
    return [result.amount for result in results]


class TestTheSumGuarantee:
    def test_thirds_of_a_thousand_still_sum_to_a_thousand(self) -> None:
        """
        The case naive allocation gets wrong: 1000/3 three ways rounds to
        333.333 each, summing to 999.999. The missing 0.001 must land
        somewhere deliberate.
        """
        results = allocate("1000", items("1", "1", "1"))
        assert sum(amounts(results)) == Decimal("1000.000")
        assert amounts(results) == [
            Decimal("333.334"),
            Decimal("333.333"),
            Decimal("333.333"),
        ]

    def test_sevenths_sum_exactly(self) -> None:
        assert sum(amounts(allocate("100", items(*["1"] * 7)))) == Decimal("100.000")

    def test_uneven_weights_sum_exactly(self) -> None:
        results = allocate("1000", items("3", "5", "7", "11"))
        assert sum(amounts(results)) == Decimal("1000.000")

    def test_allocation_is_proportional(self) -> None:
        assert amounts(allocate("1000", items("1", "3"))) == [
            Decimal("250.000"),
            Decimal("750.000"),
        ]


class TestSequenceIsTheTieBreak:
    def test_results_come_back_ordered_by_sequence(self) -> None:
        scrambled = [
            AllocationItem(sequence=3, weight=Decimal("1")),
            AllocationItem(sequence=1, weight=Decimal("1")),
            AllocationItem(sequence=2, weight=Decimal("1")),
        ]
        results = allocate("1000", scrambled)
        assert [result.sequence for result in results] == [1, 2, 3]

    def test_caller_order_does_not_change_the_outcome(self) -> None:
        """
        The whole point. The same economic input passed in any order must
        allocate identically — a queryset's implicit ordering cannot be
        allowed to move a dinar.
        """
        forward = [
            AllocationItem(sequence=1, weight=Decimal("1")),
            AllocationItem(sequence=2, weight=Decimal("1")),
            AllocationItem(sequence=3, weight=Decimal("1")),
        ]
        reversed_order = list(reversed(forward))
        assert allocate("1000", forward) == allocate("1000", reversed_order)

    def test_residual_goes_to_the_lowest_sequence_on_a_tie(self) -> None:
        """Three equal weights, one spare quantum, tie on remainder."""
        results = allocate("1000", items("1", "1", "1"))
        by_sequence = {result.sequence: result.amount for result in results}
        assert by_sequence[1] > by_sequence[2]
        assert by_sequence[2] == by_sequence[3]

    def test_sequence_not_position_decides_the_tie(self) -> None:
        """Sequence 1 wins the residual even when passed last."""
        scrambled = [
            AllocationItem(sequence=9, weight=Decimal("1")),
            AllocationItem(sequence=5, weight=Decimal("1")),
            AllocationItem(sequence=1, weight=Decimal("1")),
        ]
        by_sequence = {r.sequence: r.amount for r in allocate("1000", scrambled)}
        assert by_sequence[1] == Decimal("333.334")
        assert by_sequence[5] == Decimal("333.333")
        assert by_sequence[9] == Decimal("333.333")


class TestSequenceValidation:
    def test_duplicate_sequence_is_refused(self) -> None:
        duplicated = [
            AllocationItem(sequence=1, weight=Decimal("1")),
            AllocationItem(sequence=1, weight=Decimal("1")),
        ]
        with pytest.raises(ValidationError) as exc:
            allocate("100", duplicated)
        assert exc.value.code == "duplicate_allocation_sequence"

    def test_negative_sequence_is_refused(self) -> None:
        with pytest.raises(ValidationError) as exc:
            allocate("100", [AllocationItem(sequence=-1, weight=Decimal("1"))])
        assert exc.value.code == "invalid_allocation_sequence"

    @pytest.mark.parametrize("bad", [None, "1", 1.0, True])
    def test_non_integer_sequence_is_refused(self, bad: object) -> None:
        with pytest.raises(ValidationError) as exc:
            allocate("100", [AllocationItem(sequence=bad, weight=Decimal("1"))])  # type: ignore[arg-type]
        assert exc.value.code == "invalid_allocation_sequence"

    def test_zero_is_a_valid_sequence(self) -> None:
        """Zero-based line numbering is legitimate; only negatives are not."""
        results = allocate("100", [AllocationItem(sequence=0, weight=Decimal("1"))])
        assert amounts(results) == [Decimal("100.000")]


class TestSignHandling:
    def test_a_credit_note_allocates_the_mirror_of_its_invoice(self) -> None:
        """A reversal must undo the original exactly, line for line."""
        invoice = allocate("1000", items("1", "1", "1"))
        credit = allocate("-1000", items("1", "1", "1"))
        assert amounts(credit) == [-amount for amount in amounts(invoice)]
        assert sum(amounts(invoice)) + sum(amounts(credit)) == Decimal("0.000")

    def test_negative_totals_sum_exactly(self) -> None:
        assert sum(amounts(allocate("-1000", items("3", "5", "7")))) == Decimal("-1000.000")


class TestEdgeCases:
    def test_zero_total_allocates_zero_everywhere(self) -> None:
        assert amounts(allocate("0", items("1", "2", "3"))) == [Decimal("0.000")] * 3

    def test_single_line_receives_everything(self) -> None:
        assert amounts(allocate("1234.567", items("1"))) == [Decimal("1234.567")]

    def test_a_zero_weight_line_normally_receives_nothing(self) -> None:
        results = allocate("1000", items("1", "0", "1"))
        by_sequence = {r.sequence: r.amount for r in results}
        assert by_sequence[2] == Decimal("0.000")
        assert sum(amounts(results)) == Decimal("1000.000")

    def test_amount_smaller_than_the_line_count(self) -> None:
        """0.002 IQD across five lines: two get a quantum, three get none."""
        results = allocate("0.002", items(*["1"] * 5))
        assert sum(amounts(results)) == Decimal("0.002")
        assert sorted(amounts(results), reverse=True)[:2] == [
            Decimal("0.001"),
            Decimal("0.001"),
        ]

    def test_no_lines_is_refused(self) -> None:
        with pytest.raises(ValidationError) as exc:
            allocate("100", [])
        assert exc.value.code == "no_lines_to_allocate"

    def test_all_zero_weights_is_refused(self) -> None:
        """There is no proportional answer; failing beats inventing one."""
        with pytest.raises(ValidationError) as exc:
            allocate("100", items("0", "0"))
        assert exc.value.code == "zero_allocation_weights"

    def test_negative_weight_is_refused(self) -> None:
        with pytest.raises(ValidationError) as exc:
            allocate("100", items("1", "-1"))
        assert exc.value.code == "negative_allocation_weight"

    def test_float_weights_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            allocate("100", [AllocationItem(sequence=1, weight=1.5)])  # type: ignore[arg-type]


class TestRealCases:
    def test_delivery_application_commission(self) -> None:
        """
        17.5% commission on a 47,350 IQD order across three items. The parts
        must reconcile to the commission the application actually charges.
        """
        commission = quantize_money(Decimal("47350") * Decimal("0.175"))
        results = allocate_by_rate("47350", items("21000", "17350", "9000"), rate="0.175")
        assert sum(amounts(results)) == commission
        assert commission == Decimal("8286.250")

    def test_document_discount_spread_over_lines(self) -> None:
        results = allocate("2000", items("12500", "7250", "3300"))
        assert sum(amounts(results)) == Decimal("2000.000")

    def test_landed_freight_spread_by_weight(self) -> None:
        """Freight of 75,000 IQD split across receipt lines by kilogram."""
        results = allocate("75000", items("30", "12.5", "7.5"))
        assert sum(amounts(results)) == Decimal("75000.000")

    def test_rate_is_applied_to_the_total_not_line_by_line(self) -> None:
        lines = ["333.333", "333.333", "333.334"]
        results = allocate_by_rate("1000", items(*lines), rate="0.175")
        assert sum(amounts(results)) == quantize_money(Decimal("1000") * Decimal("0.175"))


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
        built = [
            AllocationItem(sequence=index, weight=weight) for index, weight in enumerate(weights)
        ]
        assert sum(amounts(allocate(total, built))) == quantize_money(total)

    @given(
        total=st.decimals(
            min_value=Decimal("-100000"),
            max_value=Decimal("100000"),
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
    def test_shuffling_the_input_never_changes_the_result(
        self, total: Decimal, weights: list[Decimal]
    ) -> None:
        """Determinism, asserted over generated inputs rather than one example."""
        built = [
            AllocationItem(sequence=index, weight=weight) for index, weight in enumerate(weights)
        ]
        assert allocate(total, built) == allocate(total, list(reversed(built)))

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
        built = [
            AllocationItem(sequence=index, weight=weight) for index, weight in enumerate(weights)
        ]
        results = allocate(total, built)
        weight_total = sum(weights)
        for result, weight in zip(results, weights, strict=True):
            exact = quantize_money(total) * weight / weight_total
            assert abs(result.amount - exact) <= Decimal("0.001")

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
        built = [
            AllocationItem(sequence=index, weight=weight) for index, weight in enumerate(weights)
        ]
        forward = allocate(total, built)
        backward = allocate(-total, built)
        assert [-result.amount for result in forward] == amounts(backward)
