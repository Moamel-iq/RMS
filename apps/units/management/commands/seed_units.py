"""
Seed the standard units of measure.

Deterministic reference data, not factories: reports must be reproducible, so
the same command run twice gives the same rows. Idempotent — safe to re-run.

Only units whose factor is a matter of definition appear here. A dozen is
always twelve, a kilogram is always a thousand grams. "Carton" and "sack" are
absent by design: one carton of cups and one carton of chicken hold different
quantities, so they are item-specific packaging and belong to the Item model
in Phase 1.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.units.models import Dimension, UnitOfMeasure

#: code, ar, en, dimension, factor to base, is base
STANDARD_UNITS: list[tuple[str, str, str, str, str, bool]] = [
    # Mass — base kilogram
    ("KG", "كيلوغرام", "Kilogram", Dimension.MASS, "1", True),
    ("G", "غرام", "Gram", Dimension.MASS, "0.001", False),
    ("MG", "مليغرام", "Milligram", Dimension.MASS, "0.000001", False),
    ("TON", "طن", "Tonne", Dimension.MASS, "1000", False),
    # Volume — base litre
    ("L", "لتر", "Litre", Dimension.VOLUME, "1", True),
    ("ML", "مليلتر", "Millilitre", Dimension.VOLUME, "0.001", False),
    # Count — base piece
    ("PIECE", "قطعة", "Piece", Dimension.COUNT, "1", True),
    ("DOZEN", "دزينة", "Dozen", Dimension.COUNT, "12", False),
]


class Command(BaseCommand):
    help = "Create or update the standard units of measure. Idempotent."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        created_count = 0
        updated_count = 0

        for code, name_ar, name_en, dimension, factor, is_base in STANDARD_UNITS:
            unit, created = UnitOfMeasure.objects.update_or_create(
                code=code,
                defaults={
                    "name_ar": name_ar,
                    "name_en": name_en,
                    "dimension": dimension,
                    "factor_to_base": Decimal(factor),
                    "is_base": is_base,
                    "is_active": True,
                },
            )
            if created:
                created_count += 1
                self.stdout.write(f"  + {unit.code:<6} {name_ar}")
            else:
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Units seeded: {created_count} created, {updated_count} already present."
            )
        )
