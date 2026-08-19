"""
Forms for the daily sales document.

Separate from `forms.py` for the reason `day_views.py` is separate from
`views.py`: those maintain masters, these build a document that will move
money.

None of them resolves anything. The **service** resolves — the recipe version,
the serving, the price, the agreement, the commission — because resolution can
fail in five distinct ways and each failure has a sentence an operator can act
on. A form that resolved would have to duplicate all five, and the duplicate
would drift.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.organizations.authorization import branches_with_permission
from apps.sales.day_services import applications_for, channels_for, sellable_items
from apps.sales.models import (
    DeliveryApplication,
    DiscountProgram,
    MenuItem,
    SalesChannel,
    SalesDay,
    TenderDestination,
)
from apps.sales.permissions import CREATE_DAILY_SALES

if TYPE_CHECKING:
    from apps.users.models import User


class SalesDayForm(forms.Form):
    """
    Open a branch's trading day.

    `business_date` defaults to today and is **editable**, because a day is
    routinely entered the morning after it traded. Defaulting to today and
    refusing to change it would force every late entry into the wrong date,
    which is the error this field exists to prevent.
    """

    branch = forms.ModelChoiceField(queryset=SalesDay.objects.none(), label=_("الفرع"))
    business_date = forms.DateField(
        label=_("تاريخ العمل"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("تاريخ العمل الذي تخصّه المبيعات، لا تاريخ الإدخال."),
    )
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, actor: User, instance: None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        self.fields["branch"].queryset = branches_with_permission(  # type: ignore[attr-defined]
            actor, CREATE_DAILY_SALES
        ).order_by("code")
        self.fields["business_date"].initial = timezone.localdate()

    def clean_business_date(self) -> datetime.date:
        value: datetime.date = self.cleaned_data["business_date"]
        if value > timezone.localdate():
            raise forms.ValidationError(
                _("لا يمكن فتح يوم مبيعات في المستقبل."), code="future_business_date"
            )
        return value


class SalesLineForm(forms.Form):
    """
    One line of the day's takings.

    The three narrowing selects — item, channel, application — are populated
    from what this branch can *actually* sell today: items it offers,
    applications it is live on. That is availability only; the service still
    refuses a line whose price or recipe version has lapsed, with a sentence
    that says which.
    """

    menu_item = forms.ModelChoiceField(queryset=MenuItem.objects.none(), label=_("الصنف"))
    channel = forms.ModelChoiceField(queryset=SalesChannel.objects.none(), label=_("القناة"))
    delivery_application = forms.ModelChoiceField(
        queryset=DeliveryApplication.objects.none(),
        label=_("تطبيق التوصيل"),
        required=False,
        help_text=_("إلزامي لقنوات تطبيقات التوصيل، وممنوع لغيرها."),
    )
    quantity = forms.DecimalField(
        label=_("الكمية"),
        min_value=Decimal("0.001"),
        decimal_places=3,
        help_text=_("كمية عشرية: ٠٫٥٠٠ و١٫٠٠٠ و٢٫٥٠٠ كلها قيم عادية."),
    )
    order_count = forms.IntegerField(
        label=_("عدد الطلبات"),
        min_value=0,
        required=False,
        initial=0,
        help_text=_("يُستخدم في الرسم الثابت لكل طلب. صفر يعني غير مذكور."),
    )
    discount_program = forms.ModelChoiceField(
        queryset=DiscountProgram.objects.none(), label=_("برنامج خصم"), required=False
    )
    manual_discount_amount = forms.DecimalField(
        label=_("خصم يدوي"), min_value=0, decimal_places=3, required=False
    )
    manual_discount_reason = forms.CharField(
        label=_("سبب الخصم اليدوي"),
        max_length=300,
        required=False,
        help_text=_("إلزامي مع أي خصم يدوي. الخصم الذي لم يشرحه أحد هو تجاوز صامت."),
    )
    other_fee_amount = forms.DecimalField(
        label=_("رسوم تطبيق أخرى"), min_value=0, decimal_places=3, required=False
    )
    notes = forms.CharField(label=_("ملاحظات"), max_length=300, required=False)

    def __init__(self, *args: Any, actor: User, day: SalesDay, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.day = day
        self.fields["menu_item"].queryset = MenuItem.objects.filter(  # type: ignore[attr-defined]
            pk__in=[row.pk for row in sellable_items(day)]
        ).order_by("display_order", "code")
        self.fields["channel"].queryset = SalesChannel.objects.filter(  # type: ignore[attr-defined]
            pk__in=[row.pk for row in channels_for(day)]
        ).order_by("display_order", "code")
        self.fields["delivery_application"].queryset = DeliveryApplication.objects.filter(  # type: ignore[attr-defined]
            pk__in=[row.pk for row in applications_for(day)]
        ).order_by("code")
        self.fields["discount_program"].queryset = DiscountProgram.objects.filter(  # type: ignore[attr-defined]
            organization_id=day.organization_id, is_active=True
        ).order_by("code")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        manual = data.get("manual_discount_amount")
        reason = (data.get("manual_discount_reason") or "").strip()
        if manual and not reason:
            self.add_error("manual_discount_reason", _("الخصم اليدوي يحتاج سبباً."))
        if manual and data.get("discount_program") is not None:
            self.add_error(
                "manual_discount_amount",
                _("السطر يأخذ برنامج خصم أو خصماً يدوياً، لا الاثنين."),
            )
        return data


class TenderSummaryForm(forms.Form):
    """
    What the day's takings were **declared** to be, per tender.

    Declared rather than derived. The derived figure is what the lines say;
    this is what the operator says, and a disagreement between them is the
    thing worth looking at before the day posts.
    """

    tender = forms.ChoiceField(label=_("وجهة التحصيل"), choices=TenderDestination.choices)
    declared_amount = forms.DecimalField(label=_("المبلغ المُعلن"), min_value=0, decimal_places=3)
    notes = forms.CharField(label=_("ملاحظات"), max_length=300, required=False)

    def __init__(self, *args: Any, day: SalesDay, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.day = day


__all__ = ["SalesDayForm", "SalesLineForm", "TenderSummaryForm"]
