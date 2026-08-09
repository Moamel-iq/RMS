"""Inventory screens, mounted inside the shell."""

from django.urls import path

from apps.inventory import views

app_name = "inventory"

urlpatterns = [
    path("categories/", views.ItemCategoryListView.as_view(), name="category_list"),
    path("package-units/", views.PackageUnitListView.as_view(), name="package_unit_list"),
    path("items/", views.ItemListView.as_view(), name="item_list"),
    path("conversions/", views.ItemConversionListView.as_view(), name="conversion_list"),
    path("warehouses/", views.WarehouseListView.as_view(), name="warehouse_list"),
]
