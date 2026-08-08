"""Unit-of-measure invariants, and what the database refuses on its own."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction

from apps.units.models import Dimension, UnitOfMeasure
from apps.units.selectors import base_unit_for, convertible_units, unit_by_code, units_in_dimension

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded() -> None:
    call_command("seed_units", verbosity=0)


class TestSeed:
    def test_seed_creates_the_standard_units(self, seeded: None) -> None:
        assert UnitOfMeasure.objects.count() == 8

    def test_seed_is_idempotent(self, seeded: None) -> None:
        """Reference data must be reproducible; running twice changes nothing."""
        call_command("seed_units", verbosity=0)
        assert UnitOfMeasure.objects.count() == 8

    def test_every_dimension_has_exactly_one_base_unit(self, seeded: None) -> None:
        for dimension in Dimension:
            bases = UnitOfMeasure.objects.filter(dimension=dimension, is_base=True)
            assert bases.count() == 1, dimension

    def test_base_units_are_the_expected_ones(self, seeded: None) -> None:
        assert base_unit_for(Dimension.MASS).code == "KG"
        assert base_unit_for(Dimension.VOLUME).code == "L"
        assert base_unit_for(Dimension.COUNT).code == "PIECE"

    def test_factors_are_exact(self, seeded: None) -> None:
        assert unit_by_code("G").factor_to_base == Decimal("0.001")
        assert unit_by_code("MG").factor_to_base == Decimal("0.000001")
        assert unit_by_code("DOZEN").factor_to_base == Decimal("12")

    def test_packaging_units_are_deliberately_absent(self, seeded: None) -> None:
        """
        A carton of cups and a carton of chicken hold different quantities, so
        "carton" cannot be a global unit with one factor. It is item-specific
        packaging and belongs to Phase 1.
        """
        codes = set(UnitOfMeasure.objects.values_list("code", flat=True))
        assert not codes & {"CARTON", "SACK", "BOX", "TRAY", "BAG"}


class TestDatabaseConstraints:
    """Python validation is bypassed by bulk operations and raw SQL."""

    def test_zero_factor_is_refused(self) -> None:
        """A zero factor makes every conversion through it zero."""
        with pytest.raises(IntegrityError), transaction.atomic():
            UnitOfMeasure.objects.create(
                code="ZERO",
                name_ar="صفر",
                name_en="Zero",
                dimension=Dimension.MASS,
                factor_to_base=Decimal("0"),
            )

    def test_negative_factor_is_refused(self) -> None:
        """A negative factor would flip the sign of a stock movement."""
        with pytest.raises(IntegrityError), transaction.atomic():
            UnitOfMeasure.objects.create(
                code="NEG",
                name_ar="سالب",
                name_en="Negative",
                dimension=Dimension.MASS,
                factor_to_base=Decimal("-1"),
            )

    def test_a_base_unit_must_have_a_factor_of_one(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            UnitOfMeasure.objects.create(
                code="ODDBASE",
                name_ar="أساس",
                name_en="Odd base",
                dimension=Dimension.MASS,
                factor_to_base=Decimal("2"),
                is_base=True,
            )

    def test_a_dimension_cannot_have_two_base_units(self, seeded: None) -> None:
        """Two bases make "the" base ambiguous exactly when a conversion needs it."""
        with pytest.raises(IntegrityError), transaction.atomic():
            UnitOfMeasure.objects.create(
                code="KG2",
                name_ar="كيلو ثان",
                name_en="Second kilo",
                dimension=Dimension.MASS,
                factor_to_base=Decimal("1"),
                is_base=True,
            )

    def test_codes_are_globally_unique(self, seeded: None) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            UnitOfMeasure.objects.create(
                code="KG",
                name_ar="مكرر",
                name_en="Duplicate",
                dimension=Dimension.MASS,
                factor_to_base=Decimal("1"),
            )

    def test_database_refuses_a_malformed_code(self, seeded: None) -> None:
        unit = unit_by_code("G")
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE units_unitofmeasure SET code = %s WHERE id = %s",
                    ["bad code", unit.id],
                )

    def test_database_refuses_a_zero_factor_written_directly(self, seeded: None) -> None:
        unit = unit_by_code("G")
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE units_unitofmeasure SET factor_to_base = 0 WHERE id = %s",
                    [unit.id],
                )


class TestSelectors:
    def test_units_in_dimension(self, seeded: None) -> None:
        mass_codes = set(units_in_dimension(Dimension.MASS).values_list("code", flat=True))
        assert mass_codes == {"KG", "G", "MG", "TON"}

    def test_convertible_units_excludes_itself_and_other_dimensions(self, seeded: None) -> None:
        kg = unit_by_code("KG")
        codes = set(convertible_units(kg).values_list("code", flat=True))
        assert codes == {"G", "MG", "TON"}

    def test_unknown_code_raises_clearly(self, seeded: None) -> None:
        with pytest.raises(ValidationError) as exc:
            unit_by_code("NOPE")
        assert exc.value.code == "unknown_unit"

    def test_lookup_is_case_insensitive(self, seeded: None) -> None:
        assert unit_by_code("kg").code == "KG"

    def test_missing_base_unit_raises_rather_than_returning_none(self) -> None:
        """
        A dimension with no base is a broken install; every conversion in it is
        wrong. Failing loudly beats returning a number nobody can trust.
        """
        with pytest.raises(ValidationError) as exc:
            base_unit_for(Dimension.MASS)
        assert exc.value.code == "missing_base_unit"

    def test_inactive_units_are_excluded(self, seeded: None) -> None:
        ton = unit_by_code("TON")
        ton.is_active = False
        ton.save(update_fields=["is_active"])
        assert "TON" not in set(units_in_dimension(Dimension.MASS).values_list("code", flat=True))
