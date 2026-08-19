"""
Accounting screens.

Close, archive, post, reverse and discard are POST-only routes rather than
DELETEs or GETs: a mapping that postings snapshotted stays readable forever,
one recorded in error is archived rather than destroyed, and a GET that posted a
journal would fire on a link prefetch.
"""

from django.urls import path

from apps.accounting import chart_views, dashboard_views, journal_views, views

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
]
