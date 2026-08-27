"""
Monetary precision policy.

Money must share no rounding behaviour with quantities. These tests pin the
policy and prove the separation.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.core.money import (
    _ISOLATE_END,
    _NO_BREAK_SPACE,
    _RTL_ISOLATE_START,
    CASH_ROUNDING_ENABLED,
    CURRENCY_CODE,
    MONEY_DISPLAY_PLACES,
    MONEY_PLACES,
    MONEY_ROUNDING,
    RATE_PLACES,
    UNIT_PRICE_PLACES,
    apply_cash_settlement_rounding,
    money_audit,
    money_display,
    money_export,
    quantize_money,
    quantize_rate,
    quantize_unit_price,
)
from apps.core.quantity import QUANTITY_PLACES, decimal_places_of
from apps.core.templatetags.money_tags import (
    iqd_filter,
    iqd_full_filter,
    money_filter,
    money_full_filter,
)


class TestPolicyConstants:
    def test_constants_match_the_approved_policy(self) -> None:
        assert MONEY_PLACES == 3
        assert MONEY_DISPLAY_PLACES == 0
        assert UNIT_PRICE_PLACES == 6
        assert RATE_PLACES == 6
        assert MONEY_ROUNDING == ROUND_HALF_UP
        assert CURRENCY_CODE == "IQD"

    def test_money_precision_is_independent_of_quantity_precision(self) -> None:
        """
        They happen to be equal today. The test exists so that changing one
        does not silently change the other — they are separate policies.
        """
        assert MONEY_PLACES == QUANTITY_PLACES
        assert UNIT_PRICE_PLACES > MONEY_PLACES


class TestPostedAmountPrecision:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1000", "1000.000"),
            ("1000.0004", "1000.000"),
            ("1000.0005", "1000.001"),
            ("-1000.0005", "-1000.001"),
            ("0.0005", "0.001"),
        ],
    )
    def test_posted_amounts_round_half_up_away_from_zero(self, raw: str, expected: str) -> None:
        assert quantize_money(raw) == Decimal(expected)

    def test_rounding_is_symmetric_so_reversals_cancel(self) -> None:
        original = quantize_money("1250.0005")
        reversal = quantize_money("-1250.0005")
        assert original + reversal == Decimal("0.000")

    def test_result_always_carries_three_decimal_places(self) -> None:
        assert decimal_places_of(quantize_money("1000")) == 3

    def test_floats_are_refused(self) -> None:
        with pytest.raises(ValidationError) as exc:
            quantize_money(1000.5)
        assert exc.value.code == "float_in_quantity_path"

    def test_absurd_magnitudes_raise_a_clear_error(self) -> None:
        with pytest.raises(ValidationError) as exc:
            quantize_money("1E+40")
        assert exc.value.code == "amount_out_of_range"


class TestHigherInternalPrecision:
    def test_unit_prices_keep_six_places(self) -> None:
        """
        A per-gram cost times a recipe quantity must not have been pre-rounded
        into the answer.
        """
        assert quantize_unit_price("0.0000005") == Decimal("0.000001")
        assert decimal_places_of(quantize_unit_price("1")) == 6

    def test_rates_keep_six_places(self) -> None:
        """A 17.5% commission is 0.175000, not 0.175."""
        assert quantize_rate("0.175") == Decimal("0.175000")
        assert decimal_places_of(quantize_rate("0.175")) == 6

    def test_a_unit_price_is_finer_than_a_posted_amount(self) -> None:
        price = quantize_unit_price("12.3456785")
        assert price == Decimal("12.345679")
        assert quantize_money(price) == Decimal("12.346")


class TestRendering:
    """
    Renderers return strings, not Decimals. That is the enforcement: a
    rendered amount cannot be summed, compared numerically, or written back to
    a column, so a report physically cannot derive a discrepancy from display
    rounding.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1250.000", "1,250"),
            ("1250.001", "1,250"),
            ("1250.400", "1,250"),
            ("1250.500", "1,251"),
            ("-1250.500", "-1,251"),
            ("1234567.890", "1,234,568"),
        ],
    )
    def test_normal_ui_shows_whole_dinars(self, raw: str, expected: str) -> None:
        assert money_display(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1250.001", "1,250.001"), ("1250.000", "1,250.000"), ("-980.250", "-980.250")],
    )
    def test_audit_views_expose_the_stored_third_decimal(self, raw: str, expected: str) -> None:
        """
        The worked example from the decision: stored 1250.001 shows as 1,250
        on a normal screen and 1,250.001 on a reconciliation screen.
        """
        assert money_audit(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1250.001", "1250.001"), ("1234567.890", "1234567.890")],
    )
    def test_exports_keep_full_precision_and_drop_grouping(self, raw: str, expected: str) -> None:
        """A thousands separator inside a CSV field breaks the delimiter."""
        assert money_export(raw) == expected

    def test_renderers_return_strings_not_decimals(self) -> None:
        """The structural guard: rendered money cannot enter arithmetic."""
        for rendered in (
            money_display("1250.001"),
            money_audit("1250.001"),
            money_export("1250.001"),
        ):
            assert isinstance(rendered, str)

    def test_iqd_template_filter_groups_and_labels_operational_money(self) -> None:
        assert iqd_filter(Decimal("1234567.000")) == (
            f"{_RTL_ISOLATE_START}1,234,567 دينار{_NO_BREAK_SPACE}عراقي{_ISOLATE_END}"
        )

    def test_iqd_full_template_filter_keeps_audit_precision(self) -> None:
        assert iqd_full_filter(Decimal("1234567.890")) == (
            f"{_RTL_ISOLATE_START}1,234,567.890 دينار{_NO_BREAK_SPACE}عراقي{_ISOLATE_END}"
        )

    def test_standard_money_filters_also_name_the_currency(self) -> None:
        assert money_filter(Decimal("1250.001")) == (
            f"{_RTL_ISOLATE_START}1,250 دينار{_NO_BREAK_SPACE}عراقي{_ISOLATE_END}"
        )
        assert money_full_filter(Decimal("1250.001")) == (
            f"{_RTL_ISOLATE_START}1,250.001 دينار{_NO_BREAK_SPACE}عراقي{_ISOLATE_END}"
        )

    def test_rendered_values_cannot_be_summed_as_money(self) -> None:
        """
        Summing rendered strings concatenates rather than adding, so the
        mistake is loud instead of producing a plausible wrong total.
        """
        lines = [Decimal("333.333"), Decimal("333.333"), Decimal("333.334")]
        assert sum(lines) == Decimal("1000.000")
        with pytest.raises(TypeError):
            sum(money_display(line) for line in lines)  # type: ignore[misc]

    def test_reconciliation_must_compare_stored_values(self) -> None:
        """
        Three lines that reconcile exactly on stored values would appear to be
        1 IQD short if compared after display rounding.
        """
        lines = [Decimal("333.333"), Decimal("333.333"), Decimal("333.334")]
        total = Decimal("1000.000")
        assert sum(lines) == total  # correct: stored Decimals
        displayed_sum = sum(Decimal(money_display(line).replace(",", "")) for line in lines)
        assert displayed_sum != total  # what a display-based check would claim
        assert displayed_sum == Decimal("999")


class TestCashRoundingIsOff:
    def test_cash_rounding_is_disabled_by_default(self) -> None:
        """
        Nearest-250 must never touch sales, invoices, payroll, COGS, or
        journal entries. It is off, and this test is the tripwire.
        """
        assert CASH_ROUNDING_ENABLED is False

    def test_disabled_rounding_is_an_identity_with_no_adjustment(self) -> None:
        rounded, adjustment = apply_cash_settlement_rounding("1234.567")
        assert rounded == Decimal("1234.567")
        assert adjustment == Decimal("0.000")

    def test_the_adjustment_always_reconciles(self) -> None:
        """rounded == payable + adjustment, whatever the setting."""
        for raw in ["0", "1", "1234.567", "-980.250"]:
            rounded, adjustment = apply_cash_settlement_rounding(raw)
            assert rounded == quantize_money(raw) + adjustment
