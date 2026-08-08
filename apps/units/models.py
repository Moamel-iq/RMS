"""
Units of measure.

Global reference data. Units are organization-level per the architecture
charter section 9 — a kilogram is a kilogram in every branch — so they carry
no organization or branch foreign key.

This app models ONE of the three things the charter separates:

  A. Unit conversion       1 kg = 1000 g              <- this app
  B. Packaging conversion  1 rice sack = 30 kg        <- Phase 1, on the Item
  C. Production yield      10 kg raw meat -> 8.7 kg   <- Phase 3, a result

B is item-specific: a carton of cups and a carton of chicken hold different
quantities, so "carton" cannot be a global unit with one factor. C is not a
conversion at all; it is a production outcome with loss and variance.
Modelling either here would be a rewrite later.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel
from apps.core.quantity import FACTOR_PLACES

#: Codes appear in reports and imports; keep them unambiguous.
UNIT_CODE_PATTERN = r"^[A-Z][A-Z0-9_]*$"

#: Enough integer digits for any plausible factor (a carton of 1000 pieces)
#: alongside the twelve decimals an ounce needs.
FACTOR_MAX_DIGITS = FACTOR_PLACES + 12


class Dimension(models.TextChoices):
    """
    What a unit measures.

    A closed set fixed by physics, not by the business, so it is an enum
    rather than a table: there is no scenario where an operator legitimately
    adds a new dimension, and a table would invite one.
    """

    MASS = "MASS", _("كتلة")
    VOLUME = "VOLUME", _("حجم")
    COUNT = "COUNT", _("عدد")


class UnitOfMeasure(TimeStampedModel):
    """
    A unit, and how many base units of its dimension it represents.

    `factor_to_base` is deliberately "how many base units are in ONE of this
    unit", so converting to base is a multiplication. Multiplication of
    Decimals is exact; division is not. Storing the reciprocal instead would
    make the common direction lossy — 1/3 has no exact decimal form.
    """

    code = models.CharField(_("code"), max_length=20, unique=True)
    name_ar = models.CharField(_("name (Arabic)"), max_length=100)
    name_en = models.CharField(_("name (English)"), max_length=100)

    dimension = models.CharField(_("dimension"), max_length=10, choices=Dimension.choices)

    factor_to_base = models.DecimalField(
        _("factor to base unit"),
        max_digits=FACTOR_MAX_DIGITS,
        decimal_places=FACTOR_PLACES,
        help_text=_("How many base units are in one of this unit. One gram is 0.001 kg."),
    )

    is_base = models.BooleanField(
        _("is base unit"),
        default=False,
        help_text=_("The unit every other unit in this dimension converts through."),
    )

    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("unit of measure")
        verbose_name_plural = _("units of measure")
        ordering = ["dimension", "-is_base", "code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(code__regex=UNIT_CODE_PATTERN),
                name="unit_code_format",
            ),
            models.CheckConstraint(
                condition=~Q(name_ar="") & ~Q(name_en=""),
                name="unit_names_not_empty",
            ),
            # A zero factor makes every conversion zero; a negative one flips
            # the sign of a stock movement. Both are silent corruption.
            models.CheckConstraint(
                condition=Q(factor_to_base__gt=Decimal("0")),
                name="unit_factor_is_positive",
            ),
            # The base unit is the fixed point of its dimension. A base unit
            # with any other factor would make conversion non-idempotent.
            models.CheckConstraint(
                condition=Q(is_base=False) | Q(factor_to_base=Decimal("1")),
                name="unit_base_factor_is_one",
            ),
            # Exactly one base per dimension. Two would make "the" base
            # ambiguous at the moment a conversion needs it.
            models.UniqueConstraint(
                fields=["dimension"],
                condition=Q(is_base=True),
                name="unit_one_base_per_dimension",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"
