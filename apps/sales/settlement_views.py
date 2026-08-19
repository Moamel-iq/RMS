"""
تسويات التطبيقات — the screens that build, reconcile, post and reverse one.

Built on `SalesListView` / `SalesWriteView` exactly as `day_views.py` and
`adjustment_views.py` are, and kept in their own module for the same reason:
these drive a *document* through a lifecycle, and master-data maintenance has
almost nothing in common with it beyond the shell they render in.

The detail page is the reconciliation. It shows expected, statement and remitted
as **three separate figures** and never one net number — ADR-028 §7 — with both
unexplained residuals stated in dinars beside them, because the residual is the
only number on the page that decides whether the settlement may move.

One authority throughout: `manage_application_settlements`, exercised at the
organization. Reversal uses the same codename rather than `reverse_daily_sales`,
read off the already-migrated label rather than chosen — it reads *"Can create,
reconcile, post **and reverse** application settlements"*, and the label is the
contract.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.inventory.views import InventoryViewMixin
from apps.organizations.authorization import (
    OutOfScope,
    has_organization_permission,
    require_organization_permission,
)
from apps.sales.models import (
    DeliveryApplicationSettlementAdjustment,
    DeliveryApplicationSettlementAllocation,
    SettlementAdjustmentReason,
    SettlementStatus,
)
from apps.sales.permissions import MANAGE_APPLICATION_SETTLEMENTS, VIEW_SALES
from apps.sales.receivables import unallocated_debit
from apps.sales.selectors import resolve_settlement, visible_settlements
from apps.sales.settlement_forms import (
    SettlementAdjustmentForm,
    SettlementAllocationForm,
    SettlementForm,
    SettlementReasonForm,
)
from apps.sales.settlement_posting import post_settlement, reverse_settlement
from apps.sales.settlement_services import (
    add_settlement_adjustment,
    allocate_entry,
    reconcile_settlement,
    remove_allocation,
    remove_settlement_adjustment,
    return_settlement_to_draft,
    settled_adjustments_for,
    settled_days_for,
    three_way_for,
)
from apps.sales.views import SalesListView, SalesWriteView


class SettlementListView(SalesListView):
    template_name = "sales/settlement_list.html"
    context_object_name = "settlements"
    page_title = _("تسويات التطبيقات")
    page_hint = _(
        "التسوية تُخصَّص على حركات ذمة مرحّلة، لا على إجمالي فترة: تسوية تُسدِّد "
        "«الرصيد حتى ٣١» لا تستطيع أن تقول أيّ المبيعات دفعت ثمنها، وأول طلب "
        "متنازع عليه يصبح بلا جواب. العمولة لا تُعترف مرتين: ما في الكشف يُقارَن "
        "بالمستحق عند البيع، والفرق فرقٌ يُفسَّر لا مصروف جديد."
    )
    search_fields = ("number", "statement_reference", "delivery_application__code", "notes")
    manage_permission = MANAGE_APPLICATION_SETTLEMENTS
    manage_scope = "organization"
    create_url_name = "sales:settlement_create"
    create_label = _("تسوية جديدة")
    result_label = _("تسوية")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_settlements(self.actor)
        status = self.request.GET.get("status", "").strip()
        if status in SettlementStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-period_end", "-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = SettlementStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        return context


class SettlementCreateView(SalesWriteView):
    form_class = SettlementForm
    required_permission = MANAGE_APPLICATION_SETTLEMENTS
    success_url_name = "sales:settlement_list"
    page_title = _("تسوية تطبيق جديدة")
    page_hint = _(
        "ثلاثة أرقام منفصلة: المستحق لدينا، ما يقوله كشف التطبيق، وما وصل فعلاً. "
        "لا تُختصر إلى رقم واحد — أيّ رقمين يتطابقان هو التشخيص نفسه."
    )
    success_message = _("تم فتح التسوية.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor,
            MANAGE_APPLICATION_SETTLEMENTS,
            form.cleaned_data["branch"].organization,
        )

    def perform(self, instance: Any, form: Any) -> None:
        from apps.sales.settlement_services import create_settlement

        data = form.cleaned_data
        branch = data["branch"]
        self.created = create_settlement(
            organization=branch.organization,
            branch=branch,
            delivery_application=data["delivery_application"],
            period_start=data["period_start"],
            period_end=data["period_end"],
            business_date=data["business_date"],
            statement_reference=data["statement_reference"],
            statement_date=data["statement_date"],
            statement_amount=data["statement_amount"],
            remitted_amount=data["remitted_amount"],
            statement_commission_amount=data["statement_commission_amount"],
            remittance_destination=data["remittance_destination"],
            evidence_reference=data["evidence_reference"],
            actor=self.actor,
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is not None:
            return reverse("sales:settlement_detail", args=[created.pk])
        return reverse(self.success_url_name)


class SettlementDetailView(InventoryViewMixin, View):
    """
    One statement, reconciled against one ledger.

    A single POST endpoint handling two different additions, discriminated by a
    `form` field the templates set. Two endpoints would each need their own
    re-render of this page on a validation failure, and the page is the
    reconciliation — losing it because an amount was mistyped is the one thing
    an operator cannot be asked to tolerate here.
    """

    module_key = "sales"
    required_permission = VIEW_SALES

    def _context(self, settlement: Any, request: HttpRequest, **extra: Any) -> dict[str, Any]:
        may_edit = settlement.is_editable and has_organization_permission(
            self.actor, MANAGE_APPLICATION_SETTLEMENTS, settlement.organization
        )
        may_manage = has_organization_permission(
            self.actor, MANAGE_APPLICATION_SETTLEMENTS, settlement.organization
        )
        comparison = three_way_for(settlement)
        allocations = list(
            settlement.allocations.select_related("receivable_entry").order_by(
                "receivable_entry__business_date", "receivable_entry_id"
            )
        )
        context: dict[str, Any] = {
            "settlement": settlement,
            "three_way": comparison,
            "allocations": [(row, unallocated_debit(row.receivable_entry)) for row in allocations],
            "adjustments": settlement.adjustments.select_related("approved_by").order_by(
                "leg", "id"
            ),
            "settled_days": settled_days_for(settlement),
            "settled_adjustments": settled_adjustments_for(settlement),
            "may_edit": may_edit,
            "may_reconcile": (
                settlement.status == SettlementStatus.DRAFT
                and may_manage
                and comparison.is_reconcilable
            ),
            "may_return": settlement.status == SettlementStatus.RECONCILED and may_manage,
            "may_post": settlement.status == SettlementStatus.RECONCILED and may_manage,
            # The same codename, read off the migrated label. See the module
            # docstring.
            "may_reverse": settlement.status == SettlementStatus.POSTED and may_manage,
            "allocation_form": (
                SettlementAllocationForm(settlement=settlement) if may_edit else None
            ),
            "adjustment_form": SettlementAdjustmentForm() if may_edit else None,
            "reversal_form": (
                SettlementReasonForm()
                if settlement.status in {SettlementStatus.POSTED, SettlementStatus.RECONCILED}
                and may_manage
                else None
            ),
            "page_title": _("تسوية %(app)s — %(reference)s")
            % {
                "app": settlement.delivery_application.code,
                "reference": settlement.statement_reference,
            },
            # `_form_fragment.html`, not `_list_fragment.html`. This template
            # extends `list_base_template` **directly** rather than through
            # `settings/base_list.html`, so the block it defines is `page`;
            # `_list_fragment.html` contains only `results`, Django silently
            # drops a child block the parent does not declare, and the htmx
            # form of this screen answered 200 with an empty body.
            "list_base_template": (
                "settings/_form_fragment.html"
                if request.headers.get("HX-Request") == "true"
                else "shell.html"
            ),
        }
        context.update(extra)
        return context

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        settlement = resolve_settlement(self.actor, kwargs["pk"])
        return render(request, "sales/settlement_detail.html", self._context(settlement, request))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        settlement = resolve_settlement(self.actor, kwargs["pk"])
        require_organization_permission(
            self.actor, MANAGE_APPLICATION_SETTLEMENTS, settlement.organization
        )

        if request.POST.get("form") == "adjustment":
            return self._add_adjustment(request, settlement)
        return self._add_allocation(request, settlement)

    def _add_allocation(self, request: HttpRequest, settlement: Any) -> HttpResponse:
        form = SettlementAllocationForm(request.POST, settlement=settlement)
        if form.is_valid():
            try:
                allocate_entry(
                    settlement=settlement,
                    receivable_entry=form.cleaned_data["receivable_entry"],
                    allocated_amount=form.cleaned_data["allocated_amount"],
                    actor=self.actor,
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تم تخصيص الحركة."))
                return HttpResponseRedirect(
                    reverse("sales:settlement_detail", args=[settlement.pk])
                )
        return render(
            request,
            "sales/settlement_detail.html",
            self._context(settlement, request, allocation_form=form),
        )

    def _add_adjustment(self, request: HttpRequest, settlement: Any) -> HttpResponse:
        form = SettlementAdjustmentForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            # The approver is the caller, and only where they said so. An
            # implicit approval would satisfy the constraint while recording
            # that nobody decided anything.
            approver = self.actor if data.get("approve") else None
            try:
                add_settlement_adjustment(
                    settlement=settlement,
                    leg=data["leg"],
                    reason=data["reason"],
                    amount=data["amount"] or Decimal("0"),
                    explanation=data.get("explanation", ""),
                    actor=self.actor,
                    approver=approver,
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة تفسير الفرق."))
                return HttpResponseRedirect(
                    reverse("sales:settlement_detail", args=[settlement.pk])
                )
        return render(
            request,
            "sales/settlement_detail.html",
            self._context(settlement, request, adjustment_form=form),
        )


class SettlementAllocationDeleteView(InventoryViewMixin, View):
    """POST-only. A GET that removed a claim would fire on a link prefetch."""

    module_key = "sales"
    required_permission = MANAGE_APPLICATION_SETTLEMENTS

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        allocation = (
            DeliveryApplicationSettlementAllocation.objects.filter(pk=kwargs["pk"])
            .select_related("settlement", "settlement__organization")
            .first()
        )
        if allocation is None or allocation.settlement not in visible_settlements(self.actor):
            raise OutOfScope(_("Settlement allocation does not exist."))
        settlement = allocation.settlement
        require_organization_permission(
            self.actor, MANAGE_APPLICATION_SETTLEMENTS, settlement.organization
        )
        try:
            remove_allocation(allocation=allocation, actor=self.actor)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف التخصيص."))
        return HttpResponseRedirect(reverse("sales:settlement_detail", args=[settlement.pk]))


class SettlementAdjustmentDeleteView(InventoryViewMixin, View):
    """POST-only, for the same reason."""

    module_key = "sales"
    required_permission = MANAGE_APPLICATION_SETTLEMENTS

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        adjustment = (
            DeliveryApplicationSettlementAdjustment.objects.filter(pk=kwargs["pk"])
            .select_related("settlement", "settlement__organization")
            .first()
        )
        if adjustment is None or adjustment.settlement not in visible_settlements(self.actor):
            raise OutOfScope(_("Settlement adjustment does not exist."))
        settlement = adjustment.settlement
        require_organization_permission(
            self.actor, MANAGE_APPLICATION_SETTLEMENTS, settlement.organization
        )
        try:
            remove_settlement_adjustment(adjustment=adjustment, actor=self.actor)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف التفسير."))
        return HttpResponseRedirect(reverse("sales:settlement_detail", args=[settlement.pk]))


class SettlementTransitionView(InventoryViewMixin, View):
    """
    Reconcile, return, post and reverse — one view, four transitions.

    One view because the shape is identical: resolve the settlement with the
    caller, check the authority, call the service, turn a `ValidationError` into
    a message. Four copies would be four chances to check the wrong permission.
    """

    module_key = "sales"
    required_permission = VIEW_SALES
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        settlement = resolve_settlement(self.actor, kwargs["pk"])
        require_organization_permission(
            self.actor, MANAGE_APPLICATION_SETTLEMENTS, settlement.organization
        )
        reason = request.POST.get("reason", "").strip()

        try:
            if self.action == "reconcile":
                reconcile_settlement(settlement=settlement, actor=self.actor)
                messages.success(request, _("تمت المطابقة."))
            elif self.action == "return":
                return_settlement_to_draft(settlement=settlement, actor=self.actor, reason=reason)
                messages.success(request, _("أُعيدت التسوية إلى المسودة."))
            elif self.action == "post":
                post_settlement(settlement=settlement, actor=self.actor)
                messages.success(request, _("تم ترحيل التسوية."))
            elif self.action == "reverse":
                reverse_settlement(settlement=settlement, actor=self.actor, reason=reason)
                messages.success(request, _("تم عكس التسوية."))
            else:  # pragma: no cover - a routing mistake, not a state
                raise ValidationError(_("Unknown transition."), code="unknown_action")
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(reverse("sales:settlement_detail", args=[settlement.pk]))


#: Re-exported so a template or a test can name the escape hatch without
#: importing the model module for one constant.
UNEXPLAINED_APPROVED = SettlementAdjustmentReason.UNEXPLAINED_APPROVED


__all__ = [
    "UNEXPLAINED_APPROVED",
    "SettlementAdjustmentDeleteView",
    "SettlementAllocationDeleteView",
    "SettlementCreateView",
    "SettlementDetailView",
    "SettlementListView",
    "SettlementTransitionView",
]
