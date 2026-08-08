"""
Authentication views.

Thin by design: Django's auth machinery does the work. The only addition is
htmx handling, so a failed sign-in re-renders the form fragment instead of the
whole page, and a successful one triggers a real browser navigation.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView as DjangoLoginView
from django.contrib.auth.views import LogoutView as DjangoLogoutView
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.generic import TemplateView

from apps.core.views import ModuleViewMixin
from apps.users.forms import LoginForm

#: Fragment swapped into the page by htmx when the form is redisplayed.
LOGIN_PARTIAL = "partials/login_form.html"


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


class LoginView(DjangoLoginView):
    template_name = "registration/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # The partial is rendered both standalone and inside the full page, so
        # it cannot rely on the page's URL for its action.
        context["login_action"] = reverse_lazy("users:login")
        return context

    def form_valid(self, form: AuthenticationForm) -> HttpResponse:
        response = super().form_valid(form)
        if _is_htmx(self.request):
            # htmx would swap the redirect target into the form element.
            # HX-Redirect makes the browser navigate for real instead.
            hx_response = HttpResponse(status=204)
            hx_response["HX-Redirect"] = self.get_success_url()
            return hx_response
        return response

    def form_invalid(self, form: AuthenticationForm) -> HttpResponse:
        if _is_htmx(self.request):
            # 200, not 400: htmx does not swap error responses by default, and
            # the whole point here is to show the user their error.
            return render(self.request, LOGIN_PARTIAL, self.get_context_data(form=form))
        return super().form_invalid(form)


class LogoutView(DjangoLogoutView):
    """
    POST-only, as Django requires since 5.0. A GET must not end a session:
    it would be triggerable from an <img> tag on any page.

    The destination comes from settings.LOGOUT_REDIRECT_URL.
    """


class HomeView(ModuleViewMixin, TemplateView):
    """Landing page. Shows the shell and the branches the user may act on."""

    template_name = "home.html"
    module_key = "home"
