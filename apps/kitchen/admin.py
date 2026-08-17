"""
Read-only admin for the kitchen models.

A technical inspection surface, never a management one. Recipes are maintained
in the Arabic shell, through services that validate, lock and audit; a writable
admin would be a second write path with none of that, and the first thing it
would produce is a recipe line whose base quantity never went through a
conversion.

`has_add_permission`, `has_change_permission` and `has_delete_permission` all
return `False` for everyone, superusers included, so a hand-made POST to an
admin URL is refused rather than merely unlinked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.http import HttpRequest

from apps.kitchen.models import (
    Recipe,
    RecipeCategory,
    RecipeComponent,
    RecipeCostSnapshot,
    RecipeCostSnapshotLine,
    RecipeCostSnapshotServing,
    RecipeLine,
    RecipeLineSubstitute,
    RecipeServing,
    RecipeStep,
    RecipeStepIngredient,
    RecipeVersion,
    RecipeVersionBranchScope,
    RecipeVersionReview,
)

if TYPE_CHECKING:
    _ModelAdmin = admin.ModelAdmin[Any]
else:
    _ModelAdmin = admin.ModelAdmin


class ReadOnlyAdmin(_ModelAdmin):
    """Look, do not touch."""

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(RecipeCategory)
class RecipeCategoryAdmin(ReadOnlyAdmin):
    list_display = ("code", "name_ar", "organization", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("code", "name_ar", "name_en")


@admin.register(Recipe)
class RecipeAdmin(ReadOnlyAdmin):
    list_display = ("code", "name_ar", "recipe_type", "organization", "is_active")
    list_filter = ("recipe_type", "is_active", "organization")
    search_fields = ("code", "name_ar", "name_en")
    readonly_fields = ("public_id",)


@admin.register(RecipeVersion)
class RecipeVersionAdmin(ReadOnlyAdmin):
    list_display = (
        "recipe",
        "version_number",
        "status",
        "effective_from",
        "effective_to",
        "approved_by",
    )
    list_filter = ("status",)
    search_fields = ("recipe__code", "recipe__name_ar", "approval_reference")
    readonly_fields = ("public_id",)


@admin.register(RecipeVersionReview)
class RecipeVersionReviewAdmin(ReadOnlyAdmin):
    """
    The signature page, as rows.

    Read-only like everything else here, and doubly so: these rows are
    append-only at the database, so a writable admin would offer an action the
    trigger would refuse anyway.
    """

    list_display = ("version", "review_type", "reviewer", "decision", "reviewed_at")
    list_filter = ("review_type", "decision")
    search_fields = ("version__recipe__code", "evidence_reference")
    readonly_fields = ("public_id",)


@admin.register(RecipeVersionBranchScope)
class RecipeVersionBranchScopeAdmin(ReadOnlyAdmin):
    """Which branch, over which dates — the rows the resolver actually reads."""

    list_display = (
        "recipe",
        "branch",
        "version",
        "effective_from",
        "effective_to",
        "is_organization_wide",
    )
    list_filter = ("is_organization_wide",)
    search_fields = ("recipe__code", "branch__code")
    readonly_fields = ("public_id",)


@admin.register(RecipeLine)
class RecipeLineAdmin(ReadOnlyAdmin):
    list_display = ("version", "line_order", "item", "base_quantity", "cost_class")
    list_filter = ("cost_class", "measurement_basis")
    search_fields = ("item__code", "version__recipe__code")


@admin.register(RecipeLineSubstitute)
class RecipeLineSubstituteAdmin(ReadOnlyAdmin):
    list_display = ("line", "substitute_item", "priority", "is_active")
    search_fields = ("substitute_item__code",)


@admin.register(RecipeStep)
class RecipeStepAdmin(ReadOnlyAdmin):
    list_display = ("version", "sequence", "stage", "expected_duration", "temperature_c")
    list_filter = ("stage", "is_critical")
    search_fields = ("version__recipe__code",)


@admin.register(RecipeStepIngredient)
class RecipeStepIngredientAdmin(ReadOnlyAdmin):
    list_display = ("step", "recipe_line", "share")


@admin.register(RecipeServing)
class RecipeServingAdmin(ReadOnlyAdmin):
    list_display = (
        "version",
        "code",
        "name_ar",
        "serving_quantity",
        "factor_of_batch",
        "is_primary",
    )
    list_filter = ("is_primary", "is_active")
    search_fields = ("code", "name_ar", "version__recipe__code")


@admin.register(RecipeComponent)
class RecipeComponentAdmin(ReadOnlyAdmin):
    """The nested-recipe edges. Registered here rather than left invisible."""

    list_display = ("version", "line_order", "component_recipe", "component_version", "multiplier")
    search_fields = ("recipe__code", "component_recipe__code")


# ---------------------------------------------------------------------------
# Task 3.3 - cost snapshots
# ---------------------------------------------------------------------------
#
# Read-only like everything else here, and for one extra reason: these rows are
# append-only at the database. A writable admin would offer a button whose POST
# a trigger refuses, which is a worse experience than no button - and if the
# trigger were ever dropped, that button would be the one way to edit a costing
# record nobody may edit.


@admin.register(RecipeCostSnapshot)
class RecipeCostSnapshotAdmin(ReadOnlyAdmin):
    list_display = (
        "recipe_code",
        "version_number",
        "warehouse_code",
        "as_of_date",
        "total_material_cost",
        "ledger_cutoff_sequence",
        "calculation_version",
        "created_at",
    )
    list_filter = ("organization", "valuation_mode", "calculation_version", "as_of_date")
    search_fields = ("recipe_code", "warehouse_code", "idempotency_key", "reference")
    date_hierarchy = "as_of_date"


@admin.register(RecipeCostSnapshotLine)
class RecipeCostSnapshotLineAdmin(ReadOnlyAdmin):
    list_display = (
        "snapshot",
        "line_number",
        "component_path",
        "item_code",
        "cost_class",
        "effective_quantity",
        "unit_cost",
        "allocated_extension",
    )
    list_filter = ("source_kind", "cost_class")
    search_fields = ("item_code", "source_recipe_code")


@admin.register(RecipeCostSnapshotServing)
class RecipeCostSnapshotServingAdmin(ReadOnlyAdmin):
    list_display = (
        "snapshot",
        "code",
        "factor_of_batch",
        "cost_per_serving",
        "whole_serving_count",
        "allocated_total",
        "allocation_state",
    )
    list_filter = ("allocation_state", "is_primary")
    search_fields = ("code", "name_ar")
