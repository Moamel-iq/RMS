"""Core settings routes."""

from django.urls import path

from apps.core import views

app_name = "core"

urlpatterns = [
    path("about/", views.AboutView.as_view(), name="about"),
    path("audit/", views.AuditEventListView.as_view(), name="audit_list"),
    path("tasks/", views.AutomationTaskInboxView.as_view(), name="task_inbox"),
    path(
        "tasks/<int:pk>/acknowledge/",
        views.AutomationTaskAcknowledgeView.as_view(),
        name="task_acknowledge",
    ),
    path(
        "automation/",
        views.AutomationMonitoringView.as_view(),
        name="automation_monitoring",
    ),
]
