"""
Forms for returns, cancellations and financial corrections.

Separate from `day_forms.py` for the reason those are separate from `forms.py`:
these build a document that takes value back out of the ledger, and the rules
that decide how much are not the rules that decide how much went in.

None of them computes anything. `adjustment_services.proportional_amounts` does
the arithmetic, because it can refuse in four distinct ways and each refusal has
a sentence an operator can act on — and because a form that computed would be a
second copy of the residual rule, which is exactly the rule that must not have
two implementations.

The line form asks for **either** a quantity or an amount, decided by the
header's reason kind rather than by the operator. A `FINANCIAL_CORRECTION` may
not touch quantity at all (ADR-028 §8, and a database trigger), so offering the
field would be offering a dead end; a cancellation or a return names a quantity,
and offering an amount beside it would invite two answers to one question.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.organizations.authorization import branches_with_permission
from apps.sales.adjustment_services import adjustable_lines
from apps.sales.models import (
    SalesAdjustment,
    SalesAdjustmentReasonKind,
    SalesDay,
    SalesDayLine,
    SalesDayStatus,
)
from apps.sales.permissions import MANAGE_SALES_ADJUSTMENTS

if TYPE_CHECKING:
    from apps.users.models import User


class SalesAdjustmentForm(forms.Form):
    """
    Open a correction against a posted day.

    The day choices are **posted days at branches the caller may adjust**, and
    both halves matter: a draft has nothing to take back, and a branch the
    caller cannot adjust is a branch whose corrections are none of their
    business.

    `reason` and `evidence_reference` are required here as well as by two check
    constraints. ADR-028 §8 asks for both by name, and a correction nobody
    explained and a correction nobody can point at are the same failure wearing
    different clothes.
    """

    sales_day = forms.ModelChoiceField(queryset=SalesDay.objects.none(), label=_("يوم المبيعات"))
    reason_kind = forms.ChoiceField(
        label=_("نوع التسوية"),
        choices=SalesAdjustmentReasonKind.choices,
        help_text=_(
            "الإلغاء قبل التنفيذ وحده يخفض الاستهلاك النظري: الطلب لم يُطبخ أصلاً. "
            "الإرجاع بعد التنفيذ لا يخفضه — الطعام طُبخ وخرجت مكوّناته — والتصحيح "
            "المالي لا يمسّ الكمية إطلاقاً."
        ),
    )
    business_date = forms.DateField(
        label=_("تاريخ التسوية"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("اليوم الذي تقرّرت فيه التسوية، لا اليوم الذي تصحّحه."),
    )
    reason = forms.CharField(
        label=_("السبب"),
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("إلزامي. نوع التسوية يقول أيّ حقيقة هذه؛ السبب يقول ماذا حدث."),
    )
    evidence_reference = forms.CharField(
        label=_("المستند"),
        max_length=200,
        help_text=_("إلزامي: رقم إشعار أو محضر أو مراسلة يمكن الرجوع إليها."),
    )
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, actor: User, instance: None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        days = (
            SalesDay.objects.filter(
                status=SalesDayStatus.POSTED,
                branch__in=branches_with_permission(actor, MANAGE_SALES_ADJUSTMENTS),
            )
            .select_related("branch")
            .order_by("-business_date", "branch__code")
        )
        self.fields["sales_day"].queryset = days  # type: ignore[attr-defined]
        self.fields["business_date"].initial = timezone.localdate()

    def clean_business_date(self) -> datetime.date:
        value: datetime.date = self.cleaned_data["business_date"]
        if value > timezone.localdate():
            raise forms.ValidationError(
                _("لا يمكن تسجيل تسوية في المستقبل."), code="future_business_date"
            )
        return value

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        day = data.get("sales_day")
        business_date = data.get("business_date")
        if day is not None and business_date is not None and business_date < day.business_date:
            # Re-checked in the service and in `0008`'s trigger. This is the one
            # the operator can read before anything is written.
            self.add_error("business_date", _("التسوية لا تسبق اليوم الذي تصحّحه."))
        return data


class AdjustmentLineForm(forms.Form):
    """
    Take back part of one posted line.

    Which of the two amount fields is shown depends on the header's reason kind,
    and the other is removed rather than disabled: a disabled field is still
    submitted, and the view would then have to remember to ignore it.
    """

    original_line = forms.ModelChoiceField(
        queryset=SalesDayLine.objects.none(), label=_("السطر الأصلي")
    )
    adjusted_quantity = forms.DecimalField(
        label=_("الكمية المرتجعة"),
        min_value=Decimal("0.001"),
        decimal_places=3,
        required=False,
        help_text=_("كمية عشرية: ٠٫٥٠٠ و١٫٠٠٠ و٢٫٥٠٠ كلها قيم عادية."),
    )
    adjusted_gross = forms.DecimalField(
        label=_("المبلغ المصحَّح"),
        min_value=Decimal("0.001"),
        decimal_places=3,
        required=False,
        help_text=_("جزء من إجمالي السطر الأصلي. لا يمسّ الكمية المباعة."),
    )
    line_reason = forms.CharField(label=_("سبب السطر"), max_length=300, required=False)

    def __init__(self, *args: Any, adjustment: SalesAdjustment, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.adjustment = adjustment
        self.is_correction = (
            adjustment.reason_kind == SalesAdjustmentReasonKind.FINANCIAL_CORRECTION
        )
        candidates = (
            SalesDayLine.objects.filter(
                pk__in=[line.pk for line in adjustable_lines(adjustment.sales_day)]
            )
            .select_related("menu_item", "channel")
            .order_by("sequence")
        )
        self.fields["original_line"].queryset = candidates  # type: ignore[attr-defined]
        if self.is_correction:
            del self.fields["adjusted_quantity"]
        else:
            del self.fields["adjusted_gross"]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if self.is_correction:
            if not data.get("adjusted_gross"):
                self.add_error("adjusted_gross", _("التصحيح المالي يحتاج مبلغاً."))
        elif not data.get("adjusted_quantity"):
            self.add_error("adjusted_quantity", _("الإلغاء أو الإرجاع يحتاج كمية."))
        return data


class AdjustmentReversalForm(forms.Form):
    """
    Undo a posted adjustment.

    A reason, and nothing else. Everything the reversal needs to know is already
    on the document it reverses, and asking for it again would invite a second
    answer.
    """

    reason = forms.CharField(
        label=_("سبب العكس"),
        max_length=300,
        help_text=_("يبقى الأصل ظاهراً. العكس قيد إضافي، لا تعديل ولا حذف."),
    )


__all__ = ["AdjustmentLineForm", "AdjustmentReversalForm", "SalesAdjustmentForm"]
