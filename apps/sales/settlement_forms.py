"""
Forms for application settlements.

Separate from `adjustment_forms.py` and `day_forms.py` for the reason those are
separate from `forms.py`: these build a document that *clears* a receivable
against a counterparty's own statement, and the rules that decide how much are
not the rules that decided how much was owed.

None of them computes anything. `settlement_services.three_way_for` does the
arithmetic, because it can refuse in ways each of which has a sentence an
operator can act on — and because a form that computed would be a second copy of
the two leg equations, which are exactly the rules that must not have two
implementations.

The three figures are asked for as three separate fields and are never offered
as one "net received", which is the same refusal ADR-028 §7 makes at the model:
an operator who could type one number would have already done the reconciliation
in their head, and the pattern of which two agree — the diagnosis — would never
reach the database.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.organizations.authorization import organizations_with_permission
from apps.organizations.models import Branch
from apps.sales.models import (
    ApplicationReceivableEntry,
    DeliveryApplication,
    DeliveryApplicationSettlement,
    SettlementAdjustmentReason,
    SettlementRemittance,
    SettlementVarianceLeg,
)
from apps.sales.permissions import MANAGE_APPLICATION_SETTLEMENTS
from apps.sales.settlement_services import open_entries_for

if TYPE_CHECKING:
    from apps.users.models import User


class SettlementForm(forms.Form):
    """
    Open a settlement against one statement.

    The branch and application choices are drawn from the organizations where
    the caller holds `manage_application_settlements`, which is
    `ORGANIZATION_AUTHORITY` — settling a contract is not something a branch
    membership grants, and offering a branch the caller cannot settle for would
    be offering a dead end.

    `statement_reference` is required here and unique per application at the
    database. Paying one statement twice is the failure that makes it so.
    """

    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    delivery_application = forms.ModelChoiceField(
        queryset=DeliveryApplication.objects.none(), label=_("تطبيق التوصيل")
    )
    period_start = forms.DateField(
        label=_("بداية الفترة"), widget=forms.DateInput(attrs={"type": "date"})
    )
    period_end = forms.DateField(
        label=_("نهاية الفترة"), widget=forms.DateInput(attrs={"type": "date"})
    )
    business_date = forms.DateField(
        label=_("تاريخ التحويل"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("اليوم الذي وصل فيه المبلغ فعلاً، لا نهاية الفترة."),
    )
    statement_reference = forms.CharField(
        label=_("رقم كشف الحساب"),
        max_length=200,
        help_text=_("رقم الكشف كما أصدره التطبيق. لا يتكرر لنفس التطبيق."),
    )
    statement_date = forms.DateField(
        label=_("تاريخ الكشف"), widget=forms.DateInput(attrs={"type": "date"})
    )
    statement_amount = forms.DecimalField(
        label=_("مبلغ الكشف"),
        min_value=Decimal("0"),
        decimal_places=3,
        help_text=_("ما يقوله كشف التطبيق أنه مستحق."),
    )
    remitted_amount = forms.DecimalField(
        label=_("المبلغ المحوَّل"),
        min_value=Decimal("0"),
        decimal_places=3,
        help_text=_("ما وصل فعلاً إلى المصرف أو الصندوق. الصفر قيمة مشروعة."),
    )
    statement_commission_amount = forms.DecimalField(
        label=_("عمولة الكشف"),
        min_value=Decimal("0"),
        decimal_places=3,
        initial=Decimal("0"),
        help_text=_(
            "للمقارنة فقط: العمولة استُحقّت عند البيع ولا تُسجَّل مصروفاً مرة ثانية. "
            "أي فرق بينها وبين المستحق هو فرق يُفسَّر، لا مصروف جديد."
        ),
    )
    remittance_destination = forms.ChoiceField(
        label=_("وجهة التحويل"),
        choices=SettlementRemittance.choices,
        initial=SettlementRemittance.BANK,
    )
    evidence_reference = forms.CharField(
        label=_("المستند"),
        max_length=200,
        help_text=_("إلزامي: إشعار مصرفي أو وصل قبض يمكن الرجوع إليه."),
    )
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, actor: User, instance: None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        organizations = organizations_with_permission(actor, MANAGE_APPLICATION_SETTLEMENTS)
        self.fields["branch"].queryset = (  # type: ignore[attr-defined]
            Branch.objects.filter(organization__in=organizations)
            .select_related("organization")
            .order_by("organization__code", "code")
        )
        self.fields["delivery_application"].queryset = (  # type: ignore[attr-defined]
            DeliveryApplication.objects.filter(organization__in=organizations, is_active=True)
            .select_related("organization")
            .order_by("organization__code", "code")
        )
        self.fields["business_date"].initial = timezone.localdate()

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        start = data.get("period_start")
        end = data.get("period_end")
        if start is not None and end is not None and end < start:
            self.add_error("period_end", _("نهاية الفترة قبل بدايتها."))

        branch = data.get("branch")
        application = data.get("delivery_application")
        if (
            branch is not None
            and application is not None
            and branch.organization_id != application.organization_id
        ):
            # Re-checked in the service. This is the one an operator reads
            # before anything is written.
            self.add_error("delivery_application", _("التطبيق والفرع ليسا في المنشأة نفسها."))
        return data

    def clean_business_date(self) -> datetime.date:
        value: datetime.date = self.cleaned_data["business_date"]
        if value > timezone.localdate():
            raise forms.ValidationError(
                _("لا يمكن تسجيل تحويل في المستقبل."), code="future_business_date"
            )
        return value


class SettlementAllocationForm(forms.Form):
    """
    Claim part of one posted receivable entry.

    The choices are the application's **debit** entries dated no later than the
    statement's `period_end`, which is exactly what `0010`'s containment trigger
    permits — a screen that offered more would be offering a row the database
    refuses.

    The amount is asked for rather than defaulted to the whole entry, because a
    statement routinely pays part of a day: one order disputed out of forty is
    the ordinary case, and a default of "all of it" would be a click away from
    claiming a settlement paid for something it did not.
    """

    receivable_entry = forms.ModelChoiceField(
        queryset=ApplicationReceivableEntry.objects.none(), label=_("حركة الذمة")
    )
    allocated_amount = forms.DecimalField(
        label=_("المبلغ المخصَّص"),
        min_value=Decimal("0.001"),
        decimal_places=3,
        help_text=_("لا يتجاوز ما تبقّى مفتوحاً من الحركة."),
    )

    def __init__(
        self, *args: Any, settlement: DeliveryApplicationSettlement, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.settlement = settlement
        self.fields["receivable_entry"].queryset = open_entries_for(  # type: ignore[attr-defined]
            delivery_application=settlement.delivery_application,
            organization_id=settlement.organization_id,
            up_to=settlement.period_end,
        )


class SettlementAdjustmentForm(forms.Form):
    """
    Claim part of one gap against a named reason.

    `leg` is asked for and never inferred. Which leg a contractual dispute lands
    on is a fact about the counterparty's statement layout, not about this
    software — an application that publishes commission net of promotions puts a
    rate difference on the remittance leg and one that publishes it gross puts
    the same difference on the statement leg — so guessing it would be this form
    asserting something about a company it has never read a contract from.

    `amount` is signed, and the help text says so out loud, because a positive
    figure meaning "we received less" is the one thing an operator will get
    backwards.
    """

    leg = forms.ChoiceField(
        label=_("الفرق"),
        choices=SettlementVarianceLeg.choices,
        help_text=_(
            "فرق كشف الحساب: بين المستحق لدينا وما يقوله الكشف. فرق التحويل: بين "
            "الكشف وما وصل فعلاً."
        ),
    )
    reason = forms.ChoiceField(label=_("السبب"), choices=SettlementAdjustmentReason.choices)
    amount = forms.DecimalField(
        label=_("المبلغ"),
        decimal_places=3,
        help_text=_("بإشارة: الموجب يعني أننا استلمنا أقل مما وعد الطرف السابق."),
    )
    explanation = forms.CharField(
        label=_("التفسير"),
        max_length=300,
        required=False,
        help_text=_("إلزامي مع «فرق غير مفسّر معتمد»."),
    )
    approve = forms.BooleanField(
        label=_("أعتمد هذا الفرق"),
        required=False,
        help_text=_("الفرق غير المفسّر يحتاج معتمداً بالاسم: الفرق يُعترف به حيث يُقرَّر، وبمن قرّره."),
    )

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("amount") == Decimal("0"):
            self.add_error("amount", _("مطالبة بصفر لا تفسّر شيئاً."))
        if data.get("reason") == SettlementAdjustmentReason.UNEXPLAINED_APPROVED:
            if not (data.get("explanation") or "").strip():
                self.add_error("explanation", _("الفرق غير المفسّر يحتاج تفسيراً مكتوباً."))
            if not data.get("approve"):
                self.add_error("approve", _("الفرق غير المفسّر يحتاج اعتماداً بالاسم."))
        return data


class SettlementReasonForm(forms.Form):
    """
    A typed reason, for the two transitions that need one.

    Returning a reconciled settlement to draft and reversing a posted one are
    different acts with different consequences, and both need somebody's words:
    a hidden field holding a constant would satisfy the constraint while telling
    the next reader nothing.
    """

    reason = forms.CharField(label=_("السبب"), max_length=300)


__all__ = [
    "SettlementAdjustmentForm",
    "SettlementAllocationForm",
    "SettlementForm",
    "SettlementReasonForm",
]
