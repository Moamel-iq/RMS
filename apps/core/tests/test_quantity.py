"""
Quantity precision and rounding.

These tests pin the policy in both directions, including the cases where a
plausible misreading of "3 decimal places, ROUND_HALF_UP" produces a
different number.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest
from django.core.exceptions import ValidationError
from hypothesis import given
from hypothesis import strategies as st

from apps.core.quantity import (
    CALCULATION_PLACES,
    FACTOR_PLACES,
    QUANTITY_PLACES,
    QUANTITY_ROUNDING,
    decimal_places_of,
    ensure_decimal,
    is_quantized_to_quantity,
    quantize_calculation,
    quantize_factor,
    quantize_quantity,
)


class TestPolicyConstants:
    def test_declared_precision_matches_the_written_policy(self) -> None:
        assert QUANTITY_PLACES == 3
        assert CALCULATION_PLACES == 6
        assert QUANTITY_ROUNDING == ROUND_HALF_UP

    def test_factor_precision_stores_an_ounce_exactly(self) -> None:
        """
        One ounce is 0.028349523125 kg — twelve decimals. A factor truncated
        below that is wrong in every conversion that uses it, permanently.
        """
        assert FACTOR_PLACES >= 12
        assert quantize_factor("0.028349523125") == Decimal("0.028349523125")


class TestRoundingDirection:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.0004", "1.000"),
            ("1.0005", "1.001"),
            ("1.0006", "1.001"),
            ("1.0015", "1.002"),
            ("0.0005", "0.001"),
            ("0.0004", "0.000"),
        ],
    )
    def test_positive_ties_round_up(self, raw: str, expected: str) -> None:
        assert quantize_quantity(raw) == Decimal(expected)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("-1.0005", "-1.001"),
            ("-0.0005", "-0.001"),
            ("-1.0015", "-1.002"),
            ("-1.0004", "-1.000"),
        ],
    )
    def test_negative_ties_round_away_from_zero(self, raw: str, expected: str) -> None:
        """
        ROUND_HALF_UP means "away from zero", NOT "toward positive infinity".
        This matters for reversals: a correcting entry must cancel the
        original exactly, which only holds if rounding is symmetric in
        magnitude.
        """
        assert quantize_quantity(raw) == Decimal(expected)

    def test_rounding_is_symmetric_in_magnitude(self) -> None:
        for raw in ["1.0005", "2.4445", "0.0005", "99.9995"]:
            assert quantize_quantity(raw) == -quantize_quantity(f"-{raw}")

    def test_is_not_bankers_rounding(self) -> None:
        """Python's own default is ROUND_HALF_EVEN, which would give 1.000."""
        assert quantize_quantity("1.0005") == Decimal("1.001")
        assert quantize_quantity("1.0025") == Decimal("1.003")


class TestNoDoubleRounding:
    """
    "Carry 6dp internally, then round to 3dp" must not be implemented as two
    quantize steps. These inputs prove why.
    """

    @pytest.mark.parametrize("raw", ["1.00049999", "0.00049999", "1.0004999999", "2.99949999"])
    def test_quantizing_once_differs_from_quantizing_twice(self, raw: str) -> None:
        value = Decimal(raw)
        once = quantize_quantity(value)
        twice = quantize_quantity(quantize_calculation(value))
        assert once != twice, f"{raw} needs to distinguish the two paths"

    def test_the_correct_answer_is_the_single_rounding(self) -> None:
        """1.00049999 is below the 1.0005 tie, so it rounds down."""
        assert quantize_quantity("1.00049999") == Decimal("1.000")

    def test_calculation_precision_is_a_floor_not_a_rounding_step(self) -> None:
        """Six decimals is what an intermediate is *stored* at, if stored."""
        assert quantize_calculation("1.0004999999") == Decimal("1.000500")


class TestFloatRejection:
    def test_float_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            quantize_quantity(1.5)
        assert exc.value.code == "float_in_quantity_path"

    def test_float_zero_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ensure_decimal(0.0)

    def test_the_reason_floats_are_refused(self) -> None:
        """Decimal(0.1) is not 0.1, and a quantity path must not absorb that."""
        assert Decimal(0.1) != Decimal("0.1")

    def test_bool_is_rejected_even_though_it_is_an_int(self) -> None:
        with pytest.raises(ValidationError):
            ensure_decimal(True)

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "sNaN"])
    def test_non_finite_strings_are_rejected_by_shape(self, value: str) -> None:
        """
        These never reach the finiteness check: the numeric shape test refuses
        them first. Rejected either way, which is what matters.
        """
        with pytest.raises(ValidationError) as exc:
            ensure_decimal(value)
        assert exc.value.code == "invalid_quantity"

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_decimals_are_rejected(self, value: str) -> None:
        """
        A Decimal built elsewhere can still carry NaN or Infinity. A NaN in a
        stock balance can never be reconciled, because NaN != NaN.
        """
        with pytest.raises(ValidationError) as exc:
            ensure_decimal(Decimal(value))
        assert exc.value.code == "non_finite_quantity"

    @pytest.mark.parametrize("value", ["", "abc", "1.2.3", "1,5", "--1", "1 2"])
    def test_unparseable_strings_are_rejected(self, value: str) -> None:
        with pytest.raises(ValidationError):
            ensure_decimal(value)

    def test_none_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ensure_decimal(None)


class TestArabicNumerals:
    """
    Python's Decimal accepts any Unicode decimal digit, so this behaviour has
    to be chosen rather than inherited. Operators here may type Arabic-Indic
    numerals, so those are accepted; a mixed-script number is damage and is
    refused.
    """

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("١٢٣", "123"),
            ("١٢٣.٤٥٦", "123.456"),
            ("٠", "0"),
            ("۱۲۳", "123"),  # Persian / Extended Arabic-Indic
            ("۱۲۳.۴۵۶", "123.456"),
        ],
    )
    def test_arabic_numerals_are_accepted(self, written: str, expected: str) -> None:
        assert ensure_decimal(written) == Decimal(expected)

    def test_arabic_decimal_separator_is_understood(self) -> None:
        assert ensure_decimal("١٢٣٫٤٥٦") == Decimal("123.456")

    @pytest.mark.parametrize("written", ["1٢3", "١2٣", "12٣.4", "۱2۳"])
    def test_mixed_digit_scripts_are_refused(self, written: str) -> None:
        """
        CPython reads "1٢3" as 123. An OCR'd supplier invoice that produces
        that is far more likely mis-segmented than genuinely mixed, and
        accepting it would post a quantity nobody typed.
        """
        with pytest.raises(ValidationError) as exc:
            ensure_decimal(written)
        assert exc.value.code == "mixed_digit_scripts"

    def test_arabic_numerals_round_by_the_same_policy(self) -> None:
        assert quantize_quantity("١.٠٠٠٥") == Decimal("1.001")


class TestAcceptedInput:
    def test_decimal_passes_through(self) -> None:
        assert ensure_decimal(Decimal("1.5")) == Decimal("1.5")

    def test_int_is_accepted(self) -> None:
        assert ensure_decimal(7) == Decimal("7")

    def test_str_is_accepted(self) -> None:
        assert ensure_decimal("1.5") == Decimal("1.5")


class TestOutOfRange:
    def test_absurdly_large_values_raise_a_clear_error(self) -> None:
        """
        quantize() raises InvalidOperation on huge magnitudes. Leaking that to
        an operator tells them nothing.
        """
        with pytest.raises(ValidationError) as exc:
            quantize_quantity("1E+30")
        assert exc.value.code == "quantity_out_of_range"


class TestQuantizedShape:
    def test_result_always_carries_three_decimal_places(self) -> None:
        for raw in ["1", "1.5", "0", "-2.25"]:
            assert decimal_places_of(quantize_quantity(raw)) == QUANTITY_PLACES

    def test_quantization_is_idempotent(self) -> None:
        once = quantize_quantity("7.6665")
        assert quantize_quantity(once) == once

    def test_detects_an_already_quantized_value(self) -> None:
        assert is_quantized_to_quantity(Decimal("1.234"))
        assert not is_quantized_to_quantity(Decimal("1.2345"))


class TestQuantityProperties:
    @given(
        st.decimals(
            min_value=Decimal("-1000000"),
            max_value=Decimal("1000000"),
            allow_nan=False,
            allow_infinity=False,
            places=8,
        )
    )
    def test_quantizing_never_moves_a_value_by_more_than_half_a_step(self, value: Decimal) -> None:
        assert abs(quantize_quantity(value) - value) <= Decimal("0.0005")

    @given(
        st.decimals(
            min_value=Decimal("-1000000"),
            max_value=Decimal("1000000"),
            allow_nan=False,
            allow_infinity=False,
            places=8,
        )
    )
    def test_quantizing_is_idempotent_for_any_value(self, value: Decimal) -> None:
        once = quantize_quantity(value)
        assert quantize_quantity(once) == once

    @given(
        st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal("1000000"),
            allow_nan=False,
            allow_infinity=False,
            places=8,
        )
    )
    def test_negation_commutes_with_quantization(self, value: Decimal) -> None:
        """Required for reversals to cancel their originals exactly."""
        assert quantize_quantity(-value) == -quantize_quantity(value)


class TestTheQuantityTemplateFilter:
    """
    The filter exists because the two obvious alternatives are both wrong.

    `floatformat` converts through float, which the whole policy forbids.
    `stringformat:"f"` is printf: it also goes through float *and* prints six
    decimals whatever the column stores, so a stored `60.000` renders as
    `60.000000` — a precision the system does not have.
    """

    def test_it_renders_one_operational_display_place(self) -> None:
        from apps.core.templatetags.quantity_tags import quantity_filter

        assert quantity_filter(Decimal("60.000")) == "60.0"
        assert quantity_filter(Decimal("60")) == "60.0"
        assert quantity_filter(Decimal("17.4")) == "17.4"
        assert quantity_filter(Decimal("15.55")) == "15.6"

    def test_it_never_prints_six_places_the_way_printf_does(self) -> None:
        from apps.core.templatetags.quantity_tags import quantity_filter

        # What `stringformat:"f"` does, spelled out. Built by concatenation
        # because ruff rightly refuses percent formatting in new code, and the
        # point here is to pin the behaviour rather than to use it.
        printf = "%" + "f"
        assert printf % Decimal("60.000") == "60.000000"  # the bug being avoided
        assert quantity_filter(Decimal("60.000")) == "60.0"

    def test_it_is_ungrouped_and_locale_independent(self) -> None:
        """
        A quantity sits beside conversion factors and item codes. Django would
        localise a Decimal under Arabic to `60,000`, and that comma is
        ambiguous enough to invite a mis-typed re-entry.
        """
        from apps.core.templatetags.quantity_tags import quantity_filter

        assert quantity_filter(Decimal("60000.000")) == "60000.0"

    def test_it_rounds_the_way_the_kernel_does(self) -> None:
        from apps.core.templatetags.quantity_tags import quantity_filter

        assert quantity_filter(Decimal("0.05")) == "0.1"
        assert quantity_filter(Decimal("-0.05")) == "-0.1"

    def test_nothing_renders_as_nothing(self) -> None:
        from apps.core.templatetags.quantity_tags import quantity_filter

        assert quantity_filter(None) == ""
        assert quantity_filter("") == ""

    def test_a_string_quantity_is_accepted_and_a_float_is_not(self) -> None:
        """`ensure_decimal` is the gate; a float would already have lost digits."""
        from apps.core.templatetags.quantity_tags import quantity_filter

        assert quantity_filter("17.4") == "17.4"
        with pytest.raises(ValidationError):
            quantity_filter(17.4)
