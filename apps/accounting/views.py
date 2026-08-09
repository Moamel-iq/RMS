"""
Accounting screens, inside the Khan Mandi shell — the first two of them.

Task 1.3 delivers the account-mapping surface only: the role vocabulary
(read-only) and the organization default mappings (managed). The rest of the
accounting module's screens arrive with Phase 5, and their sections stay
visibly inert in the navigation until they do.

Same discipline as the inventory screens: no view calls `form.save()`, every
mutation goes through `apps/accounting/commands.py`, and hiding a button is
presentation, never protection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import ListView

from apps.accounting.commands import (
    archive_account_role_mapping,
    close_account_role_mapping,
    list_account_roles,
    map_account_role,
)
from apps.accounting.forms import AccountMappingForm, CloseMappingForm, manageable_organizations
from apps.accounting.models import OrganizationAccountMapping
from apps.accounting.permissions import MANAGE_ACCOUNT_MAPPINGS, VIEW_JOURNAL
from apps.core.views import ModuleViewMixin
from apps.users.models import User

if TYPE_CHECKING:
    _ListView = ListView[Any]
else:
    _ListView = ListView


class AccountingViewMixin(LoginRequiredMixin, UserPassesTestMixin, ModuleViewMixin):
    """Signed in, holding the permission the screen needs; 404 for foreign scope."""

    module_key = "accounting"
    required_permission: str = VIEW_JOURNAL
    request: HttpRequest

    def test_func(self) -> bool:
        user = self.request.user
        return bool(user.is_authenticated and user.has_perm(self.required_permission))

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().dispatch(request, *args, **kwargs)
        except ObjectDoesNotExist as missing:
            raise Http404(str(missing)) from missing

    @property
    def actor(self) -> User:
        user: User = self.request.user  # type: ignore[assignment]
        return user


class AccountRoleListView(AccountingViewMixin, _ListView):
    """The system vocabulary. There is nothing to edit and no button offering to."""

    template_name = "accounting/role_list.html"
    context_object_name = "roles"

    def get_queryset(self) -> Any:
        return list_account_roles(actor=self.actor)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("الأدوار المحاسبية")
        context["page_hint"] = _(
            "مفردات النظام: قواعد الترحيل تشير إلى الدور، والمؤسسة تقرر أي حساب يحمله. "
            "الأدوار محمية ولا تُعدَّل."
        )
        return context


class AccountMappingListView(AccountingViewMixin, _ListView):
    """Every mapping in the organizations this caller manages, newest version first."""

    template_name = "accounting/mapping_list.html"
    context_object_name = "mappings"
    required_permission = MANAGE_ACCOUNT_MAPPINGS

    def get_queryset(self) -> Any:
        return (
            OrganizationAccountMapping.objects.filter(
                organization__in=manageable_organizations(self.actor)
            )
            .select_related("organization", "account_role", "account")
            .order_by("organization__code", "account_role__code", "-version")
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("ربط الحسابات")
        context["page_hint"] = _(
            "أي حساب يحمل كل دور، ومن أي تاريخ. الربط المستعمَل لا يُعدَّل — "
            "يُغلق نطاقه ويُنشأ إصدار جديد."
        )
        context["create_url"] = reverse("accounting:mapping_create")
        context["create_label"] = _("ربط جديد")
        return context


class AccountMappingCreateView(AccountingViewMixin, View):
    template_name = "accounting/mapping_form.html"
    required_permission = MANAGE_ACCOUNT_MAPPINGS

    def _render(self, request: HttpRequest, form: AccountMappingForm) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "page_title": _("ربط دور بحساب"),
                "page_hint": _("الحساب يجب أن يكون حساباً تفصيلياً فعّالاً في المؤسسة نفسها."),
                "cancel_url": reverse("accounting:mapping_list"),
            },
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._render(request, AccountMappingForm(actor=self.actor))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = AccountMappingForm(data=request.POST, actor=self.actor)
        if form.is_valid():
            try:
                map_account_role(
                    actor=self.actor,
                    organization_id=form.cleaned_data["organization"].pk,
                    account_role_id=form.cleaned_data["account_role"].pk,
                    account_id=form.cleaned_data["account"].pk,
                    effective_from=form.cleaned_data["effective_from"],
                    effective_to=form.cleaned_data["effective_to"],
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تم الربط."))
                return HttpResponseRedirect(reverse("accounting:mapping_list"))
        return self._render(request, form)


class AccountMappingCloseView(AccountingViewMixin, View):
    """POST-only: end a mapping's range, with a stated reason."""

    template_name = "accounting/mapping_close.html"
    required_permission = MANAGE_ACCOUNT_MAPPINGS

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "form": CloseMappingForm(),
                "page_title": _("إغلاق نطاق الربط"),
                "cancel_url": reverse("accounting:mapping_list"),
            },
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = CloseMappingForm(data=request.POST)
        if form.is_valid():
            try:
                close_account_role_mapping(
                    actor=self.actor,
                    mapping_id=self.kwargs["pk"],
                    effective_to=form.cleaned_data["effective_to"],
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as error:
                messages.error(request, "؛ ".join(str(message) for message in error.messages))
            else:
                messages.success(request, _("أُغلق نطاق الربط."))
            return HttpResponseRedirect(reverse("accounting:mapping_list"))
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "page_title": _("إغلاق نطاق الربط"),
                "cancel_url": reverse("accounting:mapping_list"),
            },
        )


class AccountMappingArchiveView(AccountingViewMixin, View):
    """POST-only: withdraw an unused mapping recorded in error."""

    required_permission = MANAGE_ACCOUNT_MAPPINGS

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            archive_account_role_mapping(
                actor=self.actor,
                mapping_id=self.kwargs["pk"],
                reason=request.POST.get("reason", ""),
            )
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        else:
            messages.success(request, _("أُرشف الربط."))
        return HttpResponseRedirect(reverse("accounting:mapping_list"))
