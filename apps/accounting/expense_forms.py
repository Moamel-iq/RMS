"""
Forms for المصروفات.

The pay-from field is a **single choice across both kinds** rather than two
dropdowns and a radio button: exactly one source is allowed, and offering two
fields would let an operator fill both and be told off afterwards. The value is
encoded `cashbox:<id>` / `bank:<id>` and decoded in `clean`, so the "exactly
one" rule is a property of the widget rather than a check that can be forgotten.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounting.expense_services import EXPENSE_LINE_CLASSES
from apps.accounting.models import (
    Account,
    BankAccount,
    Cashbox,
    CostCenter,
    PaymentSource,
)
from apps.accounting.permissions import MANAGE_EXPENSE_VOUCHERS
from apps.core.money import MONEY_PLACES
from apps.organizations.authorization import branches_with_permission
from apps.organizations.models import Branch
from apps.users.models import User


def _payment_choices(organization_ids: list[int]) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    for cashbox in Cashbox.objects.filter(
        organization_id__in=organization_ids, is_active=True
    ).order_by("code"):
        choices.append((f"cashbox:{cashbox.pk}", f"صندوق — {cashbox.name}"))
    for bank in BankAccount.objects.filter(
        organization_id__in=organization_ids, is_active=True
    ).order_by("code"):
        choices.append((f"bank:{bank.pk}", f"مصرف — {bank.name}"))
    return choices


class ExpenseVoucherForm(forms.Form):
    """
    Open a voucher header.

    No supplier field and no tax field, and both absences are the design
    (ADR-030 §3). A voucher that can name a supplier becomes a supplier invoice
    with none of Procurement's controls, and it will be used as one because it
    is faster.
    """

    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    business_date = forms.DateField(
        label=_("تاريخ العملية"), widget=forms.DateInput(attrs={"type": "date"})
    )
    expense_date = forms.DateField(
        label=_("تاريخ المصروف"), widget=forms.DateInput(attrs={"type": "date"})
    )
    paid_from = forms.ChoiceField(label=_("مصدر الدفع"), choices=[])
    beneficiary = forms.CharField(label=_("المستفيد"), max_length=200)
    reason = forms.CharField(label=_("السبب"), widget=forms.Textarea(attrs={"rows": 2}))
    evidence_reference = forms.CharField(label=_("المستند/الإيصال"), max_length=200, required=False)
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        self.actor = actor
        super().__init__(*args, **kwargs)
        branches = branches_with_permission(actor, MANAGE_EXPENSE_VOUCHERS).order_by("code")
        organization_ids = list(branches.values_list("organization_id", flat=True))
        self.fields["branch"].queryset = branches  # type: ignore[attr-defined]
        self.fields["paid_from"].choices = _payment_choices(organization_ids)  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        raw = str(cleaned.get("paid_from") or "")
        kind, _sep, identifier = raw.partition(":")
        branch = cleaned.get("branch")
        if kind == "cashbox" and identifier.isdigit():
            cashbox = Cashbox.objects.filter(pk=int(identifier), is_active=True).first()
            if cashbox is None or (branch and cashbox.organization_id != branch.organization_id):
                self.add_error("paid_from", _("الصندوق غير متاح."))
            cleaned["payment_source"] = PaymentSource.CASHBOX
            cleaned["cashbox"] = cashbox
            cleaned["bank_account"] = None
        elif kind == "bank" and identifier.isdigit():
            bank = BankAccount.objects.filter(pk=int(identifier), is_active=True).first()
            if bank is None or (branch and bank.organization_id != branch.organization_id):
                self.add_error("paid_from", _("الحساب البنكي غير متاح."))
            cleaned["payment_source"] = PaymentSource.BANK
            cleaned["bank_account"] = bank
            cleaned["cashbox"] = None
        else:
            self.add_error("paid_from", _("اختر مصدر الدفع."))

        starts = cleaned.get("expense_date")
        business = cleaned.get("business_date")
        if starts and business and starts > business:
            self.add_error("expense_date", _("تاريخ المصروف بعد تاريخ العملية."))
        return cleaned


class ExpenseLineForm(forms.Form):
    """One expense account and what was spent on it."""

    account = forms.ModelChoiceField(queryset=Account.objects.none(), label=_("الحساب"))
    cost_center = forms.ModelChoiceField(
        queryset=CostCenter.objects.none(), label=_("مركز الكلفة"), required=False
    )
    description = forms.CharField(label=_("البيان"), max_length=255, required=False)
    amount = forms.DecimalField(
        label=_("المبلغ"), max_digits=18, decimal_places=MONEY_PLACES, min_value=Decimal("0.001")
    )

    def __init__(self, *args: Any, voucher: Any, **kwargs: Any) -> None:
        self.voucher = voucher
        super().__init__(*args, **kwargs)
        # The voucher's own organization, not everything the caller reaches: a
        # line from elsewhere would be refused by the posting validators anyway,
        # and offering it invites somebody to try.
        payment_account_id = (
            voucher.payment_account.pk if voucher.payment_account is not None else None
        )
        accounts = Account.objects.filter(
            organization_id=voucher.organization_id,
            is_postable=True,
            is_active=True,
            account_class__in=[value.value for value in EXPENSE_LINE_CLASSES],
        ).order_by("code")
        if payment_account_id is not None:
            accounts = accounts.exclude(pk=payment_account_id)
        self.fields["account"].queryset = accounts  # type: ignore[attr-defined]
        self.fields["cost_center"].queryset = CostCenter.objects.filter(  # type: ignore[attr-defined]
            organization_id=voucher.organization_id, is_active=True
        ).order_by("code")

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        account = cleaned.get("account")
        if account is not None and account.requires_cost_center and not cleaned.get("cost_center"):
            self.add_error("cost_center", _("هذا الحساب يتطلب مركز كلفة."))
        return cleaned


__all__ = ["ExpenseLineForm", "ExpenseVoucherForm"]
