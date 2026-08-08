"""Core settings routes."""

from django.urls import path

from apps.core import views

app_name = "core"

urlpatterns = [
    path("audit/", views.AuditEventListView.as_view(), name="audit_list"),
]
