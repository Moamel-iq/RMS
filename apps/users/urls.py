"""User-facing authentication routes."""

from django.contrib.auth.decorators import login_required
from django.urls import path

from apps.users import views

app_name = "users"

urlpatterns = [
    path("", login_required(views.HomeView.as_view()), name="home"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
]
