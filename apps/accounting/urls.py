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
    deferral_views,
    expense_views,
    journal_views,
    period_views,
    report_views,
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
    # --- المصروفات -----------------------------------------------------------
    path("expenses/", expense_views.ExpenseVoucherListView.as_view(), name="expense_list"),
    path(
        "expenses/new/",
        expense_views.ExpenseVoucherCreateView.as_view(),
        name="expense_create",
    ),
    path(
        "expenses/<int:pk>/",
        expense_views.ExpenseVoucherDetailView.as_view(),
        name="expense_detail",
    ),
    path(
        "expenses/lines/<int:pk>/delete/",
        expense_views.ExpenseLineDeleteView.as_view(),
        name="expense_line_delete",
    ),
    path(
        "expenses/<int:pk>/approve/",
        expense_views.ExpenseTransitionView.as_view(action="approve"),
        name="expense_approve",
    ),
    path(
        "expenses/<int:pk>/post/",
        expense_views.ExpenseTransitionView.as_view(action="post"),
        name="expense_post",
    ),
    path(
        "expenses/<int:pk>/reverse/",
        expense_views.ExpenseTransitionView.as_view(action="reverse"),
        name="expense_reverse",
    ),
    path(
        "expenses/<int:pk>/discard/",
        expense_views.ExpenseTransitionView.as_view(action="discard"),
        name="expense_discard",
    ),
    # --- المستحقات والمقدمات -------------------------------------------------
    path("deferrals/", deferral_views.DeferralLandingView.as_view(), name="deferral_list"),
    path(
        "deferrals/accruals/new/",
        deferral_views.AccrualCreateView.as_view(),
        name="accrual_create",
    ),
    path(
        "deferrals/accruals/<int:pk>/",
        deferral_views.AccrualDetailView.as_view(),
        name="accrual_detail",
    ),
    path(
        "deferrals/accrual-lines/<int:pk>/delete/",
        deferral_views.AccrualLineDeleteView.as_view(),
        name="accrual_line_delete",
    ),
    path(
        "deferrals/accruals/<int:pk>/approve/",
        deferral_views.AccrualTransitionView.as_view(action="approve"),
        name="accrual_approve",
    ),
    path(
        "deferrals/accruals/<int:pk>/post/",
        deferral_views.AccrualTransitionView.as_view(action="post"),
        name="accrual_post",
    ),
    path(
        "deferrals/accruals/<int:pk>/reverse/",
        deferral_views.AccrualTransitionView.as_view(action="reverse"),
        name="accrual_reverse",
    ),
    path(
        "deferrals/prepayments/new/",
        deferral_views.PrepaymentCreateView.as_view(),
        name="prepayment_create",
    ),
    path(
        "deferrals/prepayments/<int:pk>/",
        deferral_views.PrepaymentDetailView.as_view(),
        name="prepayment_detail",
    ),
    path(
        "deferrals/prepayments/<int:pk>/approve/",
        deferral_views.PrepaymentTransitionView.as_view(action="approve"),
        name="prepayment_approve",
    ),
    path(
        "deferrals/prepayments/<int:pk>/post/",
        deferral_views.PrepaymentTransitionView.as_view(action="post"),
        name="prepayment_post",
    ),
    path(
        "deferrals/schedule-lines/<int:pk>/post/",
        deferral_views.ScheduleLineActionView.as_view(action="post"),
        name="schedule_line_post",
    ),
    path(
        "deferrals/schedule-lines/<int:pk>/reverse/",
        deferral_views.ScheduleLineActionView.as_view(action="reverse"),
        name="schedule_line_reverse",
    ),
    # --- الفترات المحاسبية ----------------------------------------------------
    path("periods/", period_views.PeriodListView.as_view(), name="period_list"),
    path("periods/<int:pk>/", period_views.PeriodDetailView.as_view(), name="period_detail"),
    path(
        "periods/<int:pk>/precheck/",
        period_views.PeriodPrecheckView.as_view(),
        name="period_precheck",
    ),
    path(
        "periods/<int:pk>/soft-close/",
        period_views.PeriodTransitionView.as_view(action="soft_close"),
        name="period_soft_close",
    ),
    path(
        "periods/<int:pk>/close/",
        period_views.PeriodTransitionView.as_view(action="close"),
        name="period_close",
    ),
    path(
        "periods/<int:pk>/reopen/",
        period_views.PeriodTransitionView.as_view(action="reopen"),
        name="period_reopen",
    ),
    # --- التقارير المالية ----------------------------------------------------
    path(
        "reports/trial-balance/",
        report_views.TrialBalanceView.as_view(),
        name="trial_balance",
    ),
    path(
        "reports/general-ledger/",
        report_views.GeneralLedgerView.as_view(),
        name="general_ledger",
    ),
    path(
        "reports/income-statement/",
        report_views.IncomeStatementView.as_view(),
        name="income_statement",
    ),
    path(
        "reports/balance-sheet/",
        report_views.BalanceSheetView.as_view(),
        name="balance_sheet",
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
