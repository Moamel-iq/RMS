"""Organization settings routes."""

from django.urls import path

from apps.organizations import role_views, views

app_name = "organizations"

urlpatterns = [
    path("organizations/", views.OrganizationListView.as_view(), name="organization_list"),
    path("organizations/new/", views.OrganizationCreateView.as_view(), name="organization_create"),
    path(
        "organizations/<int:pk>/",
        views.OrganizationUpdateView.as_view(),
        name="organization_update",
    ),
    path("branches/", views.BranchListView.as_view(), name="branch_list"),
    path("branches/new/", views.BranchCreateView.as_view(), name="branch_create"),
    path("branches/<int:pk>/", views.BranchUpdateView.as_view(), name="branch_update"),
    path("access/", views.BranchAccessListView.as_view(), name="access_list"),
    path(
        "access/requests/",
        views.AccessChangeRequestListView.as_view(),
        name="access_request_list",
    ),
    path(
        "access/requests/new/",
        views.AccessChangeRequestCreateView.as_view(),
        name="access_request_create",
    ),
    path(
        "access/requests/<int:pk>/approve/",
        views.AccessChangeRequestDecisionView.as_view(decision="approve"),
        name="access_request_approve",
    ),
    path(
        "access/requests/<int:pk>/reject/",
        views.AccessChangeRequestDecisionView.as_view(decision="reject"),
        name="access_request_reject",
    ),
    path(
        "access/requests/<int:pk>/cancel/",
        views.AccessChangeRequestDecisionView.as_view(decision="cancel"),
        name="access_request_cancel",
    ),
    # The posts an organization defines, and what each may do (ADR-034).
    path("roles/", role_views.RoleListView.as_view(), name="role_list"),
    path("roles/new/", role_views.RoleCreateView.as_view(), name="role_create"),
    path("roles/<int:pk>/", role_views.RoleUpdateView.as_view(), name="role_update"),
    path(
        "roles/<int:pk>/archive/",
        role_views.RoleLifecycleView.as_view(action="archive"),
        name="role_archive",
    ),
    path(
        "roles/<int:pk>/reactivate/",
        role_views.RoleLifecycleView.as_view(action="reactivate"),
        name="role_reactivate",
    ),
]
