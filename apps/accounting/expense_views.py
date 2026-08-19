"""
المصروفات — the expense-voucher screens.

The line editor is an inline sub-form on the detail page, the same shape the
journal and the sales day use: a line that can fail validation needs somewhere
to say why, and a grid that accepted twenty rows and rejected the eighteenth
would lose the other nineteen.

Approval and posting are separate buttons behind separate permissions, and the
service refuses a self-approval whoever holds them. Hiding the button is
presentation; the refusal is the control.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounting.expense_forms import ExpenseLineForm, ExpenseVoucherForm
from apps.accounting.expense_services import (
    add_expense_line,
    approve_expense_voucher,
    discard_expense_voucher,
    open_expense_voucher,
    post_expense_voucher,
    remove_expense_line,
    reverse_expense_voucher,
)
from apps.accounting.models import (
    CostCenter,
    ExpenseVoucher,
    ExpenseVoucherLine,
    FinancialDocumentStatus,
)
from apps.accounting.permissions import (
    APPROVE_EXPENSE_VOUCHERS,
    MANAGE_EXPENSE_VOUCHERS,
)
from apps.accounting.views import (
    AccountingDetailView,
    AccountingListView,
    AccountingViewMixin,
    AccountingWriteView,
)
from apps.core.models import AuditEvent
from apps.organizations.authorization import (
    OutOfScope,
    has_branch_permission,
    has_organization_permission,
    require_branch_permission,
    require_organization_permission,
)
from apps.organizations.selectors import accessible_branches


def visible_vouchers(actor: Any) -> QuerySet[ExpenseVoucher]:
    """Every voucher at a branch this caller reaches."""
    return ExpenseVoucher.objects.filter(branch__in=accessible_branches(actor)).select_related(
        "organization", "branch", "cashbox", "bank_account", "created_by", "approved_by"
    )


class ExpenseVoucherListView(AccountingListView):
    template_name = "accounting/expense_list.html"
    context_object_name = "vouchers"
    required_permission = MANAGE_EXPENSE_VOUCHERS
    page_title = _("المصروفات")
    page_hint = _(
        "مصروف تشغيلي غير مورّد، مدفوع فوراً. فواتير الموردين ومشتريات المخزون "
        "وتسويات التطبيقات والرواتب لكلٍّ منها مستنده — ولا ضريبة هنا."
    )
    search_fields = ("number", "beneficiary", "reason")
    search_placeholder = _("ابحث بالرقم أو المستفيد…")
    result_label = _("سند")
    create_url_name = "accounting:expense_create"
    create_label = _("سند مصروف جديد")
    manage_permission = MANAGE_EXPENSE_VOUCHERS
    manage_scope = "branch"

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_vouchers(self.actor)
        get = self.request.GET

        branch = get.get("branch", "").strip()
        if branch.isdigit():
            queryset = queryset.filter(branch_id=int(branch))

        status = get.get("status", "").strip()
        if status in FinancialDocumentStatus.values:
            queryset = queryset.filter(status=status)

        account = get.get("account", "").strip()
        if account.isdigit():
            queryset = queryset.filter(lines__account_id=int(account)).distinct()

        cost_center = get.get("cost_center", "").strip()
        if cost_center.isdigit():
            queryset = queryset.filter(lines__cost_center_id=int(cost_center)).distinct()

        source = get.get("source", "").strip()
        if source == "cashbox":
            queryset = queryset.filter(cashbox__isnull=False)
        elif source == "bank":
            queryset = queryset.filter(bank_account__isnull=False)

        for key, lookup in (("from", "gte"), ("to", "lte")):
            raw = get.get(key, "").strip()
            if raw:
                try:
                    parsed = datetime.date.fromisoformat(raw)
                except ValueError:
                    continue
                queryset = queryset.filter(**{f"business_date__{lookup}": parsed})

        return queryset.order_by("-business_date", "-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["branches"] = accessible_branches(self.actor).order_by("code")
        context["statuses"] = FinancialDocumentStatus.choices
        context["cost_centers"] = CostCenter.objects.filter(
            organization__in={branch.organization_id for branch in context["branches"]},
            is_active=True,
        ).order_by("code")
        for key in ("branch", "status", "account", "cost_center", "source", "from", "to"):
            context[f"selected_{key}"] = self.request.GET.get(key, "")
        return context


class ExpenseVoucherCreateView(AccountingWriteView):
    form_class = ExpenseVoucherForm
    required_permission = MANAGE_EXPENSE_VOUCHERS
    success_url_name = "accounting:expense_list"
    page_title = _("سند مصروف جديد")
    page_hint = _(
        "الرأس فقط. تُضاف السطور من صفحة السند نفسه، حتى يقول السطر الذي يُرفض "
        "سبب رفضه دون أن يُفقد ما قبله."
    )
    success_message = _("فُتح السند.")
    submit_label = _("فتح السند")

    def build_form(self, instance: Any, data: Any = None) -> Any:
        return self.form_class(data=data, actor=self.actor)

    def authorize(self, instance: Any, form: Any) -> None:
        require_branch_permission(self.actor, MANAGE_EXPENSE_VOUCHERS, form.cleaned_data["branch"])

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        self.created = open_expense_voucher(
            branch=data["branch"],
            business_date=data["business_date"],
            expense_date=data["expense_date"],
            cashbox=data.get("cashbox"),
            bank_account=data.get("bank_account"),
            beneficiary=data["beneficiary"],
            reason=data["reason"],
            evidence_reference=data.get("evidence_reference", ""),
            notes=data.get("notes", ""),
            created_by=self.actor,
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is not None:
            return reverse("accounting:expense_detail", args=[created.pk])
        return reverse(self.success_url_name)


class ExpenseVoucherDetailView(AccountingDetailView):
    """One voucher: its lines, its state, and whatever it may do next."""

    template_name = "accounting/expense_detail.html"
    required_permission = MANAGE_EXPENSE_VOUCHERS

    def voucher(self) -> ExpenseVoucher:
        row = visible_vouchers(self.actor).filter(pk=self.kwargs["pk"]).first()
        if row is None:
            raise OutOfScope(_("Expense voucher does not exist."))
        return row

    def _context(
        self, voucher: ExpenseVoucher, request: HttpRequest, **extra: Any
    ) -> dict[str, Any]:
        may_edit = voucher.is_editable and has_branch_permission(
            self.actor, MANAGE_EXPENSE_VOUCHERS, voucher.branch
        )
        may_release = has_organization_permission(
            self.actor, APPROVE_EXPENSE_VOUCHERS, voucher.organization
        )
        context: dict[str, Any] = {
            "voucher": voucher,
            "lines": list(
                voucher.lines.select_related("account", "cost_center").order_by("sequence")
            ),
            "may_edit": may_edit,
            # The button is hidden when the caller wrote it, and the service
            # refuses it as well — hiding is presentation, never protection.
            "may_approve": (
                voucher.status == FinancialDocumentStatus.DRAFT
                and may_release
                and voucher.created_by_id != self.actor.pk
            ),
            "may_post": (
                voucher.status == FinancialDocumentStatus.APPROVED
                and may_release
                and voucher.created_by_id != self.actor.pk
            ),
            "may_reverse": voucher.status == FinancialDocumentStatus.POSTED and may_release,
            "is_own_draft": voucher.created_by_id == self.actor.pk,
            "line_form": ExpenseLineForm(voucher=voucher) if may_edit else None,
            "timeline": AuditEvent.objects.filter(
                target_type="accounting.ExpenseVoucher", target_id=str(voucher.pk)
            )
            .select_related("actor")
            .order_by("-occurred_at")[:20],
            "page_title": str(voucher),
            "page_hint": _("السند المُرحَّل لا يُعدَّل. التصحيح عكسٌ ثم سند جديد."),
        }
        context.update(extra)
        return context

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self.render_detail(request, self._context(self.voucher(), request))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        voucher = self.voucher()
        require_branch_permission(self.actor, MANAGE_EXPENSE_VOUCHERS, voucher.branch)
        form = ExpenseLineForm(data=request.POST, voucher=voucher)
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_expense_line(
                    voucher=voucher,
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
                return HttpResponseRedirect(reverse("accounting:expense_detail", args=[voucher.pk]))
        return self.render_detail(request, self._context(voucher, request, line_form=form))


class ExpenseLineDeleteView(AccountingViewMixin, View):
    """POST-only: drop one line from a draft."""

    required_permission = MANAGE_EXPENSE_VOUCHERS

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        line = (
            ExpenseVoucherLine.objects.filter(pk=kwargs["pk"])
            .select_related("voucher", "voucher__branch")
            .first()
        )
        if line is None or not visible_vouchers(self.actor).filter(pk=line.voucher_id).exists():
            raise OutOfScope(_("Expense line does not exist."))
        require_branch_permission(self.actor, MANAGE_EXPENSE_VOUCHERS, line.voucher.branch)
        voucher_id = line.voucher_id
        try:
            remove_expense_line(line=line)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        else:
            messages.success(request, _("حُذف السطر."))
        return HttpResponseRedirect(reverse("accounting:expense_detail", args=[voucher_id]))


def _require_release_authority(actor: Any, voucher: ExpenseVoucher) -> None:
    """Approving, posting and reversing all need the organization-level authority."""
    require_organization_permission(actor, APPROVE_EXPENSE_VOUCHERS, voucher.organization)


class ExpenseTransitionView(AccountingViewMixin, View):
    """
    Approve, post, reverse and discard — one view, four transitions.

    One view because the shape is identical; four would be four chances to
    check the wrong permission.
    """

    required_permission = MANAGE_EXPENSE_VOUCHERS
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        voucher = visible_vouchers(self.actor).filter(pk=kwargs["pk"]).first()
        if voucher is None:
            raise OutOfScope(_("Expense voucher does not exist."))
        reason = request.POST.get("reason", "").strip()
        detail = reverse("accounting:expense_detail", args=[voucher.pk])

        try:
            if self.action == "approve":
                _require_release_authority(self.actor, voucher)
                approve_expense_voucher(voucher=voucher, approver=self.actor, reason=reason)
                messages.success(request, _("اعتُمد السند."))
            elif self.action == "post":
                _require_release_authority(self.actor, voucher)
                post_expense_voucher(voucher=voucher, poster=self.actor, reason=reason)
                messages.success(request, _("رُحّل السند."))
            elif self.action == "reverse":
                _require_release_authority(self.actor, voucher)
                reverse_expense_voucher(
                    voucher=voucher, actor=self.actor, reason=reason or str(_("عكس يدوي"))
                )
                messages.success(request, _("عُكس السند."))
            elif self.action == "discard":
                require_branch_permission(self.actor, MANAGE_EXPENSE_VOUCHERS, voucher.branch)
                discard_expense_voucher(voucher=voucher, reason=reason)
                messages.success(request, _("حُذفت المسودة."))
                return HttpResponseRedirect(reverse("accounting:expense_list"))
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(detail)


__all__ = [
    "ExpenseLineDeleteView",
    "ExpenseTransitionView",
    "ExpenseVoucherCreateView",
    "ExpenseVoucherDetailView",
    "ExpenseVoucherListView",
    "visible_vouchers",
]
