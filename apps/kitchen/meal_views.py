"""
The staff-meal and complimentary-meal screens.

One set of views serving two meal types, parameterised by `meal_type`, because
they are the same document with a different reason on it. Two copies would
drift the first time one of them gained a column.

**Nothing on these screens posts stock or writes a journal**, and every one of
them says so in a sentence rather than leaving the operator to notice the
absence. A report labelled "staff meals" showing quantities and no money, in a
system that has money, is otherwise read as "staff meals cost nothing"
(RCP-108). The deferred accounting reclassification is named on the page for
the same reason.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.core.models import AuditEvent
from apps.kitchen.forms import ScopedForm
from apps.kitchen.meals import cancel_meal, record_meal
from apps.kitchen.models import (
    MealRecord,
    MealRecordStatus,
    MealType,
    Recipe,
    RecipeServing,
)
from apps.kitchen.permissions import RECORD_MEAL, VIEW_KITCHEN_REPORT
from apps.kitchen.selectors import (
    recordable_branches,
    resolve_meal_record,
    visible_meal_records,
    visible_recipes,
)
from apps.kitchen.views import KitchenViewMixin
from apps.organizations.authorization import (
    has_branch_permission,
    require_branch_permission,
)
from apps.organizations.models import Branch

#: The sentence every meal surface carries. Stated once here so the screens, the
#: detail page and the cancellation confirmation cannot drift apart on it.
MEAL_STATEMENT = _(
    "سجل الوجبات يفسّر الحصص المستهلكة ولا يحرّك مخزوناً ولا يكتب قيداً محاسبياً — "
    "المواد خرجت أصلاً بالإنتاج أو بالصرف. إعادة تصنيف كلفة وجبات الموظفين إلى "
    "حساب مزايا الموظفين مؤجلة حتى تُعتمد سياسة محاسبية منفصلة."
)


class MealRecordForm(ScopedForm):
    """
    Recording one meal. Every input explicit, nothing defaulted.

    There is no default date: "today" is the wrong answer for a meal recorded
    on Monday for Sunday's evening shift, and the whole point of the date is
    that it decides which recipe version applies.
    """

    scope_permission = RECORD_MEAL

    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    recipe = forms.ModelChoiceField(queryset=Recipe.objects.none(), label=_("الوصفة"))
    serving = forms.ModelChoiceField(
        queryset=RecipeServing.objects.none(), label=_("الحصة"), required=False
    )
    consumed_on = forms.DateField(label=_("تاريخ الاستهلاك"))
    quantity = forms.DecimalField(label=_("عدد الحصص"), min_value=Decimal("0"), decimal_places=6)
    beneficiary = forms.CharField(label=_("المستفيد أو الوردية"), max_length=200, required=False)
    reason = forms.CharField(label=_("السبب"), max_length=200, required=False)
    notes = forms.CharField(
        label=_("ملاحظات"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def __init__(self, *args: Any, actor: Any, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["branch"].queryset = recordable_branches(actor)  # type: ignore[attr-defined]
        self.fields["recipe"].queryset = visible_recipes(actor).filter(  # type: ignore[attr-defined]
            is_active=True
        )
        self.fields["serving"].queryset = RecipeServing.objects.filter(  # type: ignore[attr-defined]
            version__recipe__organization_id__in=visible_recipes(actor).values_list(
                "organization_id", flat=True
            )
        ).select_related("version", "serving_unit")

    def clean_quantity(self) -> Decimal:
        quantity: Decimal = self.cleaned_data["quantity"]
        if quantity <= Decimal("0"):
            raise forms.ValidationError(
                _("عدد الحصص يجب أن يكون أكبر من صفر."), code="meal_quantity_not_positive"
            )
        return quantity


class MealCancelForm(ScopedForm):
    """Cancelling a record. The reason is required and is kept forever."""

    scope_permission = RECORD_MEAL

    reason = forms.CharField(
        label=_("سبب الإلغاء"), widget=forms.Textarea(attrs={"rows": 2}), max_length=200
    )

    def clean_reason(self) -> str:
        reason: str = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise forms.ValidationError(_("الإلغاء يحتاج سبباً."), code="reason_required")
        return reason


class MealViewMixin(KitchenViewMixin):
    """Signed in, and holding the kitchen report grant to read a meal at all."""

    required_permission = VIEW_KITCHEN_REPORT
    #: Set by the URL conf, so one view class serves both meal types.
    meal_type: str = MealType.STAFF

    @property
    def type_label(self) -> Any:
        return dict(MealType.choices)[self.meal_type]

    def _base(self) -> str:
        return "kitchen/_bare.html" if self.is_htmx() else "shell.html"

    def is_htmx(self) -> bool:
        return self.request.headers.get("HX-Request") == "true"

    def _can_record(self) -> bool:
        """Whether the create control renders at all — absent, never disabled."""
        return recordable_branches(self.actor).exists()


class MealListView(MealViewMixin, View):
    """سجل وجبات الموظفين / سجل الوجبات المجانية."""

    template_name = "kitchen/meal_list.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        rows = visible_meal_records(self.actor).filter(meal_type=self.meal_type)
        status = request.GET.get("status", "").strip()
        if status in MealRecordStatus.values:
            rows = rows.filter(status=status)
        date_from = request.GET.get("date_from", "").strip()
        date_to = request.GET.get("date_to", "").strip()
        for raw, lookup in ((date_from, "gte"), (date_to, "lte")):
            if not raw:
                continue
            try:
                rows = rows.filter(**{f"consumed_on__{lookup}": datetime.date.fromisoformat(raw)})
            except ValueError:
                # A malformed date is not worth a 400: the filter does not
                # apply and the field shows what was typed.
                continue

        paginator = Paginator(rows, 50)
        page = paginator.get_page(request.GET.get("page"))
        return render(
            request,
            self.template_name,
            {
                "base_template": self._base(),
                "page_title": self.type_label,
                "meal_statement": MEAL_STATEMENT,
                "meal_type": self.meal_type,
                "rows": page.object_list,
                "page_obj": page,
                "is_paginated": page.has_other_pages(),
                "paginator": paginator,
                "total_rows": paginator.count,
                "statuses": MealRecordStatus.choices,
                "filters": {"status": status, "date_from": date_from, "date_to": date_to},
                "can_record": self._can_record(),
                "create_url": reverse(
                    "kitchen:meal_create_staff"
                    if self.meal_type == MealType.STAFF
                    else "kitchen:meal_create_complimentary"
                ),
            },
        )


class MealDetailView(MealViewMixin, View):
    """One meal, with the exact version it was resolved against."""

    template_name = "kitchen/meal_detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        record = resolve_meal_record(self.actor, kwargs["pk"])
        return render(
            request,
            self.template_name,
            {
                "base_template": self._base(),
                "page_title": _("تفاصيل الوجبة"),
                "meal_statement": MEAL_STATEMENT,
                "record": record,
                "can_record": has_branch_permission(self.actor, RECORD_MEAL, record.branch),
                "timeline": AuditEvent.objects.filter(
                    target_type="kitchen.MealRecord", target_id=str(record.pk)
                ).order_by("-occurred_at")[:50],
            },
        )


class MealCreateView(MealViewMixin, View):
    """تسجيل وجبة موظف / تسجيل وجبة مجانية."""

    template_name = "kitchen/meal_form.html"

    def _render(self, request: HttpRequest, form: MealRecordForm) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "base_template": self._base(),
                "fragment_base_template": self._base(),
                "page_title": _("تسجيل وجبة"),
                "meal_statement": MEAL_STATEMENT,
                "meal_type": self.meal_type,
                "type_label": self.type_label,
                "form": form,
            },
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._render(request, MealRecordForm(actor=self.actor))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = MealRecordForm(request.POST, actor=self.actor)
        if not form.is_valid():
            return self._render(request, form)

        branch = form.cleaned_data["branch"]
        # Checked on POST as well as on GET, and at **this** branch: a
        # hand-made request from somebody who never saw the form reaches
        # exactly this line.
        require_branch_permission(self.actor, RECORD_MEAL, branch)
        recipe: Recipe = form.cleaned_data["recipe"]
        try:
            record = record_meal(
                branch=branch,
                recipe=recipe,
                meal_type=self.meal_type,
                consumed_on=form.cleaned_data["consumed_on"],
                quantity=form.cleaned_data["quantity"],
                serving=form.cleaned_data.get("serving"),
                beneficiary=form.cleaned_data.get("beneficiary", ""),
                reason=form.cleaned_data.get("reason", ""),
                notes=form.cleaned_data.get("notes", ""),
                idempotency_key=f"screen-meal:{self.actor.pk}:{form.cleaned_data['consumed_on']}"
                f":{recipe.pk}:{self.meal_type}:{form.cleaned_data['quantity']}",
                actor=self.actor,
            )
        except ValidationError as refusal:
            form.add_error(None, refusal)
            return self._render(request, form)

        messages.success(request, _("تم تسجيل الوجبة."))
        target = reverse("kitchen:meal_detail", args=[record.pk])
        if self.is_htmx():
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        return HttpResponseRedirect(target)


class MealCancelView(MealViewMixin, View):
    """
    Cancel a recorded meal.

    Cancellation undoes nothing in either ledger, because recording moved
    nothing. What it changes is that the row stops contributing to theoretical
    consumption — and it stays visible in history, because a correction that
    hides what it corrected is not a correction.
    """

    template_name = "kitchen/meal_cancel.html"

    def _render(
        self, request: HttpRequest, record: MealRecord, form: MealCancelForm
    ) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "base_template": self._base(),
                "fragment_base_template": self._base(),
                "page_title": _("إلغاء سجل وجبة"),
                "meal_statement": MEAL_STATEMENT,
                "record": record,
                "form": form,
            },
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        record = resolve_meal_record(self.actor, kwargs["pk"])
        require_branch_permission(self.actor, RECORD_MEAL, record.branch)
        return self._render(request, record, MealCancelForm(actor=self.actor))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        record = resolve_meal_record(self.actor, kwargs["pk"])
        require_branch_permission(self.actor, RECORD_MEAL, record.branch)
        form = MealCancelForm(request.POST, actor=self.actor)
        if not form.is_valid():
            return self._render(request, record, form)
        try:
            cancel_meal(record=record, reason=form.cleaned_data["reason"], actor=self.actor)
        except ValidationError as refusal:
            form.add_error(None, refusal)
            return self._render(request, record, form)

        messages.success(request, _("تم إلغاء السجل."))
        target = reverse("kitchen:meal_detail", args=[record.pk])
        if self.is_htmx():
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        return HttpResponseRedirect(target)


__all__ = [
    "MEAL_STATEMENT",
    "MealCancelView",
    "MealCreateView",
    "MealDetailView",
    "MealListView",
]
