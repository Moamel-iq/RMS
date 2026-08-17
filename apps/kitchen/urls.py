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

from apps.kitchen import cost_views, production_views, version_views, views

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
    # Production drafting (Task 3.4). Warehouse-scoped, draft-only, money-free.
    #
    # Read this list for what is **absent**: there is no post route, no reverse
    # route, no issue, consume, complete or journal route, and no lot or location
    # picker. None of them is hidden — none of them exists, and a URL implying
    # otherwise would be the router contradicting a check constraint.
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
]
