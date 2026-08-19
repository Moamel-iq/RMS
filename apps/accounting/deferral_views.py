"""
المستحقات والمقدمات — one landing screen with two tabs, and a page per document.

Accruals and prepayments share a sidebar entry because an accountant reaches
for them together at month end, and they are separate documents underneath
because they answer opposite questions: one recognises a cost before the
paperwork, the other releases a payment after it.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounting.deferral_forms import AccrualForm, AccrualLineForm, PrepaymentForm
from apps.accounting.deferral_services import (
    add_accrual_line,
    approve_accrual,
    approve_prepayment,
    build_schedule,
    post_accrual,
    post_prepayment,
    post_schedule_line,
    remove_accrual_line,
    reverse_accrual,
    reverse_schedule_line,
)
from apps.accounting.models import (
    AccrualDocument,
    AccrualLine,
    FinancialDocumentStatus,
    Prepayment,
    PrepaymentScheduleLine,
    ScheduleLineStatus,
)
from apps.accounting.permissions import MANAGE_ACCRUALS, MANAGE_PREPAYMENTS
from apps.accounting.views import (
    AccountingDetailView,
    AccountingViewMixin,
    AccountingWriteView,
)
from apps.core.models import AuditEvent
from apps.organizations.authorization import (
    OutOfScope,
    has_organization_permission,
    organizations_with_permission,
    require_organization_permission,
)


def visible_accruals(actor: Any) -> QuerySet[AccrualDocument]:
    return AccrualDocument.objects.filter(
        organization__in=organizations_with_permission(actor, MANAGE_ACCRUALS)
    ).select_related("organization", "branch", "created_by", "approved_by")


def visible_prepayments(actor: Any) -> QuerySet[Prepayment]:
    return Prepayment.objects.filter(
        organization__in=organizations_with_permission(actor, MANAGE_PREPAYMENTS)
    ).select_related("organization", "branch", "expense_account", "prepaid_account")


class DeferralLandingView(AccountingViewMixin, View):
    """
    المستحقات والمقدمات — both registers on one page.

    Filtered by `tab` so the htmx swap can replace one table without
    re-computing the other, and so a bookmark keeps the tab it was on.
    """

    required_permission = MANAGE_ACCRUALS
    template_name = "accounting/deferral_landing.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        tab = request.GET.get("tab", "accruals")
        status = request.GET.get("status", "").strip()

        accruals = visible_accruals(self.actor)
        prepayments = visible_prepayments(self.actor)
        if status in FinancialDocumentStatus.values:
            accruals = accruals.filter(status=status)
            prepayments = prepayments.filter(status=status)

        due = request.GET.get("due", "").strip()
        schedule_rows = PrepaymentScheduleLine.objects.filter(
            prepayment__in=visible_prepayments(self.actor)
        ).select_related("prepayment")
        if due == "due":
            schedule_rows = schedule_rows.filter(
                status=ScheduleLineStatus.PLANNED, period_end__lte=timezone.localdate()
            )
        elif due == "planned":
            schedule_rows = schedule_rows.filter(status=ScheduleLineStatus.PLANNED)
        elif due == "posted":
            schedule_rows = schedule_rows.filter(status=ScheduleLineStatus.POSTED)

        return render(
            request,
            self.template_name,
            {
                "tab": tab,
                "accruals": accruals.order_by("-business_date", "-id")[:100],
                "prepayments": prepayments.order_by("-business_date", "-id")[:100],
                "schedule_rows": schedule_rows.order_by("period_end", "sequence")[:100],
                "statuses": FinancialDocumentStatus.choices,
                "selected_status": status,
                "selected_due": due,
                "today": timezone.localdate(),
                "may_manage_accruals": organizations_with_permission(
                    self.actor, MANAGE_ACCRUALS
                ).exists(),
                "may_manage_prepayments": organizations_with_permission(
                    self.actor, MANAGE_PREPAYMENTS
                ).exists(),
                "page_title": _("المستحقات والمقدمات"),
                "page_hint": _(
                    "المستحق يعترف بكلفة قبل وصول المستند؛ المقدَّم يوزّع دفعة على "
                    "فتراتها. كلاهما يحلّ حساباته بالأدوار، ولا يُكتب رقم حساب هنا."
                ),
                "list_base_template": (
                    "settings/_form_fragment.html"
                    if request.headers.get("HX-Request") == "true"
                    else "shell.html"
                ),
                "inventory_ui": False,
            },
        )


# ---------------------------------------------------------------------------
# Accruals
# ---------------------------------------------------------------------------


class AccrualCreateView(AccountingWriteView):
    form_class = AccrualForm
    required_permission = MANAGE_ACCRUALS
    success_url_name = "accounting:deferral_list"
    page_title = _("مستحق جديد")
    page_hint = _("الرأس فقط. تُضاف السطور من صفحة المستحق نفسه.")
    success_message = _("فُتح المستحق.")
    submit_label = _("فتح المستحق")

    def build_form(self, instance: Any, data: Any = None) -> Any:
        return self.form_class(data=data, actor=self.actor)

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor, MANAGE_ACCRUALS, form.cleaned_data["branch"].organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        branch = data["branch"]
        accrual = AccrualDocument(
            organization=branch.organization,
            branch=branch,
            business_date=data["business_date"],
            description=data["description"],
            reason=data.get("reason", ""),
            auto_reverse_on=data.get("auto_reverse_on"),
            evidence_reference=data.get("evidence_reference", ""),
            created_by=self.actor,
        )
        accrual.full_clean()
        accrual.save()
        self.created = accrual

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is not None:
            return reverse("accounting:accrual_detail", args=[created.pk])
        return reverse(self.success_url_name)


class AccrualDetailView(AccountingDetailView):
    template_name = "accounting/accrual_detail.html"
    required_permission = MANAGE_ACCRUALS

    def accrual(self) -> AccrualDocument:
        row = visible_accruals(self.actor).filter(pk=self.kwargs["pk"]).first()
        if row is None:
            raise OutOfScope(_("Accrual does not exist."))
        return row

    def _context(self, accrual: AccrualDocument, **extra: Any) -> dict[str, Any]:
        may_manage = has_organization_permission(self.actor, MANAGE_ACCRUALS, accrual.organization)
        context: dict[str, Any] = {
            "accrual": accrual,
            "lines": list(
                accrual.lines.select_related("account", "cost_center").order_by("sequence")
            ),
            "may_edit": accrual.is_editable and may_manage,
            "may_approve": (
                accrual.status == FinancialDocumentStatus.DRAFT
                and may_manage
                and accrual.created_by_id != self.actor.pk
            ),
            "may_post": accrual.status == FinancialDocumentStatus.APPROVED and may_manage,
            "may_reverse": accrual.status == FinancialDocumentStatus.POSTED and may_manage,
            "is_own_draft": accrual.created_by_id == self.actor.pk,
            "line_form": AccrualLineForm(accrual=accrual)
            if (accrual.is_editable and may_manage)
            else None,
            "timeline": AuditEvent.objects.filter(
                target_type="accounting.AccrualDocument", target_id=str(accrual.pk)
            ).order_by("-occurred_at")[:20],
            "page_title": str(accrual),
            "page_hint": _(
                "وصول الفاتورة الحقيقية لا يُنشئها من هنا: تُربط ويُعكس المستحق، "
                "فيُعترف بالمصروف مرة واحدة."
            ),
        }
        context.update(extra)
        return context

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self.render_detail(request, self._context(self.accrual()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        accrual = self.accrual()
        require_organization_permission(self.actor, MANAGE_ACCRUALS, accrual.organization)
        form = AccrualLineForm(data=request.POST, accrual=accrual)
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_accrual_line(
                    accrual=accrual,
                    account=data["account"],
                    amount=data["amount"],
                    cost_center=data.get("cost_center"),
                    description=data.get("description", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("أُضيف السطر."))
                return HttpResponseRedirect(reverse("accounting:accrual_detail", args=[accrual.pk]))
        return self.render_detail(request, self._context(accrual, line_form=form))


class AccrualLineDeleteView(AccountingViewMixin, View):
    required_permission = MANAGE_ACCRUALS

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        line = AccrualLine.objects.filter(pk=kwargs["pk"]).select_related("accrual").first()
        if line is None or not visible_accruals(self.actor).filter(pk=line.accrual_id).exists():
            raise OutOfScope(_("Accrual line does not exist."))
        require_organization_permission(self.actor, MANAGE_ACCRUALS, line.accrual.organization)
        accrual_id = line.accrual_id
        try:
            remove_accrual_line(line=line)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        else:
            messages.success(request, _("حُذف السطر."))
        return HttpResponseRedirect(reverse("accounting:accrual_detail", args=[accrual_id]))


class AccrualTransitionView(AccountingViewMixin, View):
    required_permission = MANAGE_ACCRUALS
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        accrual = visible_accruals(self.actor).filter(pk=kwargs["pk"]).first()
        if accrual is None:
            raise OutOfScope(_("Accrual does not exist."))
        require_organization_permission(self.actor, MANAGE_ACCRUALS, accrual.organization)
        reason = request.POST.get("reason", "").strip()
        try:
            if self.action == "approve":
                approve_accrual(accrual=accrual, approver=self.actor, reason=reason)
                messages.success(request, _("اعتُمد المستحق."))
            elif self.action == "post":
                post_accrual(accrual=accrual, poster=self.actor, reason=reason)
                messages.success(request, _("رُحّل المستحق."))
            elif self.action == "reverse":
                reverse_accrual(accrual=accrual, reason=reason or str(_("عكس المستحق")))
                messages.success(request, _("عُكس المستحق."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(reverse("accounting:accrual_detail", args=[accrual.pk]))


# ---------------------------------------------------------------------------
# Prepayments
# ---------------------------------------------------------------------------


class PrepaymentCreateView(AccountingWriteView):
    form_class = PrepaymentForm
    required_permission = MANAGE_PREPAYMENTS
    success_url_name = "accounting:deferral_list"
    page_title = _("مقدَّم جديد")
    page_hint = _(
        "الجدول يُبنى من المبلغ وعدد الفترات بالمخصِّص المعتمد، لا بقسمة يدوية — "
        "القسمة تترك كسراً لا يخرج من حساب المقدَّم أبداً."
    )
    success_message = _("فُتح المقدَّم وبُني جدوله.")
    submit_label = _("فتح المقدَّم")

    def build_form(self, instance: Any, data: Any = None) -> Any:
        return self.form_class(data=data, actor=self.actor)

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor, MANAGE_PREPAYMENTS, form.cleaned_data["branch"].organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        branch = data["branch"]
        periods = int(data["period_count"])
        months = 1 if data["frequency"] == "MONTHLY" else 3
        start = data["start_date"]
        end_month = start.month - 1 + months * periods
        end_year = start.year + end_month // 12
        end = datetime.date(end_year, end_month % 12 + 1, 1) - datetime.timedelta(days=1)

        prepayment = Prepayment(
            organization=branch.organization,
            branch=branch,
            business_date=data["business_date"],
            description=data["description"],
            total_amount=data["total_amount"],
            start_date=start,
            end_date=end,
            frequency=data["frequency"],
            period_count=periods,
            expense_account=data["expense_account"],
            prepaid_account=data["prepaid_account"],
            cost_center=data.get("cost_center"),
            payment_source=data["payment_source"],
            cashbox=data.get("cashbox"),
            bank_account=data.get("bank_account"),
            source_reference=data.get("source_reference", ""),
            created_by=self.actor,
        )
        prepayment.full_clean()
        prepayment.save()
        build_schedule(prepayment=prepayment)
        self.created = prepayment

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is not None:
            return reverse("accounting:prepayment_detail", args=[created.pk])
        return reverse(self.success_url_name)


class PrepaymentDetailView(AccountingDetailView):
    template_name = "accounting/prepayment_detail.html"
    required_permission = MANAGE_PREPAYMENTS

    def prepayment(self) -> Prepayment:
        row = visible_prepayments(self.actor).filter(pk=self.kwargs["pk"]).first()
        if row is None:
            raise OutOfScope(_("Prepayment does not exist."))
        return row

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        prepayment = self.prepayment()
        may_manage = has_organization_permission(
            self.actor, MANAGE_PREPAYMENTS, prepayment.organization
        )
        lines = list(prepayment.schedule_lines.order_by("sequence"))
        scheduled = sum((line.amount for line in lines), start=type(prepayment.total_amount)(0))
        return self.render_detail(
            request,
            {
                "prepayment": prepayment,
                "lines": lines,
                "scheduled_total": scheduled,
                # The equality this document exists to keep, shown rather than
                # assumed: if it ever fails, the page says so before anybody
                # tries to approve it.
                "schedule_matches": scheduled == prepayment.total_amount,
                "may_manage": may_manage,
                "may_approve": (
                    prepayment.status == FinancialDocumentStatus.DRAFT
                    and may_manage
                    and prepayment.created_by_id != self.actor.pk
                ),
                "may_post": prepayment.status == FinancialDocumentStatus.APPROVED and may_manage,
                "is_own_draft": prepayment.created_by_id == self.actor.pk,
                "today": timezone.localdate(),
                "page_title": str(prepayment),
                "page_hint": _(
                    "مجموع الجدول يساوي الإجمالي تماماً. السطر المُرحَّل لا يُعاد "
                    "تخطيطه، ولا يُرحَّل سطر داخل فترة مُقفلة."
                ),
            },
        )


class PrepaymentTransitionView(AccountingViewMixin, View):
    required_permission = MANAGE_PREPAYMENTS
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        prepayment = visible_prepayments(self.actor).filter(pk=kwargs["pk"]).first()
        if prepayment is None:
            raise OutOfScope(_("Prepayment does not exist."))
        require_organization_permission(self.actor, MANAGE_PREPAYMENTS, prepayment.organization)
        reason = request.POST.get("reason", "").strip()
        try:
            if self.action == "approve":
                approve_prepayment(prepayment=prepayment, approver=self.actor, reason=reason)
                messages.success(request, _("اعتُمد المقدَّم."))
            elif self.action == "post":
                post_prepayment(prepayment=prepayment, poster=self.actor, reason=reason)
                messages.success(request, _("رُحّل المقدَّم."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(reverse("accounting:prepayment_detail", args=[prepayment.pk]))


class ScheduleLineActionView(AccountingViewMixin, View):
    """POST-only: amortize one period, or reverse one that was amortized."""

    required_permission = MANAGE_PREPAYMENTS
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        line = (
            PrepaymentScheduleLine.objects.filter(pk=kwargs["pk"])
            .select_related("prepayment")
            .first()
        )
        if (
            line is None
            or not visible_prepayments(self.actor).filter(pk=line.prepayment_id).exists()
        ):
            raise OutOfScope(_("Schedule line does not exist."))
        require_organization_permission(
            self.actor, MANAGE_PREPAYMENTS, line.prepayment.organization
        )
        reason = request.POST.get("reason", "").strip()
        try:
            if self.action == "post":
                post_schedule_line(line=line, reason=reason)
                messages.success(request, _("رُحّل القسط."))
            elif self.action == "reverse":
                reverse_schedule_line(line=line, reason=reason or str(_("عكس القسط")))
                messages.success(request, _("عُكس القسط."))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(
            reverse("accounting:prepayment_detail", args=[line.prepayment_id])
        )


__all__ = [
    "AccrualCreateView",
    "AccrualDetailView",
    "AccrualLineDeleteView",
    "AccrualTransitionView",
    "DeferralLandingView",
    "PrepaymentCreateView",
    "PrepaymentDetailView",
    "PrepaymentTransitionView",
    "ScheduleLineActionView",
]
