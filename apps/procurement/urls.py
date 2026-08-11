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
    # --- purchase requests -------------------------------------------
    path("requests/", views.PurchaseRequestListView.as_view(), name="purchase_request_list"),
    path(
        "requests/new/",
        views.PurchaseRequestCreateView.as_view(),
        name="purchase_request_create",
    ),
    path(
        "requests/<int:pk>/",
        views.PurchaseRequestDetailView.as_view(),
        name="purchase_request_detail",
    ),
    path(
        "requests/<int:pk>/lines/<int:line_id>/delete/",
        views.PurchaseRequestLineDeleteView.as_view(),
        name="purchase_request_line_delete",
    ),
    path(
        "requests/<int:pk>/submit/",
        views.PurchaseRequestTransitionView.as_view(transition="submit"),
        name="purchase_request_submit",
    ),
    path(
        "requests/<int:pk>/approve/",
        views.PurchaseRequestTransitionView.as_view(transition="approve"),
        name="purchase_request_approve",
    ),
    path(
        "requests/<int:pk>/reject/",
        views.PurchaseRequestTransitionView.as_view(transition="reject"),
        name="purchase_request_reject",
    ),
    path(
        "requests/<int:pk>/cancel/",
        views.PurchaseRequestTransitionView.as_view(transition="cancel"),
        name="purchase_request_cancel",
    ),
    # --- supplier quotations -------------------------------------------
    path("quotations/", views.SupplierQuotationListView.as_view(), name="quotation_list"),
    path(
        "quotations/new/",
        views.SupplierQuotationCreateView.as_view(),
        name="quotation_create",
    ),
    path(
        "quotations/<int:pk>/",
        views.SupplierQuotationDetailView.as_view(),
        name="quotation_detail",
    ),
    path(
        "quotations/<int:pk>/lines/<int:line_id>/delete/",
        views.SupplierQuotationLineDeleteView.as_view(),
        name="quotation_line_delete",
    ),
    path(
        "quotations/<int:pk>/submit/",
        views.SupplierQuotationTransitionView.as_view(transition="submit"),
        name="quotation_submit",
    ),
    path(
        "quotations/<int:pk>/decline/",
        views.SupplierQuotationTransitionView.as_view(transition="decline"),
        name="quotation_decline",
    ),
    path(
        "requests/<int:pk>/comparison/",
        views.QuotationComparisonView.as_view(),
        name="quotation_comparison",
    ),
    path(
        "requests/<int:pk>/award/",
        views.QuotationAwardView.as_view(),
        name="quotation_award",
    ),
    # --- purchase orders -------------------------------------------------
    path("orders/", views.PurchaseOrderListView.as_view(), name="purchase_order_list"),
    path(
        "orders/new/",
        views.PurchaseOrderCreateView.as_view(),
        name="purchase_order_create",
    ),
    path(
        "orders/<int:pk>/",
        views.PurchaseOrderDetailView.as_view(),
        name="purchase_order_detail",
    ),
    path(
        "orders/<int:pk>/lines/<int:line_id>/delete/",
        views.PurchaseOrderLineDeleteView.as_view(),
        name="purchase_order_line_delete",
    ),
    path(
        "orders/<int:pk>/approve/",
        views.PurchaseOrderTransitionView.as_view(transition="approve"),
        name="purchase_order_approve",
    ),
    path(
        "orders/<int:pk>/issue/",
        views.PurchaseOrderTransitionView.as_view(transition="issue"),
        name="purchase_order_issue",
    ),
    path(
        "orders/<int:pk>/cancel/",
        views.PurchaseOrderTransitionView.as_view(transition="cancel"),
        name="purchase_order_cancel",
    ),
]
