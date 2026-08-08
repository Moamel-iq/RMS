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
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import CreateView, TemplateView, UpdateView

from apps.core.build_status import BUILD_ITEMS, PHASE_LABEL
from apps.core.views import FoundationFormViewMixin, FoundationListView, ModuleViewMixin
from apps.users.forms import LoginForm, UserAccountCreateForm, UserAccountUpdateForm
from apps.users.models import User
from apps.users.services import create_user_account, update_user_account

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


class UserListView(FoundationListView):
    model = User
    template_name = "settings/user_list.html"
    context_object_name = "users"
    page_title = _("المستخدمون")
    page_hint = _("الحسابات ينشئها المسؤول. لا يوجد تسجيل ذاتي في نظام داخلي.")
    create_url_name = "users:user_create"
    create_label = _("مستخدم جديد")
    search_fields = ("username", "phone", "first_name", "last_name")

    def get_queryset(self) -> QuerySet[User]:
        return super().get_queryset().order_by("username")


class UserCreateView(FoundationFormViewMixin, CreateView):
    model = User
    form_class = UserAccountCreateForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("users:user_list")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("مستخدم جديد")
        context["page_hint"] = _("الحسابات ينشئها المسؤول؛ لا يوجد تسجيل ذاتي.")
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: UserAccountCreateForm) -> HttpResponse:
        create_user_account(
            username=form.cleaned_data["username"],
            password=form.cleaned_data["password1"],
            phone=form.cleaned_data.get("phone"),
            first_name=form.cleaned_data.get("first_name", ""),
            last_name=form.cleaned_data.get("last_name", ""),
            is_staff=form.cleaned_data.get("is_staff", False),
        )
        return HttpResponseRedirect(self.get_success_url())


class UserUpdateView(FoundationFormViewMixin, UpdateView):
    model = User
    form_class = UserAccountUpdateForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("users:user_list")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("تعديل المستخدم") + f" — {self.object.username}"
        context["page_hint"] = _("اسم المستخدم غير قابل للتعديل لأنه يظهر في سجل التدقيق.")
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: UserAccountUpdateForm) -> HttpResponse:
        update_user_account(
            user=self.object,
            phone=form.cleaned_data.get("phone"),
            first_name=form.cleaned_data.get("first_name", ""),
            last_name=form.cleaned_data.get("last_name", ""),
            is_active=form.cleaned_data["is_active"],
            is_staff=form.cleaned_data["is_staff"],
        )
        return HttpResponseRedirect(self.get_success_url())


class HomeView(ModuleViewMixin, TemplateView):
    """Landing page. Shows the shell and the branches the user may act on."""

    template_name = "home.html"
    module_key = "home"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # Development visibility only; removed when Phase 0 exits.
        context["build_items"] = BUILD_ITEMS
        context["build_phase_label"] = PHASE_LABEL
        return context
