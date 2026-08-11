"""
Procurement screens, mounted inside the shell.

Archive and reactivate are separate POST-only routes rather than a `DELETE`.
Nothing in this module is ever destroyed: a supplier code that has been used
stays reserved, and a row every posted document points at stays readable.
"""

from django.urls import path

from apps.procurement import views

app_name = "procurement"

urlpatterns = [
    path("suppliers/", views.SupplierListView.as_view(), name="supplier_list"),
    path("suppliers/new/", views.SupplierCreateView.as_view(), name="supplier_create"),
    path("suppliers/<int:pk>/", views.SupplierUpdateView.as_view(), name="supplier_update"),
    path(
        "suppliers/<int:pk>/archive/",
        views.SupplierActionView.as_view(activate=False),
        name="supplier_archive",
    ),
    path(
        "suppliers/<int:pk>/reactivate/",
        views.SupplierActionView.as_view(activate=True),
        name="supplier_reactivate",
    ),
    # --- supplier item catalogue -------------------------------------
    path(
        "catalogue/",
        views.SupplierItemListView.as_view(),
        name="supplier_item_list",
    ),
    path(
        "catalogue/new/",
        views.SupplierItemCreateView.as_view(),
        name="supplier_item_create",
    ),
    path(
        "catalogue/<int:pk>/",
        views.SupplierItemUpdateView.as_view(),
        name="supplier_item_update",
    ),
    path(
        "catalogue/<int:pk>/archive/",
        views.SupplierItemActionView.as_view(activate=False),
        name="supplier_item_archive",
    ),
    path(
        "catalogue/<int:pk>/reactivate/",
        views.SupplierItemActionView.as_view(activate=True),
        name="supplier_item_reactivate",
    ),
]
