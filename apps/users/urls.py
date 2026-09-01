"""User-facing authentication routes."""

from django.contrib.auth.decorators import login_required
from django.urls import path

from apps.users import views

app_name = "users"

urlpatterns = [
    path("", login_required(views.HomeView.as_view()), name="home"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("settings/users/", views.UserListView.as_view(), name="user_list"),
    path("settings/users/new/", views.UserCreateView.as_view(), name="user_create"),
    path("settings/users/<int:pk>/", views.UserUpdateView.as_view(), name="user_update"),
    # The manager assigns posts here, on the employee's own file, and the
    # change applies immediately. This replaced the request-and-approve
    # ceremony that used to live at settings/access/.
    path(
        "settings/users/<int:pk>/access/",
        views.UserAccessView.as_view(),
        name="user_access",
    ),
]
