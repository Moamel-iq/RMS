"""Unit-of-measure queries. Reads only."""

from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.units.models import Dimension, UnitOfMeasure


def active_units() -> QuerySet[UnitOfMeasure]:
    return UnitOfMeasure.objects.filter(is_active=True)


def units_in_dimension(dimension: Dimension | str) -> QuerySet[UnitOfMeasure]:
    return active_units().filter(dimension=dimension)


def base_unit_for(dimension: Dimension | str) -> UnitOfMeasure:
    """
    The base unit of a dimension.

    Raises rather than returning None: a dimension without a base unit is a
    broken installation, and every conversion in that dimension is wrong until
    it is fixed. Failing loudly beats returning a quantity nobody can trust.
    """
    try:
        return UnitOfMeasure.objects.get(dimension=dimension, is_base=True)
    except ObjectDoesNotExist as exc:
        raise ValidationError(
            _("Dimension %(dimension)s has no base unit."),
            code="missing_base_unit",
            params={"dimension": dimension},
        ) from exc


def unit_by_code(code: str) -> UnitOfMeasure:
    """Look up a unit by its code, raising a clear error when it is unknown."""
    try:
        return UnitOfMeasure.objects.get(code=code.strip().upper())
    except ObjectDoesNotExist as exc:
        raise ValidationError(
            _("Unknown unit of measure: %(code)s"),
            code="unknown_unit",
            params={"code": code},
        ) from exc


def convertible_units(unit: UnitOfMeasure) -> QuerySet[UnitOfMeasure]:
    """Units `unit` can be converted to — same dimension, active, excluding itself."""
    return units_in_dimension(unit.dimension).exclude(pk=unit.pk)
