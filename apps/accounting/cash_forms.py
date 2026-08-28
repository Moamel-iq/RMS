"""
Forms for the cashbox and bank-account screens.

Thin, and not the mutation path: the POST goes to `apps/accounting/commands.py`,
which checks organization authority and calls `cash_services`. Every queryset is
narrowed to what the caller already manages, because a dropdown offering a
foreign organization's account is a tenancy hole wearing a `<select>`.

The **account is on the create form and not on the edit form**, deliberately. A
cashbox that changed account would silently re-attribute every statement it has
ever shown — the same drawer, a different history. Leaving the field off the
edit form means nobody is invited to try, and the service refuses it anyway.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import Account, AccountClass, BankAccount, Cashbox
from apps.accounting.permissions import MANAGE_BANK_ACCOUNTS, MANAGE_CASHBOXES
from apps.organizations.authorization import organizations_with_permission
from apps.organizations.models import Branch, Organization
from apps.users.models import User


def _assignable_accounts(organizations: Any) -> Any:
    """
    Postable, active asset accounts not already backing an active cash record.

    Filtered in the queryset rather than validated afterwards, so the operator
    never picks something that will be refused. The service checks the same
    facts again — a form is a convenience and a hand-made POST must still be
    refused on its merits.
    """
    taken = set(Cashbox.objects.filter(is_active=True).values_list("account_id", flat=True)) | set(
        BankAccount.objects.filter(is_active=True).values_list("account_id", flat=True)
    )
    return (
        Account.objects.filter(
            organization__in=organizations,
            is_postable=True,
            is_active=True,
            account_class=AccountClass.ASSET,
        )
        .exclude(pk__in=taken)
        .select_related("organization")
        .order_by("organization__code", "code")
    )


class CashboxForm(forms.Form):
    """Register a drawer: where it is, and which account its movements land in."""

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    account = forms.ModelChoiceField(queryset=Account.objects.none(), label=_("حساب النقدية"))
    code = forms.CharField(
        label=_("الرمز"), max_length=20, widget=forms.TextInput(attrs={"dir": "ltr"})
    )
    name = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    opened_on = forms.DateField(
        label=_("مستعمَل من"), widget=forms.DateInput(attrs={"type": "date"})
    )
    responsible_note = forms.CharField(label=_("المسؤول"), max_length=200, required=False)
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        self.actor = actor
        super().__init__(*args, **kwargs)
        organizations = organizations_with_permission(actor, MANAGE_CASHBOXES).order_by("code")
        self.fields["organization"].queryset = organizations  # type: ignore[attr-defined]
        self.fields["branch"].queryset = Branch.objects.filter(  # type: ignore[attr-defined]
            organization__in=organizations, is_active=True
        ).order_by("code")
        self.fields["account"].queryset = _assignable_accounts(organizations)  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        organization = cleaned.get("organization")
        branch = cleaned.get("branch")
        account = cleaned.get("account")
        if organization is not None and branch is not None:
            if branch.organization_id != organization.pk:
                self.add_error("branch", _("الفرع من مؤسسة أخرى."))
        if organization is not None and account is not None:
            if account.organization_id != organization.pk:
                self.add_error("account", _("الحساب من مؤسسة أخرى."))
        return cleaned


class CashboxMetadataForm(forms.Form):
    """What a registered drawer may still change. Not its account, not its branch."""

    name = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    responsible_note = forms.CharField(label=_("المسؤول"), max_length=200, required=False)
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    reason = forms.CharField(label=_("السبب"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        self.actor = actor
        self.instance = instance
        super().__init__(*args, **kwargs)


class BankAccountForm(forms.Form):
    """Register a bank account. The number is masked on the way in, never stored whole."""

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.none(),
        label=_("الفرع"),
        required=False,
        help_text=_("اتركه فارغاً إذا كان الحساب على مستوى المؤسسة."),
    )
    account = forms.ModelChoiceField(queryset=Account.objects.none(), label=_("الحساب المحاسبي"))
    code = forms.CharField(
        label=_("الرمز"), max_length=20, widget=forms.TextInput(attrs={"dir": "ltr"})
    )
    bank_name = forms.CharField(label=_("المصرف"), max_length=200)
    name = forms.CharField(label=_("اسم الحساب بالعربية"), max_length=200)
    masked_account_number = forms.CharField(
        label=_("رقم الحساب"),
        max_length=40,
        help_text=_("يُخزَّن مقنَّعاً: آخر أربعة أرقام فقط."),
        widget=forms.TextInput(attrs={"dir": "ltr"}),
    )
    iban = forms.CharField(
        label=_("IBAN"), max_length=34, required=False, widget=forms.TextInput(attrs={"dir": "ltr"})
    )
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        self.actor = actor
        super().__init__(*args, **kwargs)
        organizations = organizations_with_permission(actor, MANAGE_BANK_ACCOUNTS).order_by("code")
        self.fields["organization"].queryset = organizations  # type: ignore[attr-defined]
        self.fields["branch"].queryset = Branch.objects.filter(  # type: ignore[attr-defined]
            organization__in=organizations, is_active=True
        ).order_by("code")
        self.fields["account"].queryset = _assignable_accounts(organizations)  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        organization = cleaned.get("organization")
        branch = cleaned.get("branch")
        account = cleaned.get("account")
        if organization is not None and branch is not None:
            if branch.organization_id != organization.pk:
                self.add_error("branch", _("الفرع من مؤسسة أخرى."))
        if organization is not None and account is not None:
            if account.organization_id != organization.pk:
                self.add_error("account", _("الحساب من مؤسسة أخرى."))
        return cleaned


class BankAccountMetadataForm(forms.Form):
    """What a registered bank account may still change. Not its GL account."""

    bank_name = forms.CharField(label=_("المصرف"), max_length=200)
    name = forms.CharField(label=_("اسم الحساب بالعربية"), max_length=200)
    masked_account_number = forms.CharField(
        label=_("رقم الحساب"), max_length=40, widget=forms.TextInput(attrs={"dir": "ltr"})
    )
    iban = forms.CharField(
        label=_("IBAN"), max_length=34, required=False, widget=forms.TextInput(attrs={"dir": "ltr"})
    )
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    reason = forms.CharField(label=_("السبب"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        self.actor = actor
        self.instance = instance
        super().__init__(*args, **kwargs)


__all__ = [
    "BankAccountForm",
    "BankAccountMetadataForm",
    "CashboxForm",
    "CashboxMetadataForm",
]
