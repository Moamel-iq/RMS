"""
Inventory screens, mounted inside the shell.

Archive and reactivate are separate POST-only routes rather than a `DELETE`.
Nothing in this module is ever destroyed: a code that has been used stays
reserved, and a row that has been referenced stays readable.
"""

from django.urls import path

from apps.inventory import views

app_name = "inventory"

urlpatterns = [
    # --- categories --------------------------------------------------------
    path("categories/", views.ItemCategoryListView.as_view(), name="category_list"),
    path("categories/new/", views.ItemCategoryCreateView.as_view(), name="category_create"),
    path("categories/<int:pk>/", views.ItemCategoryUpdateView.as_view(), name="category_update"),
    path(
        "categories/<int:pk>/archive/",
        views.ItemCategoryActionView.as_view(activate=False),
        name="category_archive",
    ),
    path(
        "categories/<int:pk>/reactivate/",
        views.ItemCategoryActionView.as_view(activate=True),
        name="category_reactivate",
    ),
    # --- package units -----------------------------------------------------
    path("package-units/", views.PackageUnitListView.as_view(), name="package_unit_list"),
    path("package-units/new/", views.PackageUnitCreateView.as_view(), name="package_unit_create"),
    path(
        "package-units/<int:pk>/",
        views.PackageUnitUpdateView.as_view(),
        name="package_unit_update",
    ),
    path(
        "package-units/<int:pk>/archive/",
        views.PackageUnitActionView.as_view(activate=False),
        name="package_unit_archive",
    ),
    path(
        "package-units/<int:pk>/reactivate/",
        views.PackageUnitActionView.as_view(activate=True),
        name="package_unit_reactivate",
    ),
    # --- items -------------------------------------------------------------
    path("items/", views.ItemListView.as_view(), name="item_list"),
    path("items/new/", views.ItemCreateView.as_view(), name="item_create"),
    path("items/<int:pk>/", views.ItemUpdateView.as_view(), name="item_update"),
    path(
        "items/<int:pk>/archive/",
        views.ItemActionView.as_view(activate=False),
        name="item_archive",
    ),
    path(
        "items/<int:pk>/reactivate/",
        views.ItemActionView.as_view(activate=True),
        name="item_reactivate",
    ),
    # --- item package conversions ------------------------------------------
    path("conversions/", views.ItemConversionListView.as_view(), name="conversion_list"),
    path("conversions/new/", views.ItemConversionCreateView.as_view(), name="conversion_create"),
    path(
        "conversions/<int:pk>/",
        views.ItemConversionUpdateView.as_view(),
        name="conversion_update",
    ),
    path(
        "conversions/<int:pk>/supersede/",
        views.ItemConversionSupersedeView.as_view(),
        name="conversion_supersede",
    ),
    path(
        "conversions/<int:pk>/archive/",
        views.ItemConversionActionView.as_view(activate=False),
        name="conversion_archive",
    ),
    path(
        "conversions/<int:pk>/reactivate/",
        views.ItemConversionActionView.as_view(activate=True),
        name="conversion_reactivate",
    ),
    # --- stock and movements (read only) -----------------------------------
    path("stock/", views.StockOnHandView.as_view(), name="stock_list"),
    path("movements/", views.MovementHistoryView.as_view(), name="movement_list"),
    path("movements/<int:pk>/", views.MovementDetailView.as_view(), name="movement_detail"),
    # --- account-mapping overrides (Task 1.3) -------------------------------
    path("account-mappings/", views.InventoryMappingListView.as_view(), name="mapping_list"),
    path(
        "account-mappings/new/",
        views.InventoryMappingCreateView.as_view(),
        name="mapping_create",
    ),
    path(
        "account-mappings/<int:pk>/close/",
        views.InventoryMappingCloseView.as_view(),
        name="mapping_close",
    ),
    path(
        "account-mappings/<int:pk>/archive/",
        views.InventoryMappingArchiveView.as_view(),
        name="mapping_archive",
    ),
    # --- opening stock documents (Task 1.3) ---------------------------------
    path("openings/", views.OpeningListView.as_view(), name="opening_list"),
    path("openings/new/", views.OpeningCreateView.as_view(), name="opening_create"),
    path("openings/<int:pk>/", views.OpeningDetailView.as_view(), name="opening_detail"),
    path("openings/<int:pk>/edit/", views.OpeningUpdateView.as_view(), name="opening_update"),
    path(
        "openings/<int:pk>/submit/",
        views.OpeningActionView.as_view(action="submit"),
        name="opening_submit",
    ),
    path(
        "openings/<int:pk>/return/",
        views.OpeningActionView.as_view(action="return"),
        name="opening_return",
    ),
    path(
        "openings/<int:pk>/post/",
        views.OpeningActionView.as_view(action="post"),
        name="opening_post",
    ),
    path(
        "openings/<int:pk>/reverse/",
        views.OpeningActionView.as_view(action="reverse"),
        name="opening_reverse",
    ),
    path(
        "openings/<int:pk>/delete/",
        views.OpeningActionView.as_view(action="delete"),
        name="opening_delete",
    ),
    path(
        "openings/<int:pk>/lines/<int:line_pk>/delete/",
        views.OpeningActionView.as_view(action="delete_line"),
        name="opening_line_delete",
    ),
    # --- reconciliation (read only) -----------------------------------------
    path("reconciliation/", views.ReconciliationView.as_view(), name="reconciliation"),
    # --- warehouses --------------------------------------------------------
    path("warehouses/", views.WarehouseListView.as_view(), name="warehouse_list"),
    path("warehouses/new/", views.WarehouseCreateView.as_view(), name="warehouse_create"),
    path("warehouses/<int:pk>/", views.WarehouseUpdateView.as_view(), name="warehouse_update"),
    path(
        "warehouses/<int:pk>/archive/",
        views.WarehouseActionView.as_view(activate=False),
        name="warehouse_archive",
    ),
    path(
        "warehouses/<int:pk>/reactivate/",
        views.WarehouseActionView.as_view(activate=True),
        name="warehouse_reactivate",
    ),
]
