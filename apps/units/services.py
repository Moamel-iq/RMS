"""
Unit conversion.

The algorithm, in plain language:

    To convert Q from unit A to unit B, both must measure the same dimension.
    Multiply Q by A's factor to reach the dimension's base unit, then divide
    by B's factor to reach B.

        Q_base = Q * A.factor_to_base
        Q_B    = Q_base / B.factor_to_base

Two rules govern precision, and they are the whole reason this module exists
rather than the arithmetic being written inline:

  1. `convert` returns FULL precision and does not round. Rounding here would
     be rounding mid-calculation, and a caller converting through several
     units would compound the error at every step.
  2. `convert_to_stored_quantity` is the boundary function. It rounds exactly
     once, to the stored quantity precision. Anything persisting or
     displaying a converted quantity calls this one.

Deliberately NOT handled in this task:
  - Item-specific packaging (one sack of THIS rice = 30 kg). Phase 1.
  - Production yield. Phase 3; it is not a conversion.
  - Effective dating. A kilogram has never been a different number of grams.
    Item packaging DOES change over time and will carry effective dates when
    it arrives in Phase 1.
  - Minimum issue increments and per-item fraction rules. Phase 1, because
    they are properties of an item, not of a unit: half a chicken is
    meaningful, half a cup is not, and both are COUNT.
"""

from __future__ import annotations

from decimal import Decimal, DivisionByZero, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.quantity import ensure_decimal, quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.units.models import Dimension, UnitOfMeasure


def _require_same_dimension(from_unit: UnitOfMeasure, to_unit: UnitOfMeasure) -> None:
    if from_unit.dimension != to_unit.dimension:
        raise ValidationError(
            _(
                "Cannot convert %(from_code)s (%(from_dim)s) to %(to_code)s (%(to_dim)s): "
                "different dimensions."
            ),
            code="dimension_mismatch",
            params={
                "from_code": from_unit.code,
                "from_dim": from_unit.dimension,
                "to_code": to_unit.code,
                "to_dim": to_unit.dimension,
            },
        )


def to_base(quantity: object, *, unit: UnitOfMeasure) -> Decimal:
    """Express `quantity` of `unit` in that dimension's base unit. Exact."""
    value = ensure_decimal(quantity, field="quantity")
    return value * unit.factor_to_base


def from_base(base_quantity: object, *, unit: UnitOfMeasure) -> Decimal:
    """Express a base-unit quantity in `unit`."""
    value = ensure_decimal(base_quantity, field="quantity")
    try:
        return value / unit.factor_to_base
    except (DivisionByZero, InvalidOperation) as exc:
        # The database forbids a zero factor, but a unit built in memory and
        # never saved could still carry one.
        raise ValidationError(
            _("Unit %(code)s has a zero conversion factor."),
            code="zero_factor",
            params={"code": unit.code},
        ) from exc


def convert(quantity: object, *, from_unit: UnitOfMeasure, to_unit: UnitOfMeasure) -> Decimal:
    """
    Convert between two units of the same dimension, at full precision.

    Does NOT round. Use `convert_to_stored_quantity` at the point where the
    result is persisted or displayed.
    """
    _require_same_dimension(from_unit, to_unit)
    value = ensure_decimal(quantity, field="quantity")

    # Same unit is an identity, not a round trip. Multiplying then dividing by
    # the same factor is exact for terminating factors but needless, and it
    # would drag a repeating decimal into an otherwise exact answer.
    if from_unit.pk is not None and from_unit.pk == to_unit.pk:
        return value

    return from_base(to_base(value, unit=from_unit), unit=to_unit)


def convert_to_stored_quantity(
    quantity: object, *, from_unit: UnitOfMeasure, to_unit: UnitOfMeasure
) -> Decimal:
    """
    Convert, then round once to the stored quantity precision.

    This is the boundary. Everything that writes a converted quantity to the
    database or shows it to a user goes through here, so the rounding happens
    in exactly one place and in exactly one direction.
    """
    return quantize_quantity(convert(quantity, from_unit=from_unit, to_unit=to_unit))


# ---------------------------------------------------------------------------
# Maintaining the unit list
# ---------------------------------------------------------------------------


@transaction.atomic
def create_unit(
    *,
    code: str,
    name_ar: str,
    name_en: str,
    dimension: Dimension | str,
    factor_to_base: Decimal,
    is_base: bool = False,
) -> UnitOfMeasure:
    unit = UnitOfMeasure(
        code=code.strip().upper(),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        dimension=dimension,
        factor_to_base=factor_to_base,
        is_base=is_base,
    )
    unit.full_clean()
    unit.save()
    record_audit_event(action=AuditAction.CREATED, target=unit, new_state=snapshot(unit))
    return unit


@transaction.atomic
def update_unit(
    *,
    unit: UnitOfMeasure,
    name_ar: str,
    name_en: str,
    factor_to_base: Decimal,
    is_active: bool,
) -> UnitOfMeasure:
    """
    Update a unit.

    Code, dimension, and base status are not editable. Changing the factor
    already restates every quantity ever converted through this unit, which is
    why before and after are both captured; changing the dimension or which
    unit is the base would invalidate conversions that have no way to know
    they were computed under different rules.
    """
    # Re-read from the database: a ModelForm mutates its instance in place
    # during validation, so an in-memory snapshot would already be the new
    # values and the trail would show no change at all.
    before = snapshot(UnitOfMeasure.objects.get(pk=unit.pk))
    unit.name_ar = name_ar.strip()
    unit.name_en = name_en.strip()
    unit.factor_to_base = factor_to_base
    unit.is_active = is_active
    unit.full_clean()
    unit.save()

    factor_changed = before["factor_to_base"] != str(unit.factor_to_base)
    record_audit_event(
        action=AuditAction.UPDATED if is_active else AuditAction.DEACTIVATED,
        target=unit,
        previous_state=before,
        new_state=snapshot(unit),
        reason="conversion factor changed" if factor_changed else "",
    )
    return unit
