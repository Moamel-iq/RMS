"""
Procurement screens, mounted inside the shell.

Archive and reactivate are separate POST-only routes rather than a `DELETE`.
Nothing in this module is ever destroyed: a supplier code that has been used
stays reserved, and a row every posted document points at stays readable.
"""

from django.urls import path

from apps.procurement import report_views, views

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
    # --- effective-dated supplier credit terms ----------------------------
    path(
        "credit-terms/",
        views.SupplierCreditTermListView.as_view(),
        name="credit_term_list",
    ),
    path(
        "credit-terms/new/",
        views.SupplierCreditTermCreateView.as_view(),
        name="credit_term_create",
    ),
    path(
        "credit-terms/<int:pk>/",
        views.SupplierCreditTermDetailView.as_view(),
        name="credit_term_detail",
    ),
    path(
        "credit-terms/<int:pk>/edit/",
        views.SupplierCreditTermUpdateView.as_view(),
        name="credit_term_update",
    ),
    path(
        "credit-terms/<int:pk>/activate/",
        views.SupplierCreditTermActivateView.as_view(),
        name="credit_term_activate",
    ),
    path(
        "credit-terms/<int:pk>/history/",
        views.SupplierCreditTermHistoryView.as_view(),
        name="credit_term_history",
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
    path(
        "orders/<int:pk>/history/",
        views.PurchaseOrderHistoryView.as_view(),
        name="purchase_order_history",
    ),
    path(
        "orders/<int:pk>/revise/",
        views.PurchaseOrderReviseView.as_view(),
        name="purchase_order_revise",
    ),
    # --- goods receipts --------------------------------------------------
    path("receipts/", views.GoodsReceiptListView.as_view(), name="goods_receipt_list"),
    path(
        "receipts/new/",
        views.GoodsReceiptCreateView.as_view(),
        name="goods_receipt_create",
    ),
    path(
        "receipts/<int:pk>/",
        views.GoodsReceiptDetailView.as_view(),
        name="goods_receipt_detail",
    ),
    path(
        "receipts/<int:pk>/lines/<int:line_id>/delete/",
        views.GoodsReceiptLineDeleteView.as_view(),
        name="goods_receipt_line_delete",
    ),
    path(
        "receipts/<int:pk>/lines/<int:line_id>/inspect/",
        views.GoodsReceiptInspectView.as_view(),
        name="goods_receipt_inspect",
    ),
    # Command routes, POST-only. Named for the act rather than for the record,
    # because posting a receipt is not editing one: there is no generic write
    # endpoint over a posted receipt and there is deliberately not going to be.
    path(
        "receipts/<int:pk>/post/",
        views.GoodsReceiptPostView.as_view(),
        name="goods_receipt_post",
    ),
    path(
        "receipts/<int:pk>/reverse/",
        views.GoodsReceiptReverseView.as_view(),
        name="goods_receipt_reverse",
    ),
    # --- supplier invoices -----------------------------------------------
    path(
        "invoices/",
        views.SupplierInvoiceListView.as_view(),
        name="supplier_invoice_list",
    ),
    path(
        "invoices/new/",
        views.SupplierInvoiceCreateView.as_view(),
        name="supplier_invoice_create",
    ),
    path(
        "invoices/<int:pk>/",
        views.SupplierInvoiceDetailView.as_view(),
        name="supplier_invoice_detail",
    ),
    path(
        "invoices/<int:pk>/edit/",
        views.SupplierInvoiceUpdateView.as_view(),
        name="supplier_invoice_update",
    ),
    path(
        "invoices/<int:pk>/lines/<int:line_id>/delete/",
        views.SupplierInvoiceLineDeleteView.as_view(),
        name="supplier_invoice_line_delete",
    ),
    # Command routes, POST-only. There is no writable generic endpoint over a
    # posted invoice and there is deliberately not going to be (PRC-062).
    path(
        "invoices/<int:pk>/approve/",
        views.SupplierInvoiceTransitionView.as_view(transition="approve"),
        name="supplier_invoice_approve",
    ),
    path(
        "invoices/<int:pk>/return/",
        views.SupplierInvoiceTransitionView.as_view(transition="return_to_draft"),
        name="supplier_invoice_return",
    ),
    path(
        "invoices/<int:pk>/post/",
        views.SupplierInvoiceTransitionView.as_view(transition="post"),
        name="supplier_invoice_post",
    ),
    path(
        "invoices/<int:pk>/reverse/",
        views.SupplierInvoiceTransitionView.as_view(transition="reverse"),
        name="supplier_invoice_reverse",
    ),
    # --- structured invoice additional costs -----------------------------
    path(
        "additional-costs/",
        views.SupplierInvoiceChargeListView.as_view(),
        name="supplier_invoice_charge_list",
    ),
    path(
        "invoices/<int:pk>/additional-costs/new/",
        views.SupplierInvoiceChargeCreateView.as_view(),
        name="supplier_invoice_charge_create",
    ),
    path(
        "additional-costs/<int:pk>/",
        views.SupplierInvoiceChargeDetailView.as_view(),
        name="supplier_invoice_charge_detail",
    ),
    path(
        "additional-costs/<int:pk>/edit/",
        views.SupplierInvoiceChargeUpdateView.as_view(),
        name="supplier_invoice_charge_update",
    ),
    path(
        "additional-costs/<int:pk>/delete/",
        views.SupplierInvoiceChargeDeleteView.as_view(),
        name="supplier_invoice_charge_delete",
    ),
    path(
        "additional-costs/<int:pk>/allocation/",
        views.SupplierInvoiceChargeAllocationView.as_view(),
        name="supplier_invoice_charge_allocation",
    ),
    # --- three-way matching ------------------------------------------------
    path("matching/", views.MatchingQueueView.as_view(), name="matching_queue"),
    path("matches/", views.PurchaseMatchListView.as_view(), name="purchase_match_list"),
    path(
        "matches/<int:pk>/",
        views.PurchaseMatchDetailView.as_view(),
        name="purchase_match_detail",
    ),
    # Opening a match is an act on the invoice, so it hangs off the invoice.
    path(
        "invoices/<int:pk>/match/",
        views.PurchaseMatchCreateView.as_view(),
        name="purchase_match_create",
    ),
    path(
        "matches/<int:pk>/allocations/<int:allocation_id>/delete/",
        views.MatchAllocationDeleteView.as_view(),
        name="match_allocation_delete",
    ),
    # Command routes, POST-only. `ready` freezes the evidence and posts
    # nothing — Task 2.12 is what turns a ready match into a journal.
    path(
        "matches/<int:pk>/ready/",
        views.PurchaseMatchTransitionView.as_view(transition="ready"),
        name="purchase_match_ready",
    ),
    path(
        "matches/<int:pk>/cancel/",
        views.PurchaseMatchTransitionView.as_view(transition="cancel"),
        name="purchase_match_cancel",
    ),
    # --- supplier returns --------------------------------------------------
    path("returns/", views.SupplierReturnListView.as_view(), name="supplier_return_list"),
    path(
        "returns/new/",
        views.SupplierReturnCreateView.as_view(),
        name="supplier_return_create",
    ),
    path(
        "returns/<int:pk>/",
        views.SupplierReturnDetailView.as_view(),
        name="supplier_return_detail",
    ),
    path(
        "returns/<int:pk>/lines/<int:line_id>/delete/",
        views.SupplierReturnLineDeleteView.as_view(),
        name="supplier_return_line_delete",
    ),
    # Command routes, POST-only. Posting a return takes goods out of stock and
    # money out of the inventory account; there is no writable generic
    # endpoint over a posted return and there is deliberately not going to be.
    path(
        "returns/<int:pk>/post/",
        views.SupplierReturnTransitionView.as_view(transition="post"),
        name="supplier_return_post",
    ),
    path(
        "returns/<int:pk>/reverse/",
        views.SupplierReturnTransitionView.as_view(transition="reverse"),
        name="supplier_return_reverse",
    ),
    # --- supplier credit notes ---------------------------------------------
    path(
        "credit-notes/",
        views.SupplierCreditNoteListView.as_view(),
        name="supplier_credit_note_list",
    ),
    path(
        "credit-notes/new/",
        views.SupplierCreditNoteCreateView.as_view(),
        name="supplier_credit_note_create",
    ),
    path(
        "credit-notes/<int:pk>/",
        views.SupplierCreditNoteDetailView.as_view(),
        name="supplier_credit_note_detail",
    ),
    path(
        "credit-notes/<int:pk>/allocations/<int:allocation_id>/delete/",
        views.CreditAllocationDeleteView.as_view(),
        name="credit_allocation_delete",
    ),
    path(
        "credit-notes/<int:pk>/settlements/<int:allocation_id>/delete/",
        views.CreditReturnAllocationDeleteView.as_view(),
        name="credit_return_allocation_delete",
    ),
    # Command routes, POST-only. Posting a note moves the payable; there is
    # no writable generic endpoint over a posted note and deliberately none.
    path(
        "credit-notes/<int:pk>/post/",
        views.SupplierCreditNoteTransitionView.as_view(transition="post"),
        name="supplier_credit_note_post",
    ),
    path(
        "credit-notes/<int:pk>/reverse/",
        views.SupplierCreditNoteTransitionView.as_view(transition="reverse"),
        name="supplier_credit_note_reverse",
    ),
    # --- supplier payments -------------------------------------------------
    path(
        "payments/",
        views.SupplierPaymentListView.as_view(),
        name="supplier_payment_list",
    ),
    path(
        "payments/new/",
        views.SupplierPaymentCreateView.as_view(),
        name="supplier_payment_create",
    ),
    path(
        "payments/<int:pk>/",
        views.SupplierPaymentDetailView.as_view(),
        name="supplier_payment_detail",
    ),
    path(
        "payments/<int:pk>/allocations/<int:allocation_id>/delete/",
        views.PaymentAllocationDeleteView.as_view(),
        name="payment_allocation_delete",
    ),
    # Command routes, POST-only. Money leaves here; there is no writable
    # generic endpoint over a posted payment and deliberately none.
    path(
        "payments/<int:pk>/post/",
        views.SupplierPaymentTransitionView.as_view(transition="post"),
        name="supplier_payment_post",
    ),
    path(
        "payments/<int:pk>/reverse/",
        views.SupplierPaymentTransitionView.as_view(transition="reverse"),
        name="supplier_payment_reverse",
    ),
    # --- reports (Task 2.16) ----------------------------------------------
    # GET-only screens on the Phase 1 report contract: same queryset for the
    # screen and its CSV, cost columns omitted without view_supplier_cost.
    path(
        "reports/supplier-aging/",
        report_views.SupplierAgingReportView.as_view(),
        name="report_supplier_aging",
    ),
    path(
        "reports/supplier-statement/",
        report_views.SupplierStatementReportView.as_view(),
        name="report_supplier_statement",
    ),
    path(
        "reports/open-orders/",
        report_views.OpenPurchaseOrdersReportView.as_view(),
        name="report_open_purchase_orders",
    ),
    path(
        "reports/outstanding-receipts/",
        report_views.OutstandingReceiptQuantityReportView.as_view(),
        name="report_outstanding_receipts",
    ),
    path(
        "reports/grni-exceptions/",
        report_views.GrniExceptionsReportView.as_view(),
        name="report_grni_exceptions",
    ),
    path(
        "reports/invoice-without-receipt/",
        report_views.InvoiceWithoutReceiptReportView.as_view(),
        name="report_invoice_without_receipt",
    ),
    path(
        "reports/matching-exceptions/",
        report_views.MatchingExceptionsReportView.as_view(),
        name="report_matching_exceptions",
    ),
    path(
        "reports/purchase-spend/",
        report_views.PurchaseSpendReportView.as_view(),
        name="report_purchase_spend",
    ),
    path(
        "reports/price-variance/",
        report_views.PriceVarianceReportView.as_view(),
        name="report_price_variance",
    ),
    path(
        "reports/return-credit-status/",
        report_views.ReturnCreditStatusReportView.as_view(),
        name="report_return_credit_status",
    ),
    path(
        "reports/payment-allocations/",
        report_views.PaymentAllocationsReportView.as_view(),
        name="report_payment_allocations",
    ),
    path(
        "reports/procurement-to-gl/",
        report_views.ProcurementToGlReportView.as_view(),
        name="report_procurement_to_gl",
    ),
]
