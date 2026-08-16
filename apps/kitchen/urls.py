"""
Kitchen routes.

Every path that changes something resolves its target through a scoped selector
before it acts, and checks the same permission the API checks. A route that is
not rendered as a button is still a route, and it is still refused.

There is deliberately **no cost route and no component route**: Task 3.3 owns
costing and Task 3.2B owns nested recipes, and a route to a service that does
not exist would be a promise the system cannot keep.
"""

from django.urls import path

from apps.kitchen import version_views, views

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
]
