"""
المرتجعات والإلغاءات — the screens that draft, post and reverse a correction.

Built on `SalesListView` / `SalesWriteView` exactly as `day_views.py` is, and
kept in their own module for the same reason: these drive a *document* through a
lifecycle, and master-data maintenance has almost nothing in common with it
beyond the shell they render in.

The detail page is a grid with an inline add-line form rather than a
spreadsheet, because every line has to be *contained* — inside the quantity the
original sold, inside its gross, and inside what other posted adjustments have
already taken back — and a containment that can fail needs somewhere to say
which of the three it was.

Two authorities, deliberately different. Drafting and posting are
`MANAGE_SALES_ADJUSTMENTS` at the branch; **reversing is
`REVERSE_DAILY_SALES`** across the organization, read off the already-migrated
labels rather than chosen: `manage_sales_adjustments` reads *"Can record returns
and cancellations"* and says nothing about reversal, while `reverse_daily_sales`
is this module's declared supervisory undo.
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
    has_branch_permission,
    has_organization_permission,
    require_branch_permission,
    require_organization_permission,
)
from apps.sales.adjustment_forms import (
    AdjustmentLineForm,
    AdjustmentReversalForm,
    SalesAdjustmentForm,
)
from apps.sales.adjustment_posting import post_sales_adjustment, reverse_sales_adjustment
from apps.sales.adjustment_services import (
    add_adjustment_line,
    create_sales_adjustment,
    remove_adjustment_line,
    totals_for,
)
from apps.sales.models import (
    SalesAdjustment,
    SalesAdjustmentLine,
    SalesAdjustmentReasonKind,
    SalesAdjustmentStatus,
)
from apps.sales.permissions import (
    MANAGE_SALES_ADJUSTMENTS,
    REVERSE_DAILY_SALES,
    VIEW_SALES,
)
from apps.sales.selectors import resolve_sales_adjustment, visible_sales_adjustments
from apps.sales.views import SalesListView, SalesWriteView


class SalesAdjustmentListView(SalesListView):
    template_name = "sales/sales_adjustment_list.html"
    context_object_name = "adjustments"
    page_title = _("المرتجعات والإلغاءات")
    page_hint = _(
        "السطر المرحّل لا يُعدَّل أبداً: ما رجع مستند قائم بذاته إلى جانب ما بيع. "
        "الإلغاء قبل التنفيذ وحده يخفض الاستهلاك النظري — الطلب لم يُطبخ — أما "
        "الإرجاع بعد التنفيذ فلا، لأن المكوّنات خرجت فعلاً وطرحها يصنع فرقاً "
        "غير مفسّر بمقدار المرتجع بالضبط."
    )
    search_fields = ("number", "branch__code", "evidence_reference", "reason")
    manage_permission = MANAGE_SALES_ADJUSTMENTS
    manage_scope = "branch"
    create_url_name = "sales:adjustment_create"
    create_label = _("تسوية جديدة")
    result_label = _("تسوية")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_sales_adjustments(self.actor)
        status = self.request.GET.get("status", "").strip()
        if status in SalesAdjustmentStatus.values:
            queryset = queryset.filter(status=status)
        kind = self.request.GET.get("kind", "").strip()
        if kind in SalesAdjustmentReasonKind.values:
            queryset = queryset.filter(reason_kind=kind)
        return queryset.order_by("-business_date", "-id")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = SalesAdjustmentStatus.choices
        context["kinds"] = SalesAdjustmentReasonKind.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["selected_kind"] = self.request.GET.get("kind", "")
        return context


class SalesAdjustmentCreateView(SalesWriteView):
    form_class = SalesAdjustmentForm
    required_permission = MANAGE_SALES_ADJUSTMENTS
    success_url_name = "sales:adjustment_list"
    page_title = _("تسوية مبيعات جديدة")
    page_hint = _(
        "تُسجَّل التسوية على يوم مرحّل، وتحمل سبباً ومستنداً إلزاميين. كل تسوية "
        "تخفض: التصحيح الذي يزيد ما حُصِّل هو يوم مبيعات جديد، لا تسوية."
    )
    success_message = _("تم فتح التسوية.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_branch_permission(
            self.actor, MANAGE_SALES_ADJUSTMENTS, form.cleaned_data["sales_day"].branch
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        self.created = create_sales_adjustment(
            sales_day=data["sales_day"],
            reason_kind=data["reason_kind"],
            business_date=data["business_date"],
            reason=data["reason"],
            evidence_reference=data["evidence_reference"],
            actor=self.actor,
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is not None:
            return reverse("sales:adjustment_detail", args=[created.pk])
        return reverse(self.success_url_name)


class SalesAdjustmentDetailView(InventoryViewMixin, View):
    """
    One correction: what it takes back, and whatever it may do next.

    The action buttons are decided from the adjustment's status **and** the
    caller's authority, so somebody who may record a return but not reverse one
    sees "post" and not "reverse". Hiding a button is presentation rather than
    protection — the transition view refuses the same request either way — but
    offering a dead end is its own kind of wrong.
    """

    module_key = "sales"
    required_permission = VIEW_SALES

    def _context(
        self, adjustment: SalesAdjustment, request: HttpRequest, **extra: Any
    ) -> dict[str, Any]:
        may_edit = adjustment.is_editable and has_branch_permission(
            self.actor, MANAGE_SALES_ADJUSTMENTS, adjustment.branch
        )
        may_reverse = adjustment.status == SalesAdjustmentStatus.POSTED and (
            has_organization_permission(self.actor, REVERSE_DAILY_SALES, adjustment.organization)
        )
        context: dict[str, Any] = {
            "adjustment": adjustment,
            "lines": adjustment.lines.select_related(
                "original_line",
                "original_line__menu_item",
                "original_line__channel",
                "original_line__delivery_application",
            ).order_by("sequence"),
            "totals": totals_for(adjustment),
            "may_edit": may_edit,
            "may_post": adjustment.status == SalesAdjustmentStatus.DRAFT
            and has_branch_permission(self.actor, MANAGE_SALES_ADJUSTMENTS, adjustment.branch),
            # **Not** `manage_sales_adjustments`. See the module docstring.
            "may_reverse": may_reverse,
            "line_form": AdjustmentLineForm(adjustment=adjustment) if may_edit else None,
            # A typed reason rather than a canned one. `reversal_reason` may not
            # be blank — a check constraint says so — and a hidden field holding
            # a constant would satisfy the constraint while telling the next
            # reader nothing about why this correction was undone.
            "reversal_form": AdjustmentReversalForm() if may_reverse else None,
            "page_title": _("تسوية %(branch)s — %(date)s")
            % {
                "branch": adjustment.branch.code,
                "date": adjustment.business_date.isoformat(),
            },
            "list_base_template": (
                "settings/_list_fragment.html"
                if request.headers.get("HX-Request") == "true"
                else "shell.html"
            ),
        }
        context.update(extra)
        return context

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        adjustment = resolve_sales_adjustment(self.actor, kwargs["pk"])
        return render(
            request, "sales/sales_adjustment_detail.html", self._context(adjustment, request)
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        adjustment = resolve_sales_adjustment(self.actor, kwargs["pk"])
        require_branch_permission(self.actor, MANAGE_SALES_ADJUSTMENTS, adjustment.branch)

        form = AdjustmentLineForm(request.POST, adjustment=adjustment)
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_adjustment_line(
                    adjustment=adjustment,
                    original_line=data["original_line"],
                    adjusted_quantity=data.get("adjusted_quantity") or Decimal("0"),
                    adjusted_gross=data.get("adjusted_gross"),
                    line_reason=data.get("line_reason", ""),
                    actor=self.actor,
                )
            except ValidationError as error:
                # A containment that failed — more quantity than the line sold,
                # more value than it carried, a correction touching quantity.
                # Rendered as a sentence, because every one of them is something
                # an operator can fix.
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة السطر."))
                return HttpResponseRedirect(
                    reverse("sales:adjustment_detail", args=[adjustment.pk])
                )
        return render(
            request,
            "sales/sales_adjustment_detail.html",
            self._context(adjustment, request, line_form=form),
        )


class SalesAdjustmentLineDeleteView(InventoryViewMixin, View):
    """POST-only. A GET that deleted a line would fire on a link prefetch."""

    module_key = "sales"
    required_permission = MANAGE_SALES_ADJUSTMENTS

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        line = (
            SalesAdjustmentLine.objects.filter(pk=kwargs["pk"])
            .select_related("adjustment", "adjustment__branch")
            .first()
        )
        if line is None or line.adjustment not in visible_sales_adjustments(self.actor):
            raise OutOfScope(_("Sales adjustment line does not exist."))
        adjustment = line.adjustment
        require_branch_permission(self.actor, MANAGE_SALES_ADJUSTMENTS, adjustment.branch)
        try:
            remove_adjustment_line(line=line, actor=self.actor)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف السطر."))
        return HttpResponseRedirect(reverse("sales:adjustment_detail", args=[adjustment.pk]))


class SalesAdjustmentTransitionView(InventoryViewMixin, View):
    """
    Post and reverse — one view, two transitions.

    One view because the shape is identical: resolve the adjustment with the
    caller, check the authority the *specific* transition needs, call the
    service, turn a `ValidationError` into a message. Two views would be two
    copies of that with two chances to check the wrong permission, and the two
    permissions here are genuinely different.
    """

    module_key = "sales"
    required_permission = VIEW_SALES
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        adjustment = resolve_sales_adjustment(self.actor, kwargs["pk"])
        reason = request.POST.get("reason", "").strip()

        try:
            if self.action == "post":
                require_branch_permission(self.actor, MANAGE_SALES_ADJUSTMENTS, adjustment.branch)
                post_sales_adjustment(adjustment=adjustment, actor=self.actor)
                messages.success(request, _("تم ترحيل التسوية."))
            elif self.action == "reverse":
                require_organization_permission(
                    self.actor, REVERSE_DAILY_SALES, adjustment.organization
                )
                reverse_sales_adjustment(adjustment=adjustment, actor=self.actor, reason=reason)
                messages.success(request, _("تم عكس التسوية."))
            else:  # pragma: no cover - a routing mistake, not a state
                raise ValidationError(_("Unknown transition."), code="unknown_action")
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(reverse("sales:adjustment_detail", args=[adjustment.pk]))


__all__ = [
    "SalesAdjustmentCreateView",
    "SalesAdjustmentDetailView",
    "SalesAdjustmentLineDeleteView",
    "SalesAdjustmentListView",
    "SalesAdjustmentTransitionView",
]
