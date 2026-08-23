"""
المبيعات اليومية — the screens that build, submit, post and reverse a day.

Kept in their own module rather than added to `views.py`, which is master-data
maintenance. These screens drive a *document* through a lifecycle, and the two
have almost nothing in common beyond the shell they render in.

The entry grid is a detail page with an inline add-line form rather than a
spreadsheet widget, and that is deliberate: every line has to be **resolved**
against the business date — its recipe version, its serving, its price, its
agreement, its commission — and a resolution that can fail needs somewhere to
say why. A grid that accepted twenty rows and rejected the eighteenth would
lose the other nineteen.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.inventory.views import InventoryViewMixin
from apps.organizations.authorization import (
    has_branch_permission,
    has_organization_permission,
    require_branch_permission,
    require_organization_permission,
)
from apps.sales.day_forms import SalesDayForm, SalesLineForm, TenderSummaryForm
from apps.sales.day_services import (
    add_sales_line,
    create_sales_day,
    remove_sales_line,
    return_sales_day_to_draft,
    set_tender_summary,
    submit_sales_day,
    totals_for,
)
from apps.sales.models import SalesDay, SalesDayLine, SalesDayStatus
from apps.sales.permissions import (
    CREATE_DAILY_SALES,
    POST_DAILY_SALES,
    REVERSE_DAILY_SALES,
    SUBMIT_DAILY_SALES,
    VIEW_SALES,
)
from apps.sales.posting import post_sales_day, reverse_sales_day
from apps.sales.selectors import resolve_sales_day, visible_sales_days
from apps.sales.views import SalesListView, SalesWriteView


class SalesDayListView(SalesListView):
    template_name = "sales/sales_day_list.html"
    context_object_name = "days"
    page_title = _("المبيعات اليومية")
    page_hint = _(
        "مستند واحد لكل فرع ولكل تاريخ عمل. تاريخ العمل يُدخَل ولا يُشتق من الوقت: "
        "يوم البيع يبدأ بتوقيت الفرع نفسه، واشتقاقه من الطابع الزمني يضع مبيعات "
        "ما بعد منتصف الليل في اليوم الخطأ."
    )
    search_fields = ("number", "branch__code", "notes")
    manage_permission = CREATE_DAILY_SALES
    manage_scope = "branch"
    create_url_name = "sales:day_create"
    create_label = _("يوم مبيعات جديد")
    result_label = _("يوم")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_sales_days(self.actor)
        status = self.request.GET.get("status", "").strip()
        if status in SalesDayStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-business_date", "branch__code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = SalesDayStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        return context


class SalesDayCreateView(SalesWriteView):
    form_class = SalesDayForm
    required_permission = CREATE_DAILY_SALES
    success_url_name = "sales:day_list"
    page_title = _("يوم مبيعات جديد")
    page_hint = _("فرع واحد وتاريخ عمل واحد. لا يمكن فتح يومين لنفس الفرع ونفس التاريخ.")
    success_message = _("تم فتح يوم المبيعات.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_branch_permission(self.actor, CREATE_DAILY_SALES, form.cleaned_data["branch"])

    def perform(self, instance: Any, form: Any) -> None:
        branch = form.cleaned_data["branch"]
        self.created = create_sales_day(
            organization=branch.organization,
            branch=branch,
            business_date=form.cleaned_data["business_date"],
            actor=self.actor,
            notes=form.cleaned_data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is not None:
            return reverse("sales:day_detail", args=[created.pk])
        return reverse(self.success_url_name)


class SalesDayDetailView(InventoryViewMixin, View):
    """
    One day: its lines, its totals, and whatever it may do next.

    The action buttons are decided from the day's own status **and** the
    caller's authority at that branch, so a cashier sees "submit" and not
    "post". Hiding a button is presentation rather than protection — the
    transition view refuses the same request either way — but offering a dead
    end is its own kind of wrong.
    """

    module_key = "sales"
    required_permission = VIEW_SALES

    def _context(self, day: SalesDay, request: HttpRequest, **extra: Any) -> dict[str, Any]:
        may_edit = day.is_editable and has_branch_permission(
            self.actor, CREATE_DAILY_SALES, day.branch
        )
        context: dict[str, Any] = {
            "day": day,
            "lines": day.lines.select_related(
                "menu_item",
                "channel",
                "delivery_application",
                "recipe",
                "recipe_version",
                "serving",
                "inventory_item",
                "inventory_item__base_unit",
                "source_warehouse",
            ).order_by("sequence"),
            "totals": totals_for(day),
            "tender_summaries": day.tender_summaries.all(),
            "may_edit": may_edit,
            "may_submit": day.status == SalesDayStatus.DRAFT
            and has_branch_permission(self.actor, SUBMIT_DAILY_SALES, day.branch),
            "may_post": day.status == SalesDayStatus.SUBMITTED
            and has_branch_permission(self.actor, POST_DAILY_SALES, day.branch),
            "may_return": day.status == SalesDayStatus.SUBMITTED
            and has_branch_permission(self.actor, CREATE_DAILY_SALES, day.branch),
            "may_reverse": day.status == SalesDayStatus.POSTED
            and has_organization_permission(self.actor, REVERSE_DAILY_SALES, day.organization),
            "line_form": SalesLineForm(actor=self.actor, day=day) if may_edit else None,
            "tender_form": TenderSummaryForm(day=day) if may_edit else None,
            "page_title": _("مبيعات %(branch)s — %(date)s")
            % {"branch": day.branch.code, "date": day.business_date.isoformat()},
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
        day = resolve_sales_day(self.actor, kwargs["pk"])
        return render(request, "sales/sales_day_detail.html", self._context(day, request))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        day = resolve_sales_day(self.actor, kwargs["pk"])
        require_branch_permission(self.actor, CREATE_DAILY_SALES, day.branch)

        if request.POST.get("form") == "tender":
            tender_form = TenderSummaryForm(request.POST, day=day)
            if tender_form.is_valid():
                try:
                    set_tender_summary(
                        day=day,
                        tender=tender_form.cleaned_data["tender"],
                        declared_amount=tender_form.cleaned_data["declared_amount"],
                        notes=tender_form.cleaned_data.get("notes", ""),
                    )
                except ValidationError as error:
                    messages.error(request, "؛ ".join(str(m) for m in error.messages))
                else:
                    messages.success(request, _("تم تسجيل المُعلن."))
                    return HttpResponseRedirect(reverse("sales:day_detail", args=[day.pk]))
            return render(
                request,
                "sales/sales_day_detail.html",
                self._context(day, request, tender_form=tender_form),
            )

        form = SalesLineForm(request.POST, actor=self.actor, day=day)
        if form.is_valid():
            data = form.cleaned_data
            try:
                add_sales_line(
                    day=day,
                    menu_item=data["menu_item"],
                    channel=data["channel"],
                    quantity=data["quantity"],
                    delivery_application=data.get("delivery_application"),
                    order_count=data.get("order_count") or 0,
                    discount_program=data.get("discount_program"),
                    manual_discount_amount=data.get("manual_discount_amount"),
                    manual_discount_reason=data.get("manual_discount_reason", ""),
                    other_fee_amount=data.get("other_fee_amount") or Decimal("0"),
                    notes=data.get("notes", ""),
                )
            except ValidationError as error:
                # A resolution that failed — no price, no effective version, no
                # agreement. Rendered as a sentence rather than a traceback,
                # because every one of them is something an operator can fix.
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تمت إضافة السطر."))
                return HttpResponseRedirect(reverse("sales:day_detail", args=[day.pk]))
        return render(
            request, "sales/sales_day_detail.html", self._context(day, request, line_form=form)
        )


class SalesDayLineDeleteView(InventoryViewMixin, View):
    """POST-only. A GET that deleted a line would fire on a link prefetch."""

    module_key = "sales"
    required_permission = CREATE_DAILY_SALES

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        line = (
            SalesDayLine.objects.filter(pk=kwargs["pk"])
            .select_related("sales_day", "sales_day__branch")
            .first()
        )
        if line is None or line.sales_day not in visible_sales_days(self.actor):
            raise ObjectDoesNotExist(_("Sales line does not exist."))
        day = line.sales_day
        require_branch_permission(self.actor, CREATE_DAILY_SALES, day.branch)
        try:
            remove_sales_line(line=line)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(m) for m in error.messages))
        else:
            messages.success(request, _("تم حذف السطر."))
        return HttpResponseRedirect(reverse("sales:day_detail", args=[day.pk]))


class SalesDayTransitionView(InventoryViewMixin, View):
    """
    Submit, return, post and reverse — one view, four transitions.

    One view because the shape is identical: resolve the day with the caller,
    check the authority the *specific* transition needs, call the service, turn
    a `ValidationError` into a message. Four views would be four copies of that
    with four chances to check the wrong permission.
    """

    module_key = "sales"
    required_permission = VIEW_SALES
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        day = resolve_sales_day(self.actor, kwargs["pk"])
        reason = request.POST.get("reason", "").strip()

        try:
            if self.action == "submit":
                require_branch_permission(self.actor, SUBMIT_DAILY_SALES, day.branch)
                submit_sales_day(day=day, actor=self.actor)
                messages.success(request, _("تم إرسال اليوم للترحيل."))
            elif self.action == "return":
                require_branch_permission(self.actor, CREATE_DAILY_SALES, day.branch)
                return_sales_day_to_draft(day=day, actor=self.actor, reason=reason)
                messages.success(request, _("أُعيد اليوم إلى المسودة."))
            elif self.action == "post":
                require_branch_permission(self.actor, POST_DAILY_SALES, day.branch)
                post_sales_day(day=day, actor=self.actor)
                messages.success(request, _("تم ترحيل اليوم."))
            elif self.action == "reverse":
                require_organization_permission(self.actor, REVERSE_DAILY_SALES, day.organization)
                reverse_sales_day(day=day, actor=self.actor, reason=reason)
                messages.success(request, _("تم عكس اليوم."))
            else:  # pragma: no cover - a routing mistake, not a state
                raise ValidationError(_("Unknown transition."), code="unknown_action")
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(reverse("sales:day_detail", args=[day.pk]))


__all__ = [
    "SalesDayCreateView",
    "SalesDayDetailView",
    "SalesDayLineDeleteView",
    "SalesDayListView",
    "SalesDayTransitionView",
]
