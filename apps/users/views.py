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
from django.core.exceptions import ValidationError
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import CreateView, TemplateView, UpdateView

from apps.core.build_status import BUILD_ITEMS, PHASE_LABEL
from apps.core.views import FoundationFormViewMixin, FoundationListView, ModuleViewMixin
from apps.organizations.security_permissions import MANAGE_USERS
from apps.users.forms import LoginForm, UserAccountCreateForm, UserAccountUpdateForm
from apps.users.home_dashboard import home_overview, readiness_share
from apps.users.models import User
from apps.users.services import create_user_account, update_user_account

#: Fragment swapped into the page by htmx when the form is redisplayed.
LOGIN_PARTIAL = "partials/login_form.html"


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _actor(request: HttpRequest) -> User:
    """The signed-in caller. `test_func` has already refused anonymity."""
    user: User = request.user  # type: ignore[assignment]
    return user


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
    required_permission = MANAGE_USERS

    def get_queryset(self) -> QuerySet[User]:
        queryset = super().get_queryset()
        if not self.request.user.is_superuser:
            organizations = self.authorized_organizations()
            queryset = queryset.filter(
                Q(organization_memberships__organization__in=organizations)
                | Q(branch_memberships__branch__organization__in=organizations)
            ).distinct()
        return queryset.exclude(is_staff=True).exclude(is_superuser=True).order_by("username")


class UserCreateView(FoundationFormViewMixin, CreateView):
    model = User
    form_class = UserAccountCreateForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("users:user_list")
    required_permission = MANAGE_USERS

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["actor"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("مستخدم جديد")
        context["page_hint"] = _("الحسابات ينشئها المسؤول؛ لا يوجد تسجيل ذاتي.")
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: UserAccountCreateForm) -> HttpResponse:
        try:
            create_user_account(
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password1"],
                phone=form.cleaned_data.get("phone"),
                first_name=form.cleaned_data.get("first_name", ""),
                last_name=form.cleaned_data.get("last_name", ""),
                organization=form.cleaned_data["organization"],
                actor=_actor(self.request),
            )
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
            return self.form_invalid(form)
        return HttpResponseRedirect(self.get_success_url())


class UserUpdateView(FoundationFormViewMixin, UpdateView):
    model = User
    form_class = UserAccountUpdateForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("users:user_list")
    required_permission = MANAGE_USERS

    def get_queryset(self) -> QuerySet[User]:
        queryset = User.objects.exclude(is_staff=True).exclude(is_superuser=True)
        if not self.request.user.is_superuser:
            organizations = self.authorized_organizations()
            queryset = queryset.filter(
                Q(organization_memberships__organization__in=organizations)
                | Q(branch_memberships__branch__organization__in=organizations)
            ).exclude(pk=_actor(self.request).pk)
        return queryset.distinct()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("تعديل المستخدم") + f" — {self.object.username}"
        context["page_hint"] = _("اسم المستخدم غير قابل للتعديل لأنه يظهر في سجل التدقيق.")
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: UserAccountUpdateForm) -> HttpResponse:
        try:
            update_user_account(
                user=self.object,
                phone=form.cleaned_data.get("phone"),
                first_name=form.cleaned_data.get("first_name", ""),
                last_name=form.cleaned_data.get("last_name", ""),
                is_active=form.cleaned_data["is_active"],
                actor=_actor(self.request),
            )
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
            return self.form_invalid(form)
        return HttpResponseRedirect(self.get_success_url())


class HomeView(ModuleViewMixin, TemplateView):
    """
    Landing page: one dashboard composed from every module's own overview.

    The view decides nothing about the figures. Each module's read function is
    called with the permissions the caller actually holds, so this page can
    never show a number that module's own screen would hide.
    """

    template_name = "home.html"
    module_key = "home"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            overview = home_overview(self.request.user)
            context["home"] = overview
            context["readiness_share"] = readiness_share(overview.readiness)
        # Development visibility only; removed when Phase 0 exits.
        context["build_items"] = BUILD_ITEMS
        context["build_phase_label"] = PHASE_LABEL
        return context
