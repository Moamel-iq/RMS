"""Organization and branch settings screens."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import CreateView, UpdateView

from apps.core.views import FoundationFormViewMixin, FoundationListView
from apps.organizations.forms import (
    BranchForm,
    OrganizationCreateForm,
    OrganizationUpdateForm,
)
from apps.organizations.models import (
    Branch,
    Organization,
)
from apps.organizations.security_permissions import (
    MANAGE_ORG_SETTINGS,
)
from apps.organizations.services import (
    create_branch,
    create_organization,
    update_branch,
    update_organization,
)
from apps.users.models import User


class OrganizationListView(FoundationListView):
    model = Organization
    template_name = "settings/organization_list.html"
    context_object_name = "organizations"
    page_title = _("المؤسسات")
    page_hint = _("الحد الأعلى للنشاط. كل فرع يتبع مؤسسة واحدة.")
    create_url_name = "organizations:organization_create"
    create_label = _("مؤسسة جديدة")
    search_fields = ("code", "name")
    required_permission = MANAGE_ORG_SETTINGS

    def get_queryset(self) -> QuerySet[Organization]:
        return super().get_queryset().filter(pk__in=self.authorized_organizations())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # Opening a new legal/tenant boundary is a break-glass operation, not
        # part of an owner's day-to-day organization configuration.
        if not self.request.user.is_superuser:
            context["create_url"] = None
        return context


class OrganizationCreateView(FoundationFormViewMixin, CreateView):
    model = Organization
    form_class = OrganizationCreateForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("organizations:organization_list")
    superuser_only = True

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("مؤسسة جديدة")
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: OrganizationCreateForm) -> HttpResponse:
        # Through the service, so the audit event is written and the rules are
        # applied in the one place that owns them.
        create_organization(
            code=form.cleaned_data["code"],
            name=form.cleaned_data["name"],
        )
        return HttpResponseRedirect(self.get_success_url())


class OrganizationUpdateView(FoundationFormViewMixin, UpdateView):
    model = Organization
    form_class = OrganizationUpdateForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("organizations:organization_list")
    required_permission = MANAGE_ORG_SETTINGS

    def get_queryset(self) -> QuerySet[Organization]:
        return Organization.objects.filter(pk__in=self.authorized_organizations())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("تعديل المؤسسة") + f" — {self.object.code}"
        context["page_hint"] = _("الرمز غير قابل للتعديل لأنه يظهر في ترقيم المستندات.")
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: OrganizationUpdateForm) -> HttpResponse:
        # `test_func` has already refused anonymous and inactive callers, but
        # the stubs still type the request user as possibly anonymous.
        actor: User = self.request.user  # type: ignore[assignment]
        update_organization(
            organization=self.object,
            name=form.cleaned_data["name"],
            is_active=form.cleaned_data["is_active"],
            actor=actor,
        )
        return HttpResponseRedirect(self.get_success_url())


class BranchListView(FoundationListView):
    model = Branch
    template_name = "settings/branch_list.html"
    context_object_name = "branches"
    page_title = _("الفروع")
    page_hint = _("لكل فرع منطقته الزمنية وبداية يوم عمله، وعليهما يُبنى تاريخ العمل.")
    create_url_name = "organizations:branch_create"
    create_label = _("فرع جديد")
    search_fields = ("code", "name")
    required_permission = MANAGE_ORG_SETTINGS

    def get_queryset(self) -> QuerySet[Branch]:
        return (
            super()
            .get_queryset()
            .filter(organization__in=self.authorized_organizations())
            .select_related("organization")
        )


class BranchCreateView(FoundationFormViewMixin, CreateView):
    model = Branch
    form_class = BranchForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("organizations:branch_list")
    required_permission = MANAGE_ORG_SETTINGS

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["actor"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("فرع جديد")
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: BranchForm) -> HttpResponse:
        actor: User = self.request.user  # type: ignore[assignment]
        create_branch(
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name=form.cleaned_data["name"],
            business_day_start_time=form.cleaned_data["business_day_start_time"],
            timezone=form.cleaned_data["timezone"],
            actor=actor,
        )
        return HttpResponseRedirect(self.get_success_url())


class BranchUpdateView(FoundationFormViewMixin, UpdateView):
    model = Branch
    form_class = BranchForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("organizations:branch_list")
    required_permission = MANAGE_ORG_SETTINGS

    def get_queryset(self) -> QuerySet[Branch]:
        return Branch.objects.filter(organization__in=self.authorized_organizations())

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["actor"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("تعديل الفرع") + f" — {self.object.code}"
        context["page_hint"] = _(
            "تغيير المنطقة الزمنية أو بداية يوم العمل يعيد تحديد تاريخ العمل لكل ما سُجّل "
            "على هذا الفرع."
        )
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: BranchForm) -> HttpResponse:
        actor: User = self.request.user  # type: ignore[assignment]
        update_branch(
            branch=self.object,
            name=form.cleaned_data["name"],
            business_day_start_time=form.cleaned_data["business_day_start_time"],
            timezone=form.cleaned_data["timezone"],
            is_active=form.cleaned_data["is_active"],
            actor=actor,
        )
        return HttpResponseRedirect(self.get_success_url())
