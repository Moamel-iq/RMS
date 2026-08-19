"""
إقفال الكاشير — the screens that open, count, close, approve and reverse a till.

Built on `SalesListView` / `SalesWriteView` exactly as `day_views.py`,
`adjustment_views.py` and `settlement_views.py` are, and kept in their own
module for the same reason: these drive a *document* through a lifecycle.

## Two authorities, and the maker-checker that is not one of them

`close_cashier_shift` opens, counts, closes and reopens.
`approve_cashier_closing` approves. Both are `BRANCH`, and a branch manager
legitimately holds **both** — that is not a hole.

Maker-checker here is enforced on the **actor**: `approve_cashier_shift` refuses
when `actor == shift.closed_by`, and `sales_shift_approver_is_not_the_closer`
refuses the same row at the database. So a lone manager can run their branch —
close a shift somebody else counted, or have their own count approved by the
accounting manager — and what nobody can do is close and approve the same
drawer. Encoding the control as "only some other role may approve" would break
the first single-manager branch and would be a weaker control besides: it would
still let two managers approve each other's shifts *and their own*.

**Reversing** an approved shift is `REVERSE_DAILY_SALES` across the
organization, read off the already-migrated labels rather than chosen:
`close_cashier_shift` reads *"Can open and close a cashier shift"*,
`approve_cashier_closing` reads *"Can approve a cashier closing"*, and neither
says reverse. `reverse_daily_sales` is this module's declared supervisory undo.

The approve button is hidden from the person who closed the shift. Hiding it is
presentation rather than protection — the transition view and the database
refuse the request either way — but offering somebody a button that is
guaranteed to fail is its own kind of wrong.
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
    has_branch_permission,
    has_organization_permission,
    require_branch_permission,
    require_organization_permission,
)
from apps.sales.models import CashierShiftStatus, TenderDestination
from apps.sales.permissions import (
    APPROVE_CASHIER_CLOSING,
    CLOSE_CASHIER_SHIFT,
    REVERSE_DAILY_SALES,
    VIEW_SALES,
)
from apps.sales.selectors import resolve_cashier_shift, visible_cashier_shifts
from apps.sales.shift_forms import (
    CashierShiftCloseForm,
    CashierShiftForm,
    ShiftReasonForm,
    TenderCountForm,
)
from apps.sales.shift_posting import approve_cashier_shift, reverse_cashier_shift
from apps.sales.shift_services import (
    close_cashier_shift,
    expected_by_tender,
    open_cashier_shift,
    reopen_cashier_shift,
    set_tender_count,
)
from apps.sales.views import SalesListView, SalesWriteView


class CashierShiftListView(SalesListView):
    template_name = "sales/cashier_shift_list.html"
    context_object_name = "shifts"
    page_title = _("إقفال الكاشير")
    page_hint = _(
        "الإقفال يُرحّل شيئاً واحداً فقط: فرق الصندوق المعتمد. البيع اعترف "
        "بالإيراد وأثبت النقد يوم رُحِّل، وإقفال يعيد ترحيل الإيرادات يضاعف كل "
        "رقم نقدي في النظام دون أن يظهر ذلك في أي تقرير. العهدة الافتتاحية ليست "
        "إيراداً، والبطاقات ليست في الدرج."
    )
    search_fields = ("number", "branch__code", "cashier__username", "notes")
    manage_permission = CLOSE_CASHIER_SHIFT
    manage_scope = "branch"
    create_url_name = "sales:shift_create"
    create_label = _("صندوق جديد")
    result_label = _("صندوق")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_cashier_shifts(self.actor)
        status = self.request.GET.get("status", "").strip()
        if status in CashierShiftStatus.values:
            queryset = queryset.filter(status=status)
        return queryset.order_by("-business_date", "branch__code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = CashierShiftStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        return context


class CashierShiftCreateView(SalesWriteView):
    form_class = CashierShiftForm
    required_permission = CLOSE_CASHIER_SHIFT
    success_url_name = "sales:shift_list"
    page_title = _("فتح صندوق")
    page_hint = _(
        "صندوق واحد لكل فرع في كل تاريخ عمل في الإصدار الأول: دقة المبيعات هنا "
        "يوم كامل، وصندوق ثانٍ لن يكون له نصيب مبدئي من نقد اليوم — وتوزيعه "
        "تقديراً يفرّغ الفرق، وهو الرقم الوحيد الذي وُجد المستند لإنتاجه."
    )
    success_message = _("تم فتح الصندوق.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_branch_permission(self.actor, CLOSE_CASHIER_SHIFT, form.cleaned_data["branch"])

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        branch = data["branch"]
        self.created = open_cashier_shift(
            organization=branch.organization,
            branch=branch,
            business_date=data["business_date"],
            cashier=data["cashier"],
            opening_float=data["opening_float"],
            actor=self.actor,
            notes=data.get("notes", ""),
        )

    def get_success_url(self) -> str:
        created = getattr(self, "created", None)
        if created is not None:
            return reverse("sales:shift_detail", args=[created.pk])
        return reverse(self.success_url_name)


class CashierShiftDetailView(InventoryViewMixin, View):
    """
    One till: what was expected, what was counted, and whatever it may do next.

    The counts table shows expected beside counted **after** the count is
    recorded and never inside the input, for the reason `shift_forms` states: a
    field pre-filled with the expectation is an invitation to confirm rather
    than to count.

    While the shift is open the expected column is a live figure from the posted
    day; from `CLOSED` onwards it is the stamped one, because that is what the
    variance was computed against and re-deriving it would let an approved
    difference change whenever a later document did.
    """

    module_key = "sales"
    required_permission = VIEW_SALES

    def _context(self, shift: Any, request: HttpRequest, **extra: Any) -> dict[str, Any]:
        may_count = shift.is_editable and has_branch_permission(
            self.actor, CLOSE_CASHIER_SHIFT, shift.branch
        )
        may_approve = (
            shift.status == CashierShiftStatus.CLOSED
            and has_branch_permission(self.actor, APPROVE_CASHIER_CLOSING, shift.branch)
            # Presentation only — the service and the constraint refuse it
            # anyway — but a button guaranteed to fail is its own kind of wrong.
            and shift.closed_by_id != self.actor.pk
        )
        live_expected = expected_by_tender(shift)
        counts = list(shift.tender_counts.all())
        rows = [
            {
                "row": row,
                "expected": (
                    row.expected_amount
                    if shift.status != CashierShiftStatus.OPEN
                    else live_expected.get(row.tender, Decimal("0"))
                ),
            }
            for row in counts
        ]

        context: dict[str, Any] = {
            "shift": shift,
            "count_rows": rows,
            "live_expected": live_expected,
            "expected_cash_now": (
                shift.opening_float + live_expected.get(TenderDestination.CASH, Decimal("0"))
            ),
            "may_count": may_count,
            "may_close": may_count,
            "may_reopen": shift.status == CashierShiftStatus.CLOSED
            and has_branch_permission(self.actor, CLOSE_CASHIER_SHIFT, shift.branch),
            "may_approve": may_approve,
            "closer_cannot_approve": (
                shift.status == CashierShiftStatus.CLOSED
                and shift.closed_by_id == self.actor.pk
                and has_branch_permission(self.actor, APPROVE_CASHIER_CLOSING, shift.branch)
            ),
            # **Not** `approve_cashier_closing`. See the module docstring.
            "may_reverse": shift.status == CashierShiftStatus.APPROVED
            and has_organization_permission(self.actor, REVERSE_DAILY_SALES, shift.organization),
            "count_form": TenderCountForm(shift=shift) if may_count else None,
            "close_form": CashierShiftCloseForm(shift=shift) if may_count else None,
            "reason_form": ShiftReasonForm(),
            "page_title": _("صندوق %(branch)s — %(date)s")
            % {"branch": shift.branch.code, "date": shift.business_date.isoformat()},
            "list_base_template": (
                "settings/_list_fragment.html"
                if request.headers.get("HX-Request") == "true"
                else "shell.html"
            ),
        }
        context.update(extra)
        return context

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        shift = resolve_cashier_shift(self.actor, kwargs["pk"])
        return render(request, "sales/cashier_shift_detail.html", self._context(shift, request))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        shift = resolve_cashier_shift(self.actor, kwargs["pk"])
        require_branch_permission(self.actor, CLOSE_CASHIER_SHIFT, shift.branch)

        form = TenderCountForm(request.POST, shift=shift)
        if form.is_valid():
            data = form.cleaned_data
            try:
                set_tender_count(
                    shift=shift,
                    tender=data["tender"],
                    counted_amount=data["counted_amount"],
                    actor=self.actor,
                    notes=data.get("notes", ""),
                )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تم تسجيل العدّ."))
                return HttpResponseRedirect(reverse("sales:shift_detail", args=[shift.pk]))
        return render(
            request,
            "sales/cashier_shift_detail.html",
            self._context(shift, request, count_form=form),
        )


class CashierShiftTransitionView(InventoryViewMixin, View):
    """
    Close, reopen, approve and reverse — one view, four transitions.

    One view because the shape is identical: resolve the shift with the caller,
    check the authority the *specific* transition needs, call the service, turn
    a `ValidationError` into a message. Four copies would be four chances to
    check the wrong permission, and the three permissions here are genuinely
    different.

    `close` is the one that carries a form, because it has to name the posted
    day the drawer is reconciled against. The other three carry a reason or
    nothing.
    """

    module_key = "sales"
    required_permission = VIEW_SALES
    action: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        shift = resolve_cashier_shift(self.actor, kwargs["pk"])
        reason = request.POST.get("reason", "").strip()

        try:
            if self.action == "close":
                require_branch_permission(self.actor, CLOSE_CASHIER_SHIFT, shift.branch)
                form = CashierShiftCloseForm(request.POST, shift=shift)
                if not form.is_valid():
                    # The commonest case by far is a day that has not posted
                    # yet, which leaves the selector empty. Saying so is more
                    # use than re-rendering a form with a blank dropdown.
                    raise ValidationError(
                        _(
                            "Name the posted sales day this drawer is reconciled against. "
                            "A draft day cannot be used."
                        ),
                        code="day_not_posted",
                    )
                close_cashier_shift(
                    shift=shift,
                    sales_day=form.cleaned_data["sales_day"],
                    actor=self.actor,
                    notes=form.cleaned_data.get("notes", ""),
                )
                messages.success(request, _("تم إقفال الصندوق."))
            elif self.action == "reopen":
                require_branch_permission(self.actor, CLOSE_CASHIER_SHIFT, shift.branch)
                reopen_cashier_shift(shift=shift, actor=self.actor, reason=reason)
                messages.success(request, _("أُعيد فتح الصندوق."))
            elif self.action == "approve":
                require_branch_permission(self.actor, APPROVE_CASHIER_CLOSING, shift.branch)
                approve_cashier_shift(shift=shift, actor=self.actor)
                messages.success(request, _("تم اعتماد الإقفال."))
            elif self.action == "reverse":
                require_organization_permission(self.actor, REVERSE_DAILY_SALES, shift.organization)
                reverse_cashier_shift(shift=shift, actor=self.actor, reason=reason)
                messages.success(request, _("تم عكس الإقفال."))
            else:  # pragma: no cover - a routing mistake, not a state
                raise ValidationError(_("Unknown transition."), code="unknown_action")
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        return HttpResponseRedirect(reverse("sales:shift_detail", args=[shift.pk]))


__all__ = [
    "CashierShiftCreateView",
    "CashierShiftDetailView",
    "CashierShiftListView",
    "CashierShiftTransitionView",
]
