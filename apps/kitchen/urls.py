"""
Kitchen routes.

Every path that changes something resolves its target through a scoped selector
before it acts, and checks the same permission the API checks. A route that is
not rendered as a button is still a route, and it is still refused.

Task 3.2B added the component routes, all of them draft-only for mutation and
none of them exposing a cost column. **Task 3.3 adds the costing routes** at the
end, and they are the only paths in this module that carry money. Each one
requires `view_recipe_cost` in addition to reaching the organization, and the
recipe, version, line, step, serving and component routes above stay money-free.

The snapshot routes are a **command and two reads**. There is no edit route and
no delete route, because the rows refuse both verbs at the database and a URL
that implied otherwise would be the router contradicting a trigger.
"""

from django.urls import path

from apps.kitchen import (
    consumption_views,
    cost_views,
    meal_views,
    production_views,
    report_views,
    version_views,
    views,
)

app_name = "kitchen"

urlpatterns = [
    # Recipes
    path("recipes/", views.RecipeListView.as_view(), name="recipe_list"),
    path("recipes/new/", views.RecipeCreateView.as_view(), name="recipe_create"),
    path("recipes/<int:pk>/", views.RecipeDetailView.as_view(), name="recipe_detail"),
    path("recipes/<int:pk>/edit/", views.RecipeUpdateView.as_view(), name="recipe_update"),
    path("recipes/<int:pk>/archive/", views.RecipeArchiveView.as_view(), name="recipe_archive"),
    path(
        "recipes/<int:pk>/reactivate/",
        views.RecipeReactivateView.as_view(),
        name="recipe_reactivate",
    ),
    # Categories
    path("categories/", views.RecipeCategoryListView.as_view(), name="category_list"),
    path("categories/new/", views.RecipeCategoryCreateView.as_view(), name="category_create"),
    path(
        "categories/<int:pk>/edit/",
        views.RecipeCategoryUpdateView.as_view(),
        name="category_update",
    ),
    # Draft versions
    path(
        "recipes/<int:pk>/versions/new/",
        views.DraftVersionCreateView.as_view(),
        name="version_create",
    ),
    path("versions/<int:pk>/edit/", views.DraftVersionUpdateView.as_view(), name="version_update"),
    path(
        "versions/<int:pk>/discard/",
        views.DraftVersionDiscardView.as_view(),
        name="version_discard",
    ),
    # Lines
    path("versions/<int:pk>/lines/new/", views.LineCreateView.as_view(), name="line_create"),
    path("lines/<int:pk>/edit/", views.LineUpdateView.as_view(), name="line_update"),
    path("lines/<int:pk>/delete/", views.LineDeleteView.as_view(), name="line_delete"),
    # Substitutes
    path(
        "lines/<int:pk>/substitutes/new/",
        views.SubstituteCreateView.as_view(),
        name="substitute_create",
    ),
    path(
        "substitutes/<int:pk>/delete/",
        views.SubstituteDeleteView.as_view(),
        name="substitute_delete",
    ),
    # Steps
    path("versions/<int:pk>/steps/new/", views.StepCreateView.as_view(), name="step_create"),
    path("steps/<int:pk>/edit/", views.StepUpdateView.as_view(), name="step_update"),
    path("steps/<int:pk>/delete/", views.StepDeleteView.as_view(), name="step_delete"),
    path("steps/<int:pk>/link/", views.StepLinkView.as_view(), name="step_link"),
    path("step-links/<int:pk>/unlink/", views.StepUnlinkView.as_view(), name="step_unlink"),
    # Servings
    path(
        "versions/<int:pk>/servings/new/",
        views.ServingCreateView.as_view(),
        name="serving_create",
    ),
    path("servings/<int:pk>/edit/", views.ServingUpdateView.as_view(), name="serving_update"),
    path("servings/<int:pk>/delete/", views.ServingDeleteView.as_view(), name="serving_delete"),
    # The version lifecycle
    path("versions/", version_views.VersionListView.as_view(), name="version_list"),
    path("versions/<int:pk>/", version_views.VersionDetailView.as_view(), name="version_detail"),
    path(
        "versions/<int:pk>/timeline/",
        version_views.VersionTimelineView.as_view(),
        name="version_timeline",
    ),
    path(
        "versions/<int:pk>/compare/",
        version_views.VersionCompareView.as_view(),
        name="version_compare",
    ),
    path(
        "versions/<int:pk>/submit/",
        version_views.VersionSubmitView.as_view(),
        name="version_submit",
    ),
    path(
        "versions/<int:pk>/review/",
        version_views.VersionReviewView.as_view(),
        name="version_review",
    ),
    path(
        "versions/<int:pk>/approve/",
        version_views.VersionApproveView.as_view(),
        name="version_approve",
    ),
    path(
        "versions/<int:pk>/reject/",
        version_views.VersionRejectView.as_view(),
        name="version_reject",
    ),
    path(
        "versions/<int:pk>/activate/",
        version_views.VersionActivateView.as_view(),
        name="version_activate",
    ),
    path(
        "versions/<int:pk>/supersede/",
        version_views.VersionSupersedeView.as_view(),
        name="version_supersede",
    ),
    path(
        "recipes/<int:pk>/versions/",
        version_views.RecipeVersionHistoryView.as_view(),
        name="recipe_versions",
    ),
    path(
        "recipes/<int:pk>/effective/",
        version_views.ResolverPreviewView.as_view(),
        name="version_resolve",
    ),
    # Nested components
    path(
        "versions/<int:pk>/components/",
        version_views.ComponentEditorView.as_view(),
        name="component_editor",
    ),
    path(
        "versions/<int:pk>/components/new/",
        version_views.ComponentCreateView.as_view(),
        name="component_create",
    ),
    path(
        "versions/<int:pk>/component-tree/",
        version_views.ComponentTreeView.as_view(),
        name="component_tree",
    ),
    path(
        "versions/<int:pk>/used-by/",
        version_views.ComponentDependencyView.as_view(),
        name="component_dependencies",
    ),
    path(
        "components/<int:pk>/edit/",
        version_views.ComponentUpdateView.as_view(),
        name="component_update",
    ),
    path(
        "components/<int:pk>/reorder/",
        version_views.ComponentReorderView.as_view(),
        name="component_reorder",
    ),
    path(
        "components/<int:pk>/delete/",
        version_views.ComponentDeleteView.as_view(),
        name="component_delete",
    ),
    # Costing (Task 3.3). Money, and therefore `view_recipe_cost`.
    path(
        "versions/<int:pk>/cost/",
        cost_views.RecipeCostCardView.as_view(),
        name="cost_card",
    ),
    path(
        "recipes/<int:pk>/cost-on-date/",
        cost_views.RecipeHistoricalCostView.as_view(),
        name="cost_on_date",
    ),
    path(
        "versions/<int:pk>/cost-snapshots/new/",
        cost_views.CostSnapshotCreateView.as_view(),
        name="cost_snapshot_create",
    ),
    path(
        "cost-snapshots/",
        cost_views.CostSnapshotListView.as_view(),
        name="cost_snapshot_list",
    ),
    path(
        "cost-snapshots/<int:pk>/",
        cost_views.CostSnapshotDetailView.as_view(),
        name="cost_snapshot_detail",
    ),
    # Production (Tasks 3.4 and 3.5). Warehouse-scoped throughout, and three
    # different grants: reading, drafting, posting. Task 3.4 shipped the
    # drafting half of this list and said what was absent; Task 3.5 adds the
    # allocation, posting, reversal and movement routes.
    #
    # Read the list for what is **still** absent: no meal log, no theoretical or
    # actual consumption read, no usage variance. Those are Tasks 3.6 to 3.9,
    # and a URL implying otherwise would be the router promising a screen that
    # does not exist.
    path(
        "production/",
        production_views.ProductionBatchListView.as_view(),
        name="production_list",
    ),
    path(
        "production/new/",
        production_views.ProductionBatchCreateView.as_view(),
        name="production_create",
    ),
    path(
        "production/preview/",
        production_views.ProductionPreviewView.as_view(),
        name="production_preview",
    ),
    path(
        "production/<int:pk>/",
        production_views.ProductionBatchDetailView.as_view(),
        name="production_detail",
    ),
    # The three panels the workspace swaps after an edit. Each renders the same
    # partial the detail page includes, so a fragment and the page it came from
    # cannot drift.
    path(
        "production/<int:pk>/requirements/",
        production_views.ProductionRequirementsView.as_view(),
        name="production_requirements",
    ),
    path(
        "production/<int:pk>/readiness/",
        production_views.ProductionReadinessView.as_view(),
        name="production_readiness",
    ),
    path(
        "production/<int:pk>/timeline/",
        production_views.ProductionTimelineView.as_view(),
        name="production_timeline",
    ),
    path(
        "production/<int:pk>/rescale/",
        production_views.ProductionRescaleView.as_view(),
        name="production_rescale",
    ),
    path(
        "production/<int:pk>/output/",
        production_views.ProductionOutputView.as_view(),
        name="production_output",
    ),
    path(
        "production/<int:pk>/notes/",
        production_views.ProductionNotesView.as_view(),
        name="production_notes",
    ),
    path(
        "production/<int:pk>/discard/",
        production_views.ProductionDiscardView.as_view(),
        name="production_discard",
    ),
    path(
        "production-lines/<int:pk>/substitutes/new/",
        production_views.ProductionSubstituteCreateView.as_view(),
        name="production_substitute_create",
    ),
    path(
        "production-actuals/<int:pk>/edit/",
        production_views.ProductionActualUpdateView.as_view(),
        name="production_actual_update",
    ),
    path(
        "production-actuals/<int:pk>/delete/",
        production_views.ProductionActualDeleteView.as_view(),
        name="production_actual_delete",
    ),
    # --- Task 3.5 ---------------------------------------------------------
    path(
        "production-actuals/<int:pk>/allocate/",
        production_views.ProductionAllocateView.as_view(),
        name="production_allocate",
    ),
    path(
        "production/<int:pk>/movements/",
        production_views.ProductionMovementsView.as_view(),
        name="production_movements",
    ),
    path(
        "production/<int:pk>/post/",
        production_views.ProductionPostView.as_view(),
        name="production_post",
    ),
    path(
        "production/<int:pk>/reverse/",
        production_views.ProductionReverseView.as_view(),
        name="production_reverse",
    ),
    # --- Task 3.6: reads over what production and Inventory already did ----
    #
    # Six screens, one report shell, one export path. The two custody routes
    # are named for custody rather than for consumption on purpose: goods
    # moving into or out of the kitchen store have changed hands and have not
    # been used, and the naming is the first place that distinction is either
    # kept or quietly lost.
    path(
        "reports/production/",
        report_views.ProductionRegisterView.as_view(),
        name="report_production_register",
    ),
    path(
        "reports/productivity/",
        report_views.ProductivityReportView.as_view(),
        name="report_productivity",
    ),
    path(
        "reports/recipe-cost/",
        report_views.RecipeCostReportView.as_view(),
        name="report_recipe_cost",
    ),
    path(
        "reports/recipe-cost/<int:pk>/",
        report_views.RecipeCostReportDetailView.as_view(),
        name="report_recipe_cost_detail",
    ),
    path(
        "reports/batch-variance/<int:pk>/",
        report_views.BatchVarianceView.as_view(),
        name="report_batch_variance",
    ),
    path(
        "reports/kitchen-issue/",
        report_views.KitchenIssueReportView.as_view(),
        name="report_kitchen_issue",
    ),
    path(
        "reports/kitchen-return/",
        report_views.KitchenReturnReportView.as_view(),
        name="report_kitchen_return",
    ),
    path(
        "reports/kitchen-waste/",
        report_views.KitchenWasteReportView.as_view(),
        name="report_kitchen_waste",
    ),
    # --- Task 3.7: staff and complimentary meals --------------------------
    #
    # One set of views for both meal types, parameterised in the route rather
    # than duplicated: they are the same document with a different reason on
    # it, and two copies would drift the first time one gained a column.
    #
    # Nothing behind these routes posts stock or writes a journal. The
    # ingredients already left through production or an issue, and recording
    # the meal a second time as a stock movement would take the same kilogram
    # out twice.
    path(
        "meals/staff/",
        meal_views.MealListView.as_view(meal_type="STAFF"),
        name="meal_staff_list",
    ),
    path(
        "meals/complimentary/",
        meal_views.MealListView.as_view(meal_type="COMPLIMENTARY"),
        name="meal_complimentary_list",
    ),
    path(
        "meals/new/staff/",
        meal_views.MealCreateView.as_view(meal_type="STAFF"),
        name="meal_create_staff",
    ),
    path(
        "meals/new/complimentary/",
        meal_views.MealCreateView.as_view(meal_type="COMPLIMENTARY"),
        name="meal_create_complimentary",
    ),
    path("meals/<int:pk>/", meal_views.MealDetailView.as_view(), name="meal_detail"),
    path("meals/<int:pk>/cancel/", meal_views.MealCancelView.as_view(), name="meal_cancel"),
    # --- Task 3.8: consumption, the movement partition, and attribution ----
    #
    # Read the names for what this task does and does not claim. There is a
    # `theoretical` route and a `variance` route, and neither one promises a
    # sales-based figure: both carry `SALES_NOT_INCLUDED_PHASE_4` on every
    # response, and the variance screen labels its residual `PARTIAL_COVERAGE`
    # and `NOT_FINAL_USAGE_VARIANCE`. A route called `usage-variance` that
    # returned a definitive number would be the router making a promise the
    # data cannot keep.
    #
    # The two link routes are `POST` targets that create one explanatory row
    # each and touch no ledger. There is deliberately no delete route: an
    # attribution is withdrawn by cancelling it with a reason, and the row
    # stays.
    path(
        "reports/warehouse-flow/",
        consumption_views.WarehouseFlowView.as_view(),
        name="report_warehouse_flow",
    ),
    path(
        "reports/actual-consumption/",
        consumption_views.ActualConsumptionView.as_view(),
        name="report_actual_consumption",
    ),
    path(
        "reports/production-standard/",
        consumption_views.StandardRequirementsView.as_view(),
        name="report_production_standard",
    ),
    path(
        "reports/theoretical-consumption/",
        consumption_views.TheoreticalConsumptionView.as_view(),
        name="report_theoretical_consumption",
    ),
    path(
        "reports/usage-variance/",
        consumption_views.UsageVarianceView.as_view(),
        name="report_usage_variance",
    ),
    path(
        "reports/meal-equivalents/staff/",
        consumption_views.MealEquivalentUsageView.as_view(meal_type="STAFF"),
        name="report_meal_equivalent_staff",
    ),
    path(
        "reports/meal-equivalents/complimentary/",
        consumption_views.MealEquivalentUsageView.as_view(meal_type="COMPLIMENTARY"),
        name="report_meal_equivalent_complimentary",
    ),
    path(
        "reports/batch-consumption/<int:pk>/",
        consumption_views.BatchConsumptionView.as_view(),
        name="report_batch_consumption",
    ),
    path(
        "production/<int:pk>/links/waste/",
        consumption_views.BatchDocumentLinkCreateView.as_view(link_type="ABNORMAL_WASTE_CONTEXT"),
        name="batch_link_waste",
    ),
    path(
        "production/<int:pk>/links/custody/",
        consumption_views.BatchDocumentLinkCreateView.as_view(link_type="CUSTODY_RETURN_CONTEXT"),
        name="batch_link_custody",
    ),
    path(
        "batch-links/<int:pk>/cancel/",
        consumption_views.BatchDocumentLinkCancelView.as_view(),
        name="batch_link_cancel",
    ),
]
