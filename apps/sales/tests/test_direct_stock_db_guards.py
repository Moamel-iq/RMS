"""Focused PostgreSQL guards added with direct-stock sales.

These are transaction tests because the cross-table guards are intentionally
deferred until commit.  Ordinary ``django_db`` tests are wrapped in a rollback
transaction and would therefore never execute a deferred constraint trigger.
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from django.db import IntegrityError

from apps.kitchen.models import Recipe
from apps.organizations.models import Organization
from apps.sales.models import (
    MenuItem,
    SalesAdjustment,
    SalesAdjustmentLine,
    SalesDay,
    SalesDayLine,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _clone_concrete(instance: object) -> dict[str, object]:
    """Copy one row without its PK/timestamps and give UUID evidence a new id."""

    values: dict[str, object] = {}
    for field in instance._meta.concrete_fields:  # type: ignore[attr-defined]
        if field.primary_key or field.name in {"created_at", "updated_at"}:
            continue
        values[field.attname] = getattr(instance, field.attname)
    if "public_id" in values:
        values["public_id"] = uuid.uuid4()
    return values


def test_posted_sales_day_cannot_acquire_a_new_line(scenario: dict[str, object]) -> None:
    day = cast(SalesDay, scenario["day"])
    original = day.lines.order_by("sequence").first()
    assert original is not None
    values = _clone_concrete(original)
    values["sequence"] = 99

    with pytest.raises(IntegrityError, match="only be inserted while the day is a draft"):
        SalesDayLine.objects.create(**values)


def test_posted_adjustment_cannot_acquire_a_new_line(
    scenario: dict[str, object],
) -> None:
    adjustment = cast(SalesAdjustment, scenario["adjustment"])
    original = adjustment.lines.order_by("sequence").first()
    assert original is not None
    values = _clone_concrete(original)
    values["sequence"] = 99

    with pytest.raises(
        IntegrityError,
        match="only be inserted while the adjustment is a draft",
    ):
        SalesAdjustmentLine.objects.create(**values)


def test_menu_recipe_cannot_cross_organization(
    scenario_recipe: Recipe,
    other_organization: Organization,
) -> None:
    with pytest.raises(IntegrityError, match="menu recipe must belong"):
        MenuItem.objects.create(
            organization=other_organization,
            code="BAD-CROSS-TENANT-MENU",
            name="صنف غير صالح",
            fulfillment_source="RECIPE_SERVING",
            recipe=scenario_recipe,
            serving_code="WHOLE",
        )
