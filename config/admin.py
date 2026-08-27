"""Django Admin is a break-glass surface, never an ERP workspace."""

from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden


class SuperuserOnlyAdminSite(admin.AdminSite):
    """Keep Django Admin available only to explicit break-glass accounts."""

    def has_permission(self, request: HttpRequest) -> bool:
        user = request.user
        return bool(user.is_authenticated and user.is_active and user.is_superuser)

    def login(
        self, request: HttpRequest, extra_context: dict[str, object] | None = None
    ) -> HttpResponse:
        # Do not turn a logged-in ERP user into an admin-login loop.  More
        # importantly, make the denial unambiguous in access logs and tests.
        if request.user.is_authenticated and not self.has_permission(request):
            return HttpResponseForbidden("Django Admin is restricted to superusers.")
        return super().login(request, extra_context=extra_context)


# Registrations across the project use django.contrib.admin.site.  Changing
# the site's class preserves every registration while applying the gate once.
admin.site.__class__ = SuperuserOnlyAdminSite
site = admin.site
