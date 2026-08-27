"""Organization and branch settings screens."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, FormView, UpdateView

from apps.core.views import FoundationFormViewMixin, FoundationListView, FoundationViewMixin
from apps.organizations.forms import (
    AccessChangeRequestForm,
    BranchForm,
    OrganizationCreateForm,
    OrganizationUpdateForm,
)
from apps.organizations.models import (
    AccessChangeRequest,
    AccessChangeRequestStatus,
    Branch,
    BranchMembership,
    Organization,
)
from apps.organizations.security_permissions import (
    MANAGE_ACCESS,
    MANAGE_ORG_SETTINGS,
)
from apps.organizations.services import (
    cancel_access_change,
    create_branch,
    create_organization,
    decide_access_change,
    request_access_change,
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
    search_fields = ("code", "name_ar", "name_en")
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
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
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
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
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
    search_fields = ("code", "name_ar", "name_en")
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
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
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
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            business_day_start_time=form.cleaned_data["business_day_start_time"],
            timezone=form.cleaned_data["timezone"],
            is_active=form.cleaned_data["is_active"],
            actor=actor,
        )
        return HttpResponseRedirect(self.get_success_url())


class BranchAccessListView(FoundationListView):
    model = BranchMembership
    template_name = "settings/access_list.html"
    context_object_name = "memberships"
    page_title = _("الصلاحيات الحالية")
    page_hint = _("لا يتغير الوصول مباشرة: أنشئ طلباً، ثم يعتمد شخص مستقل الطلب.")
    create_url_name = "organizations:access_request_create"
    create_label = _("طلب تغيير صلاحية")
    search_fields = ("user__username", "branch__code")
    required_permission = MANAGE_ACCESS

    def get_queryset(self) -> QuerySet[BranchMembership]:
        return (
            super()
            .get_queryset()
            .filter(branch__organization__in=self.authorized_organizations())
            .select_related("user", "branch", "branch__organization")
        )


class AccessChangeRequestListView(FoundationListView):
    model = AccessChangeRequest
    template_name = "settings/access_request_list.html"
    context_object_name = "access_requests"
    page_title = _("طلبات تغيير الصلاحيات")
    page_hint = _("المنشئ والمستفيد لا يستطيعان اعتماد الطلب؛ يبقى القرار وسجل سببه محفوظين.")
    create_url_name = "organizations:access_request_create"
    create_label = _("طلب جديد")
    search_fields = (
        "target_user__username",
        "requested_by__username",
        "organization__code",
        "reason",
    )
    required_permission = MANAGE_ACCESS

    def get_queryset(self) -> QuerySet[AccessChangeRequest]:
        return (
            super()
            .get_queryset()
            .filter(organization__in=self.authorized_organizations())
            .select_related("organization", "branch", "target_user", "requested_by", "reviewed_by")
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["pending_status"] = AccessChangeRequestStatus.PENDING
        return context


class AccessChangeRequestCreateView(FoundationFormViewMixin, FormView):
    form_class = AccessChangeRequestForm
    template_name = "settings/base_form.html"
    success_url = reverse_lazy("organizations:access_request_list")
    required_permission = MANAGE_ACCESS

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["actor"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = _("طلب تغيير صلاحية")
        context["page_hint"] = _("لا يُمنح الدور هنا. يراجعه مستخدم آخر مخوّل قبل أن يصبح فعالاً.")
        context["cancel_url"] = reverse("organizations:access_request_list")
        return context

    def form_valid(self, form: AccessChangeRequestForm) -> HttpResponse:
        actor: User = self.request.user  # type: ignore[assignment]
        try:
            request_access_change(
                actor=actor,
                target_user=form.cleaned_data["target_user"],
                organization=form.cleaned_data["organization"],
                branch=form.cleaned_data["branch"],
                action=form.cleaned_data["action"],
                requested_role=form.cleaned_data["requested_role"],
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
            return self.form_invalid(form)
        return HttpResponseRedirect(self.get_success_url())


class AccessChangeRequestDecisionView(FoundationViewMixin, View):
    """Approve, reject, or cancel a request through the audited service."""

    required_permission = MANAGE_ACCESS
    decision = ""

    def post(self, request: Any, *args: Any, **kwargs: Any) -> HttpResponse:
        from django.contrib import messages
        from django.shortcuts import get_object_or_404, redirect

        access_request = get_object_or_404(
            AccessChangeRequest.objects.filter(organization__in=self.authorized_organizations()),
            pk=self.kwargs["pk"],
        )
        try:
            if self.decision == "cancel":
                cancel_access_change(
                    request=access_request,
                    actor=request.user,
                    reason=request.POST.get("reason", ""),
                )
                message = _("أُلغي طلب تغيير الصلاحية.")
            else:
                approved = self.decision == "approve"
                decide_access_change(
                    request=access_request,
                    actor=request.user,
                    approve=approved,
                    reason=request.POST.get("reason", ""),
                )
                message = (
                    _("اعتمد طلب تغيير الصلاحية.") if approved else _("رُفض طلب تغيير الصلاحية.")
                )
        except ValidationError as error:
            messages.error(request, "؛ ".join(error.messages))
        else:
            messages.success(request, message)
        return redirect(reverse("organizations:access_request_list"))
