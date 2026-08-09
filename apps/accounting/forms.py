"""
Forms for the account-mapping screens.

Thin, like the inventory forms, and not the mutation path: the POST goes to
`apps/accounting/commands.py`, which checks organization authority and calls
the kernel service. Every queryset is narrowed to what the caller already
manages, because a selector offering a foreign organization's account would
turn a dropdown into a tenancy hole.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import Account, AccountRole
from apps.accounting.permissions import MANAGE_ACCOUNT_MAPPINGS
from apps.organizations.authorization import organizations_with_permission
from apps.organizations.models import Organization
from apps.users.models import User


class AccountMappingForm(forms.Form):
    """
    Map a role to a postable account, from a date.

    The account list spans every organization the caller manages; the service
    re-checks that the chosen account belongs to the chosen organization, so a
    mismatched pair is refused with a readable error rather than trusted.
    """

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    account_role = forms.ModelChoiceField(
        queryset=AccountRole.objects.none(), label=_("الدور المحاسبي")
    )
    account = forms.ModelChoiceField(queryset=Account.objects.none(), label=_("الحساب"))
    effective_from = forms.DateField(
        label=_("سارٍ من"), widget=forms.DateInput(attrs={"type": "date"})
    )
    effective_to = forms.DateField(
        label=_("سارٍ حتى"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("اتركه فارغاً إذا كان سارياً حتى إشعار آخر."),
    )

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        self.actor = actor
        super().__init__(*args, **kwargs)
        organizations = organizations_with_permission(actor, MANAGE_ACCOUNT_MAPPINGS).order_by(
            "code"
        )
        self.fields["organization"].queryset = organizations  # type: ignore[attr-defined]
        self.fields["account_role"].queryset = AccountRole.objects.filter(  # type: ignore[attr-defined]
            is_active=True
        ).order_by("domain", "code")
        self.fields["account"].queryset = (  # type: ignore[attr-defined]
            Account.objects.filter(organization__in=organizations, is_postable=True, is_active=True)
            .select_related("organization")
            .order_by("organization__code", "code")
        )

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean()  # type: ignore[assignment]
        starts = cleaned.get("effective_from")
        ends = cleaned.get("effective_to")
        if starts and ends and ends < starts:
            self.add_error("effective_to", _("تاريخ الانتهاء قبل تاريخ البدء."))
        return cleaned


class CloseMappingForm(forms.Form):
    """End a mapping's effective range — the correction path for a used one."""

    effective_to = forms.DateField(
        label=_("ينتهي في"), widget=forms.DateInput(attrs={"type": "date"})
    )
    reason = forms.CharField(label=_("السبب"), max_length=200, required=False)


def manageable_organizations(actor: User) -> QuerySet[Organization]:
    """Organizations whose mappings this caller may manage."""
    return organizations_with_permission(actor, MANAGE_ACCOUNT_MAPPINGS).order_by("code")
