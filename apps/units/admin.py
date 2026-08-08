"""Admin registration for units of measure."""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.units.models import UnitOfMeasure


@admin.register(UnitOfMeasure)
class UnitOfMeasureAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name_ar",
        "name_en",
        "dimension",
        "factor_to_base",
        "is_base",
        "is_active",
    )
    list_filter = ("dimension", "is_base", "is_active")
    search_fields = ("code", "name_ar", "name_en")
    ordering = ("dimension", "-is_base", "code")
    fieldsets = (
        (None, {"fields": ("code", "name_ar", "name_en", "is_active")}),
        (
            _("Conversion"),
            {
                "fields": ("dimension", "is_base", "factor_to_base"),
                "description": _(
                    "The factor is how many base units are in one of this unit. Changing it "
                    "after stock exists would restate every quantity ever converted through "
                    "it. Treat as immutable once transactions begin."
                ),
            },
        ),
    )
