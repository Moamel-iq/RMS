"""Organization settings routes."""

from django.urls import path

from apps.organizations import views

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
]
