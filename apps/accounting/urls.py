"""
Accounting screens — the Task 1.3 account-mapping surface.

Close and archive are POST-only routes rather than a DELETE: a mapping that
postings snapshotted stays readable forever, and one recorded in error is
archived, never destroyed.
"""

from django.urls import path

from apps.accounting import views

app_name = "accounting"

urlpatterns = [
    path("roles/", views.AccountRoleListView.as_view(), name="role_list"),
    path("mappings/", views.AccountMappingListView.as_view(), name="mapping_list"),
    path("mappings/new/", views.AccountMappingCreateView.as_view(), name="mapping_create"),
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
]
