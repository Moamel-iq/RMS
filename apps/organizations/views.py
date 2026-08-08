"""Organization and branch settings screens."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import CreateView, UpdateView

from apps.core.views import FoundationFormViewMixin, FoundationListView
from apps.organizations.forms import (
    BranchForm,
    BranchMembershipForm,
    OrganizationCreateForm,
    OrganizationUpdateForm,
)
from apps.organizations.models import Branch, BranchMembership, Organization
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    revoke_branch_access,
    update_branch,
    update_organization,
)


class OrganizationListView(FoundationListView):
    model = Organization
    template_name = "settings/organization_list.html"
    context_object_name = "organizations"
    page_title = _("المؤسسات")
    page_hint = _("الحد الأعلى للنشاط. كل فرع يتبع مؤسسة واحدة.")
    create_url_name = "organizations:organization_create"
    create_label = _("مؤسسة جديدة")
    search_fields = ("code", "name_ar", "name_en")


class OrganizationCreateView(FoundationFormViewMixin, CreateView):
    model = Organization
    form_class = OrganizationCreateForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("organizations:organization_list")

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
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
        )
        return HttpResponseRedirect(self.get_success_url())


class OrganizationUpdateView(FoundationFormViewMixin, UpdateView):
    model = Organization
    form_class = OrganizationUpdateForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("organizations:organization_list")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("تعديل المؤسسة") + f" — {self.object.code}"
        context["page_hint"] = _("الرمز غير قابل للتعديل لأنه يظهر في ترقيم المستندات.")
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: OrganizationUpdateForm) -> HttpResponse:
        update_organization(
            organization=self.object,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            is_active=form.cleaned_data["is_active"],
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
    search_fields = ("code", "name_ar", "name_en")

    def get_queryset(self) -> QuerySet[Branch]:
        return super().get_queryset().select_related("organization")


class BranchCreateView(FoundationFormViewMixin, CreateView):
    model = Branch
    form_class = BranchForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("organizations:branch_list")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("فرع جديد")
        context["cancel_url"] = self.success_url
        return context

    def form_valid(self, form: BranchForm) -> HttpResponse:
        create_branch(
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            business_day_start_time=form.cleaned_data["business_day_start_time"],
            timezone=form.cleaned_data["timezone"],
        )
        return HttpResponseRedirect(self.get_success_url())


class BranchUpdateView(FoundationFormViewMixin, UpdateView):
    model = Branch
    form_class = BranchForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("organizations:branch_list")

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
        update_branch(
            branch=self.object,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            business_day_start_time=form.cleaned_data["business_day_start_time"],
            timezone=form.cleaned_data["timezone"],
            is_active=form.cleaned_data["is_active"],
        )
        return HttpResponseRedirect(self.get_success_url())


class BranchAccessListView(FoundationListView):
    model = BranchMembership
    template_name = "settings/access_list.html"
    context_object_name = "memberships"
    page_title = _("صلاحيات الفروع")
    page_hint = _("من يصل إلى أي فرع وبأي دور. السحب لا يحذف السجل.")
    search_fields = ("user__username", "branch__code")

    def get_queryset(self) -> QuerySet[BranchMembership]:
        return super().get_queryset().select_related("user", "branch")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["form"] = BranchMembershipForm()
        return context

    def post(self, request: Any, *args: Any, **kwargs: Any) -> HttpResponse:
        """Grant or revoke, both through the services that audit them."""
        from django.shortcuts import redirect

        if "revoke" in request.POST:
            membership = BranchMembership.objects.select_related("user", "branch").get(
                pk=request.POST["revoke"]
            )
            revoke_branch_access(user=membership.user, branch=membership.branch)
            return redirect(reverse("organizations:access_list"))

        form = BranchMembershipForm(request.POST)
        if form.is_valid():
            grant_branch_access(
                user=form.cleaned_data["user"],
                branch=form.cleaned_data["branch"],
                role=form.cleaned_data["role"],
            )
            return redirect(reverse("organizations:access_list"))

        self.object_list = self.get_queryset()
        context = self.get_context_data()
        context["form"] = form
        return self.render_to_response(context)
