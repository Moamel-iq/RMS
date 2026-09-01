"""
Insights routes.

Findings are addressed by `public_id` rather than by primary key: an id in a
URL that somebody pastes into a message is a small, permanent invitation to
guess the neighbouring one, and the selector's 404 is easier to trust when
there is nothing to enumerate.
"""

from django.urls import path

from apps.insights import views

app_name = "insights"

urlpatterns = [
    path("", views.InsightDashboardView.as_view(), name="dashboard"),
    path("<uuid:public_id>/", views.InsightDetailView.as_view(), name="detail"),
    # POST-only command routes, named for the act rather than the record.
    path(
        "<uuid:public_id>/acknowledge/",
        views.InsightLifecycleView.as_view(action="acknowledge"),
        name="acknowledge",
    ),
    path(
        "<uuid:public_id>/dismiss/",
        views.InsightLifecycleView.as_view(action="dismiss"),
        name="dismiss",
    ),
    path(
        "<uuid:public_id>/reopen/",
        views.InsightLifecycleView.as_view(action="reopen"),
        name="reopen",
    ),
]
