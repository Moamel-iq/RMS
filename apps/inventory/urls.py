"""
Inventory screens, mounted inside the shell.

Archive and reactivate are separate POST-only routes rather than a `DELETE`.
Nothing in this module is ever destroyed: a code that has been used stays
reserved, and a row that has been referenced stays readable.
"""

from django.urls import path
from django.utils.translation import gettext_lazy as _

from apps.inventory import report_views, views
from apps.inventory.models import InventoryDocumentType

app_name = "inventory"

urlpatterns = [
    # --- overview ----------------------------------------------------------
    # Mounted at the module root: opening the module lands on the summary, and
    # every deeper screen is reached from it.
    path("", report_views.InventoryOverviewView.as_view(), name="overview"),
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
        "package-units/quick-add/",
        views.PackageUnitQuickCreateView.as_view(),
        name="package_unit_quick_add",
    ),
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


# --- operational documents (Task 1.4) ---------------------------------------
#
# Identical route sets, generated rather than written out once per type. The
# document type is bound into the view here, so it comes from the URL and
# never from a request body a caller controls: `/issues/` posts issues and an
# id from one series cannot resolve under another.
#
# The un-invoiced receipt and the return-from-issue were withdrawn from the
# product: goods now enter stock through a purchase goods receipt, which
# carries the supplier and the liability that an un-invoiced receipt never
# had. Their `InventoryDocumentType` values went with them.

_OPERATIONAL_SCREENS = (
    (
        "issues",
        InventoryDocumentType.ISSUE,
        _("صرف مخزني للاستهلاك"),
        _("بضاعة تخرج من العهدة للاستهلاك النهائي. ليست تحويلاً بين المخازن."),
        _("صرف جديد"),
    ),
    (
        "waste",
        InventoryDocumentType.WASTE,
        _("إتلاف مخزني"),
        _("بضاعة تالفة تُشطب من العهدة بسبب مُسجَّل ومركز كلفة. ليست صرفاً للاستهلاك."),
        _("إتلاف جديد"),
    ),
)

for _path, _type, _title, _hint, _create_label in _OPERATIONAL_SCREENS:
    _slug = _type.lower()
    urlpatterns += [
        path(
            f"{_path}/",
            views.OperationalListView.as_view(
                document_type=_type,
                page_title=_title,
                page_hint=_hint,
                create_url_name=f"inventory:{_slug}_create",
                create_label=_create_label,
            ),
            name=f"{_slug}_list",
        ),
        path(
            f"{_path}/new/",
            views.OperationalCreateView.as_view(
                document_type=_type, page_title=_create_label, page_hint=_hint
            ),
            name=f"{_slug}_create",
        ),
        path(
            f"{_path}/<int:pk>/",
            views.OperationalDetailView.as_view(document_type=_type),
            name=f"{_slug}_detail",
        ),
        path(
            f"{_path}/<int:pk>/post/",
            views.OperationalActionView.as_view(document_type=_type, action="post"),
            name=f"{_slug}_post",
        ),
        path(
            f"{_path}/<int:pk>/reverse/",
            views.OperationalActionView.as_view(document_type=_type, action="reverse"),
            name=f"{_slug}_reverse",
        ),
        path(
            f"{_path}/<int:pk>/delete/",
            views.OperationalActionView.as_view(document_type=_type, action="delete"),
            name=f"{_slug}_delete",
        ),
        path(
            f"{_path}/<int:pk>/lines/<int:line_pk>/delete/",
            views.OperationalActionView.as_view(document_type=_type, action="delete_line"),
            name=f"{_slug}_line_delete",
        ),
    ]


# --- transfers (Task 1.5) ---------------------------------------------------
#
# A receipt and a shortage live under their own top-level paths once they
# exist, and are *created* under their transfer's path. The nesting is where
# the route constrains the object: `/transfers/7/receipts/new/` can only make
# a receipt against transfer 7, and a receipt id from another transfer 404s
# rather than resolving quietly.

urlpatterns += [
    path("transfers/", views.TransferListView.as_view(), name="transfer_list"),
    path("transfers/new/", views.TransferCreateView.as_view(), name="transfer_create"),
    path("transfers/<int:pk>/", views.TransferDetailView.as_view(), name="transfer_detail"),
    path(
        "transfers/<int:pk>/dispatch/",
        views.TransferDispatchView.as_view(),
        name="transfer_dispatch",
    ),
    path(
        "transfers/<int:pk>/reverse-dispatch/",
        views.TransferActionView.as_view(action="reverse"),
        name="transfer_reverse",
    ),
    path(
        "transfers/<int:pk>/delete/",
        views.TransferActionView.as_view(action="delete"),
        name="transfer_delete",
    ),
    path(
        "transfers/<int:pk>/lines/<int:line_pk>/delete/",
        views.TransferActionView.as_view(action="delete_line"),
        name="transfer_line_delete",
    ),
    path(
        "transfers/<int:pk>/receipts/new/",
        views.TransferReceiptCreateView.as_view(),
        name="transfer_receipt_create",
    ),
    path(
        "transfer-receipts/<int:pk>/",
        views.TransferReceiptDetailView.as_view(),
        name="transfer_receipt_detail",
    ),
    path(
        "transfer-receipts/<int:pk>/post/",
        views.TransferReceiptActionView.as_view(action="post"),
        name="transfer_receipt_post",
    ),
    path(
        "transfer-receipts/<int:pk>/reverse/",
        views.TransferReceiptActionView.as_view(action="reverse"),
        name="transfer_receipt_reverse",
    ),
    path(
        "transfer-receipts/<int:pk>/delete/",
        views.TransferReceiptActionView.as_view(action="delete"),
        name="transfer_receipt_delete",
    ),
    path(
        "transfers/<int:pk>/shortage/",
        views.TransferShortageCreateView.as_view(),
        name="transfer_shortage_create",
    ),
    path(
        "transfer-shortages/<int:pk>/reverse/",
        views.TransferShortageActionView.as_view(action="reverse"),
        name="transfer_shortage_reverse",
    ),
]

# --- reason codes, counts and adjustments (Task 1.6) ------------------------

urlpatterns += [
    path("counts/", views.StockCountListView.as_view(), name="count_list"),
    path("counts/new/", views.StockCountCreateView.as_view(), name="count_create"),
    path("counts/<int:pk>/", views.StockCountDetailView.as_view(), name="count_detail"),
    # The blind sheet is its own route with its own view and its own template.
    # Sharing the detail view and hiding columns would mean the book quantity
    # was fetched and merely not printed — which is not blind.
    path("counts/<int:pk>/sheet/", views.BlindCountView.as_view(), name="count_sheet"),
    path(
        "counts/<int:pk>/unexpected/",
        views.UnexpectedCountLineView.as_view(),
        name="count_unexpected",
    ),
    path(
        "counts/<int:pk>/start/",
        views.StockCountActionView.as_view(action="start"),
        name="count_start",
    ),
    path(
        "counts/<int:pk>/submit/",
        views.StockCountActionView.as_view(action="submit"),
        name="count_submit",
    ),
    path(
        "counts/<int:pk>/approve/",
        views.StockCountActionView.as_view(action="approve"),
        name="count_approve",
    ),
    path(
        "counts/<int:pk>/cancel/",
        views.StockCountActionView.as_view(action="cancel"),
        name="count_cancel",
    ),
    path(
        "counts/<int:pk>/reverse/",
        views.StockCountActionView.as_view(action="reverse"),
        name="count_reverse",
    ),
    path(
        "counts/<int:pk>/delete/",
        views.StockCountActionView.as_view(action="delete"),
        name="count_delete",
    ),
    path("adjustments/", views.AdjustmentListView.as_view(), name="adjustment_list"),
    path("adjustments/new/", views.AdjustmentCreateView.as_view(), name="adjustment_create"),
    path(
        "adjustments/<int:pk>/",
        views.AdjustmentDetailView.as_view(),
        name="adjustment_detail",
    ),
    path(
        "adjustments/<int:pk>/post/",
        views.AdjustmentActionView.as_view(action="post"),
        name="adjustment_post",
    ),
    path(
        "adjustments/<int:pk>/reverse/",
        views.AdjustmentActionView.as_view(action="reverse"),
        name="adjustment_reverse",
    ),
    path(
        "adjustments/<int:pk>/delete/",
        views.AdjustmentActionView.as_view(action="delete"),
        name="adjustment_delete",
    ),
]

# ---------------------------------------------------------------------------
# Task 1.7 — reports and imports
# ---------------------------------------------------------------------------
# Reports live under one prefix so the sidebar section and the permission story
# are both legible. Every one of them is a GET, is scoped by the same selector
# the operational screens use, and carries `?export=csv` on the same query.

urlpatterns += [
    path(
        "reports/valuation/",
        report_views.StockValuationReportView.as_view(),
        name="report_valuation",
    ),
    path(
        "reports/stock-card/",
        report_views.StockCardReportView.as_view(),
        name="report_stock_card",
    ),
    path("reports/expiry/", report_views.ExpiryReportView.as_view(), name="report_expiry"),
    path("reports/reorder/", report_views.ReorderReportView.as_view(), name="report_reorder"),
    path("reports/waste/", report_views.WasteReportView.as_view(), name="report_waste"),
    path(
        "reports/count-variance/",
        report_views.CountVarianceReportView.as_view(),
        name="report_count_variance",
    ),
    path(
        "reports/adjustments/",
        report_views.AdjustmentReportView.as_view(),
        name="report_adjustments",
    ),
    path(
        "reports/locations/",
        report_views.LocationBalanceReportView.as_view(),
        name="report_locations",
    ),
]
