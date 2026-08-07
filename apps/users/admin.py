"""Admin registration for the custom user model."""

from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.translation import gettext_lazy as _

from apps.users.forms import UserAdminChangeForm, UserAdminCreationForm
from apps.users.models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):  # generic in stubs only; see apps/users/forms.py
    """Django's UserAdmin with the phone field surfaced and normalised."""

    model = User
    form = UserAdminChangeForm
    add_form = UserAdminCreationForm

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name", "email", "phone")}),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "phone", "password1", "password2"),
            },
        ),
    )

    list_display = ("username", "phone", "first_name", "last_name", "is_active", "is_staff")
    list_filter = ("is_active", "is_staff", "is_superuser", "groups")
    search_fields = ("username", "phone", "first_name", "last_name", "email")
    ordering = ("username",)
