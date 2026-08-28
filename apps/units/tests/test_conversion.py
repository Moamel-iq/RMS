"""
Unit conversion.

The golden cases use real Khan Mandi numbers and are duplicated in
docs/testing/golden-cases/units-conversion.md, hand-calculated.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apps.core.quantity import decimal_places_of, quantize_quantity
from apps.units.models import Dimension, UnitOfMeasure
from apps.units.selectors import unit_by_code
from apps.units.services import convert, convert_to_stored_quantity, from_base, to_base

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded() -> None:
    call_command("seed_units", verbosity=0)


class TestGoldenCases:
    """Hand-calculated cases from real Khan Mandi operations."""

    def test_one_rice_sack_weight_expressed_in_grams(self, seeded: None) -> None:
        """A 30 kg sack of rice, in grams: 30 * 1000 = 30,000."""
        result = convert_to_stored_quantity(
            "30", from_unit=unit_by_code("KG"), to_unit=unit_by_code("G")
        )
        assert result == Decimal("30000.000")

    def test_recipe_spice_quantity_grams_to_kilograms(self, seeded: None) -> None:
        """7 g of spice per portion is 0.007 kg."""
        result = convert_to_stored_quantity(
            "7", from_unit=unit_by_code("G"), to_unit=unit_by_code("KG")
        )
        assert result == Decimal("0.007")

    def test_oil_millilitres_to_litres(self, seeded: None) -> None:
        """250 ml of oil is 0.25 L."""
        result = convert_to_stored_quantity(
            "250", from_unit=unit_by_code("ML"), to_unit=unit_by_code("L")
        )
        assert result == Decimal("0.250")

    def test_chicken_dozens_to_pieces(self, seeded: None) -> None:
        """3.5 dozen chickens is 42 pieces."""
        result = convert_to_stored_quantity(
            "3.5", from_unit=unit_by_code("DOZEN"), to_unit=unit_by_code("PIECE")
        )
        assert result == Decimal("42.000")

    def test_half_chicken_is_representable(self, seeded: None) -> None:
        """The charter's half-portion case. COUNT units permit fractions."""
        result = convert_to_stored_quantity(
            "0.5", from_unit=unit_by_code("PIECE"), to_unit=unit_by_code("PIECE")
        )
        assert result == Decimal("0.500")

    def test_sub_gram_quantity_rounds_to_the_stored_precision(self, seeded: None) -> None:
        """
        1 mg in kilograms is 0.000001 — below the 3dp storage precision, so it
        stores as zero. Worth knowing before someone records saffron in kg.
        """
        result = convert_to_stored_quantity(
            "1", from_unit=unit_by_code("MG"), to_unit=unit_by_code("KG")
        )
        assert result == Decimal("0.000")


class TestDimensionSafety:
    def test_mass_cannot_convert_to_volume(self, seeded: None) -> None:
        """A kilogram of rice is not a litre of rice."""
        with pytest.raises(ValidationError) as exc:
            convert("1", from_unit=unit_by_code("KG"), to_unit=unit_by_code("L"))
        assert exc.value.code == "dimension_mismatch"

    @pytest.mark.parametrize(
        ("source", "target"),
        [("KG", "PIECE"), ("L", "KG"), ("PIECE", "ML"), ("DOZEN", "G")],
    )
    def test_all_cross_dimension_pairs_are_refused(
        self, seeded: None, source: str, target: str
    ) -> None:
        with pytest.raises(ValidationError):
            convert("1", from_unit=unit_by_code(source), to_unit=unit_by_code(target))


class TestPrecisionBehaviour:
    def test_convert_does_not_round(self, seeded: None) -> None:
        """
        Rounding inside convert() would be rounding mid-calculation, and a
        caller chaining conversions would compound the error each step.
        """
        result = convert("1", from_unit=unit_by_code("MG"), to_unit=unit_by_code("KG"))
        assert result == Decimal("0.000001")
        assert result != Decimal("0")

    def test_the_boundary_function_rounds_once(self, seeded: None) -> None:
        assert convert_to_stored_quantity(
            "1", from_unit=unit_by_code("MG"), to_unit=unit_by_code("KG")
        ) == Decimal("0.000")

    def test_result_carries_the_stored_precision(self, seeded: None) -> None:
        result = convert_to_stored_quantity(
            "1", from_unit=unit_by_code("KG"), to_unit=unit_by_code("G")
        )
        assert decimal_places_of(result) == 3

    def test_float_input_is_refused(self, seeded: None) -> None:
        with pytest.raises(ValidationError) as exc:
            convert(1.5, from_unit=unit_by_code("KG"), to_unit=unit_by_code("G"))
        assert exc.value.code == "float_in_quantity_path"

    def test_arabic_numerals_convert(self, seeded: None) -> None:
        assert convert_to_stored_quantity(
            "٣٠", from_unit=unit_by_code("KG"), to_unit=unit_by_code("G")
        ) == Decimal("30000.000")

    def test_same_unit_is_an_identity(self, seeded: None) -> None:
        """Not a multiply-then-divide round trip, which could lose precision."""
        kg = unit_by_code("KG")
        assert convert("1.234567", from_unit=kg, to_unit=kg) == Decimal("1.234567")


class TestBaseHelpers:
    def test_to_base_multiplies(self, seeded: None) -> None:
        assert to_base("2", unit=unit_by_code("G")) == Decimal("0.002")

    def test_from_base_divides(self, seeded: None) -> None:
        assert from_base("0.002", unit=unit_by_code("G")) == Decimal("2")

    def test_base_unit_is_its_own_base(self, seeded: None) -> None:
        assert to_base("5", unit=unit_by_code("KG")) == Decimal("5")

    def test_unsaved_unit_with_zero_factor_is_caught(self) -> None:
        """The database forbids it, but an in-memory unit could still carry it."""
        broken = UnitOfMeasure(
            code="BROKEN",
            name="معطل",
            dimension=Dimension.MASS,
            factor_to_base=Decimal("0"),
        )
        with pytest.raises(ValidationError) as exc:
            from_base("1", unit=broken)
        assert exc.value.code == "zero_factor"


#: The `seeded` fixture is not re-run between generated inputs. That is safe
#: here and only here: it creates read-only reference data, and none of these
#: property tests mutate a unit. A property test that changed a factor would
#: have to build its own units instead.
_READ_ONLY_FIXTURE = settings(suppress_health_check=[HealthCheck.function_scoped_fixture])


class TestConversionProperties:
    @_READ_ONLY_FIXTURE
    @given(
        quantity=st.decimals(
            min_value=Decimal("0.001"),
            max_value=Decimal("100000"),
            allow_nan=False,
            allow_infinity=False,
            places=3,
        ),
        pair=st.sampled_from(
            [("KG", "G"), ("G", "KG"), ("L", "ML"), ("ML", "L"), ("DOZEN", "PIECE")]
        ),
    )
    def test_round_trip_is_lossless_within_the_declared_precision(
        self, seeded: None, quantity: Decimal, pair: tuple[str, str]
    ) -> None:
        """
        Convert to another unit and back; the stored value must be unchanged.
        This is the property that stops a transfer losing stock to rounding.
        """
        source, target = unit_by_code(pair[0]), unit_by_code(pair[1])
        there = convert(quantity, from_unit=source, to_unit=target)
        back = convert(there, from_unit=target, to_unit=source)
        assert quantize_quantity(back) == quantize_quantity(quantity)

    @_READ_ONLY_FIXTURE
    @given(
        quantity=st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal("100000"),
            allow_nan=False,
            allow_infinity=False,
            places=3,
        )
    )
    def test_conversion_through_base_matches_a_direct_factor_ratio(
        self, seeded: None, quantity: Decimal
    ) -> None:
        """A->base->B must equal A * (factor_A / factor_B)."""
        gram, kilo = unit_by_code("G"), unit_by_code("KG")
        via_base = convert(quantity, from_unit=gram, to_unit=kilo)
        direct = quantity * (gram.factor_to_base / kilo.factor_to_base)
        assert quantize_quantity(via_base) == quantize_quantity(direct)

    @_READ_ONLY_FIXTURE
    @given(
        quantity=st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal("10000"),
            allow_nan=False,
            allow_infinity=False,
            places=3,
        )
    )
    def test_conversion_preserves_sign_and_zero(self, seeded: None, quantity: Decimal) -> None:
        result = convert(quantity, from_unit=unit_by_code("KG"), to_unit=unit_by_code("G"))
        assert (result == 0) == (quantity == 0)
        assert result >= 0
