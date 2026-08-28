"""Forms for المستحقات والمقدمات."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounting.deferral_services import EXPENSE_CLASSES
from apps.accounting.models import (
    Account,
    AccountClass,
    AmortizationFrequency,
    BankAccount,
    Cashbox,
    CostCenter,
    PaymentSource,
)
from apps.accounting.permissions import MANAGE_ACCRUALS, MANAGE_PREPAYMENTS
from apps.core.money import MONEY_PLACES
from apps.organizations.authorization import organizations_with_permission
from apps.organizations.models import Branch
from apps.users.models import User

_EXPENSE_CLASS_VALUES = [value.value for value in EXPENSE_CLASSES]


def _branches(actor: User, permission: str) -> Any:
    return Branch.objects.filter(
        organization__in=organizations_with_permission(actor, permission), is_active=True
    ).order_by("code")


class AccrualForm(forms.Form):
    """Open an accrual header. Lines are added on its own page."""

    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    business_date = forms.DateField(
        label=_("تاريخ العملية"), widget=forms.DateInput(attrs={"type": "date"})
    )
    description = forms.CharField(label=_("الوصف"), max_length=255)
    reason = forms.CharField(
        label=_("السبب"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    auto_reverse_on = forms.DateField(
        label=_("تاريخ العكس التلقائي"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("الحالة الشائعة: أول يوم في الفترة التالية."),
    )
    evidence_reference = forms.CharField(label=_("المستند"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        self.actor = actor
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = _branches(actor, MANAGE_ACCRUALS)  # type: ignore[attr-defined]


class AccrualLineForm(forms.Form):
    """One expense account accrued, and how much."""

    account = forms.ModelChoiceField(queryset=Account.objects.none(), label=_("حساب المصروف"))
    cost_center = forms.ModelChoiceField(
        queryset=CostCenter.objects.none(), label=_("مركز الكلفة"), required=False
    )
    description = forms.CharField(label=_("البيان"), max_length=255, required=False)
    amount = forms.DecimalField(
        label=_("المبلغ"), max_digits=18, decimal_places=MONEY_PLACES, min_value=Decimal("0.001")
    )

    def __init__(self, *args: Any, accrual: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(  # type: ignore[attr-defined]
            organization_id=accrual.organization_id,
            is_postable=True,
            is_active=True,
            account_class__in=_EXPENSE_CLASS_VALUES,
        ).order_by("code")
        self.fields["cost_center"].queryset = CostCenter.objects.filter(  # type: ignore[attr-defined]
            organization_id=accrual.organization_id, is_active=True
        ).order_by("code")


class PrepaymentForm(forms.Form):
    """
    Open a prepayment and its schedule in one step.

    The schedule is built by the service from the total and the period count,
    never typed in: a hand-entered schedule is the one place the sum could stop
    equalling the total, and that residual is what stops the prepaid account
    ever reaching zero.
    """

    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    business_date = forms.DateField(
        label=_("تاريخ العملية"), widget=forms.DateInput(attrs={"type": "date"})
    )
    description = forms.CharField(label=_("الوصف"), max_length=255)
    total_amount = forms.DecimalField(
        label=_("المبلغ الكلي"),
        max_digits=18,
        decimal_places=MONEY_PLACES,
        min_value=Decimal("0.001"),
    )
    start_date = forms.DateField(
        label=_("بداية الاستهلاك"), widget=forms.DateInput(attrs={"type": "date"})
    )
    frequency = forms.ChoiceField(
        label=_("التواتر"),
        choices=AmortizationFrequency.choices,
        initial=AmortizationFrequency.MONTHLY,
    )
    period_count = forms.IntegerField(label=_("عدد الفترات"), min_value=1, max_value=120)
    expense_account = forms.ModelChoiceField(
        queryset=Account.objects.none(), label=_("حساب المصروف")
    )
    prepaid_account = forms.ModelChoiceField(
        queryset=Account.objects.none(), label=_("حساب المصروف المدفوع مقدماً")
    )
    cost_center = forms.ModelChoiceField(
        queryset=CostCenter.objects.none(), label=_("مركز الكلفة"), required=False
    )
    paid_from = forms.ChoiceField(label=_("مصدر الدفع"), choices=[])
    source_reference = forms.CharField(label=_("المستند"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        self.actor = actor
        super().__init__(*args, **kwargs)
        organizations = organizations_with_permission(actor, MANAGE_PREPAYMENTS)
        organization_ids = list(organizations.values_list("id", flat=True))
        self.fields["branch"].queryset = _branches(actor, MANAGE_PREPAYMENTS)  # type: ignore[attr-defined]
        self.fields["expense_account"].queryset = Account.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids,
            is_postable=True,
            is_active=True,
            account_class__in=_EXPENSE_CLASS_VALUES,
        ).order_by("code")
        self.fields["prepaid_account"].queryset = Account.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids,
            is_postable=True,
            is_active=True,
            account_class=AccountClass.ASSET,
        ).order_by("code")
        self.fields["cost_center"].queryset = CostCenter.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True
        ).order_by("code")

        choices: list[tuple[str, str]] = []
        for cashbox in Cashbox.objects.filter(
            organization_id__in=organization_ids, is_active=True
        ).order_by("code"):
            choices.append((f"cashbox:{cashbox.pk}", f"صندوق — {cashbox.name}"))
        for bank in BankAccount.objects.filter(
            organization_id__in=organization_ids, is_active=True
        ).order_by("code"):
            choices.append((f"bank:{bank.pk}", f"مصرف — {bank.name}"))
        self.fields["paid_from"].choices = choices  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        raw = str(cleaned.get("paid_from") or "")
        kind, _sep, identifier = raw.partition(":")
        if kind == "cashbox" and identifier.isdigit():
            cleaned["payment_source"] = PaymentSource.CASHBOX
            cleaned["cashbox"] = Cashbox.objects.filter(pk=int(identifier)).first()
            cleaned["bank_account"] = None
        elif kind == "bank" and identifier.isdigit():
            cleaned["payment_source"] = PaymentSource.BANK
            cleaned["bank_account"] = BankAccount.objects.filter(pk=int(identifier)).first()
            cleaned["cashbox"] = None
        else:
            self.add_error("paid_from", _("اختر مصدر الدفع."))

        expense = cleaned.get("expense_account")
        prepaid = cleaned.get("prepaid_account")
        if expense is not None and prepaid is not None and expense.pk == prepaid.pk:
            # Dr and Cr on one account nets to nothing: the amortization would
            # balance and release nothing.
            self.add_error("prepaid_account", _("حساب المصروف وحساب المقدَّم لا يكونان واحداً."))
        return cleaned


class ReasonForm(forms.Form):
    reason = forms.CharField(label=_("السبب"), max_length=200, required=False)


__all__ = ["AccrualForm", "AccrualLineForm", "PrepaymentForm", "ReasonForm"]
