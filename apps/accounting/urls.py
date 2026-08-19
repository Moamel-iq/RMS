"""
Accounting screens.

Close, archive, post, reverse and discard are POST-only routes rather than
DELETEs or GETs: a mapping that postings snapshotted stays readable forever,
one recorded in error is archived rather than destroyed, and a GET that posted a
journal would fire on a link prefetch.
"""

from django.urls import path

from apps.accounting import (
    cash_views,
    chart_views,
    dashboard_views,
    journal_views,
    subledger_views,
    views,
)

app_name = "accounting"

urlpatterns = [
    # --- لوحة المحاسبة -------------------------------------------------------
    path("", dashboard_views.AccountingDashboardView.as_view(), name="dashboard"),
    path(
        "cards/<str:key>/",
        dashboard_views.DashboardCardView.as_view(),
        name="dashboard_card",
    ),
    # --- الأدوار المحاسبية ---------------------------------------------------
    path("roles/", views.AccountRoleListView.as_view(), name="role_list"),
    path("roles/<int:pk>/", views.AccountRoleDetailView.as_view(), name="role_detail"),
    # --- ربط الحسابات --------------------------------------------------------
    path("mappings/", views.AccountMappingListView.as_view(), name="mapping_list"),
    path("mappings/new/", views.AccountMappingCreateView.as_view(), name="mapping_create"),
    path("mappings/preview/", views.MappingPreviewView.as_view(), name="mapping_preview"),
    path(
        "mappings/<int:pk>/amend/",
        views.AccountMappingAmendView.as_view(),
        name="mapping_amend",
    ),
    path(
        "mappings/<int:pk>/close/",
        views.AccountMappingCloseView.as_view(),
        name="mapping_close",
    ),
    path(
        "mappings/<int:pk>/archive/",
        views.AccountMappingArchiveView.as_view(),
        name="mapping_archive",
    ),
    # --- دليل الحسابات -------------------------------------------------------
    path("accounts/", chart_views.ChartTreeView.as_view(), name="chart_tree"),
    path("accounts/list/", chart_views.ChartListView.as_view(), name="chart_list"),
    path("accounts/new/", chart_views.AccountCreateView.as_view(), name="account_create"),
    path(
        "accounts/<int:pk>/children/",
        chart_views.ChartChildrenView.as_view(),
        name="chart_children",
    ),
    path("accounts/<int:pk>/", chart_views.AccountDetailView.as_view(), name="account_detail"),
    path(
        "accounts/<int:pk>/edit/",
        chart_views.AccountUpdateView.as_view(),
        name="account_update",
    ),
    path(
        "accounts/<int:pk>/activity/",
        chart_views.AccountActivityView.as_view(),
        name="account_activity",
    ),
    path(
        "accounts/<int:pk>/statement-group/",
        chart_views.AccountReportMappingView.as_view(),
        name="account_report_mapping",
    ),
    path(
        "accounts/<int:pk>/archive/",
        chart_views.AccountArchiveView.as_view(),
        name="account_archive",
    ),
    path(
        "accounts/<int:pk>/reactivate/",
        chart_views.AccountReactivateView.as_view(),
        name="account_reactivate",
    ),
    # --- قيود اليومية --------------------------------------------------------
    path("journals/", journal_views.JournalListView.as_view(), name="journal_list"),
    path("journals/new/", journal_views.JournalCreateView.as_view(), name="journal_create"),
    path(
        "journals/<int:pk>/",
        journal_views.JournalDetailView.as_view(),
        name="journal_detail",
    ),
    path(
        "journals/lines/<int:pk>/delete/",
        journal_views.JournalLineDeleteView.as_view(),
        name="journal_line_delete",
    ),
    path(
        "journals/<int:pk>/post/",
        journal_views.JournalTransitionView.as_view(action="post"),
        name="journal_post",
    ),
    path(
        "journals/<int:pk>/reverse/",
        journal_views.JournalTransitionView.as_view(action="reverse"),
        name="journal_reverse",
    ),
    path(
        "journals/<int:pk>/discard/",
        journal_views.JournalTransitionView.as_view(action="discard"),
        name="journal_discard",
    ),
    # --- الصناديق ------------------------------------------------------------
    path("cashboxes/", cash_views.CashboxListView.as_view(), name="cashbox_list"),
    path("cashboxes/new/", cash_views.CashboxCreateView.as_view(), name="cashbox_create"),
    path("cashboxes/<int:pk>/", cash_views.CashboxDetailView.as_view(), name="cashbox_detail"),
    path(
        "cashboxes/<int:pk>/edit/",
        cash_views.CashboxUpdateView.as_view(),
        name="cashbox_update",
    ),
    path(
        "cashboxes/<int:pk>/archive/",
        cash_views.CashboxActionView.as_view(action="archive"),
        name="cashbox_archive",
    ),
    path(
        "cashboxes/<int:pk>/reactivate/",
        cash_views.CashboxActionView.as_view(action="reactivate"),
        name="cashbox_reactivate",
    ),
    path(
        "cashboxes/<int:pk>/reconcile/",
        cash_views.CashboxActionView.as_view(action="reconcile"),
        name="cashbox_reconcile",
    ),
    # --- الحسابات البنكية ----------------------------------------------------
    path("bank-accounts/", cash_views.BankAccountListView.as_view(), name="bank_account_list"),
    path(
        "bank-accounts/new/",
        cash_views.BankAccountCreateView.as_view(),
        name="bank_account_create",
    ),
    path(
        "bank-accounts/<int:pk>/",
        cash_views.BankAccountDetailView.as_view(),
        name="bank_account_detail",
    ),
    path(
        "bank-accounts/<int:pk>/edit/",
        cash_views.BankAccountUpdateView.as_view(),
        name="bank_account_update",
    ),
    path(
        "bank-accounts/<int:pk>/archive/",
        cash_views.BankAccountActionView.as_view(action="archive"),
        name="bank_account_archive",
    ),
    path(
        "bank-accounts/<int:pk>/reactivate/",
        cash_views.BankAccountActionView.as_view(action="reactivate"),
        name="bank_account_reactivate",
    ),
    path(
        "bank-accounts/<int:pk>/reconcile/",
        cash_views.BankAccountActionView.as_view(action="reconcile"),
        name="bank_account_reconcile",
    ),
    # --- ذمم الموردين --------------------------------------------------------
    path(
        "supplier-liabilities/",
        subledger_views.SupplierLiabilityListView.as_view(),
        name="supplier_liability_list",
    ),
    path(
        "supplier-liabilities/<int:pk>/",
        subledger_views.SupplierLiabilityDetailView.as_view(),
        name="supplier_liability_detail",
    ),
    # --- ذمم التطبيقات -------------------------------------------------------
    path(
        "application-receivables/",
        subledger_views.ApplicationReceivableListView.as_view(),
        name="application_receivable_list",
    ),
    path(
        "application-receivables/<int:pk>/",
        subledger_views.ApplicationReceivableDetailView.as_view(),
        name="application_receivable_detail",
    ),
]
