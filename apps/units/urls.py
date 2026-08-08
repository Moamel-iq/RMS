"""Unit of measure settings routes."""

from django.urls import path

from apps.units import views

app_name = "units"

urlpatterns = [
    path("units/", views.UnitListView.as_view(), name="unit_list"),
    path("units/new/", views.UnitCreateView.as_view(), name="unit_create"),
    path("units/<int:pk>/", views.UnitUpdateView.as_view(), name="unit_update"),
]
