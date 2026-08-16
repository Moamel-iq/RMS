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

from apps.accounting.models import Account, AccountRole, OrganizationAccountMapping
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


class AmendMappingForm(forms.Form):
    """
    Correct a mapping nothing has posted through yet.

    Every field is optional because amending is a partial correction: the
    common case is fixing one mistyped date, and requiring the account to be
    restated would invite somebody to restate it wrongly. The service refuses
    the whole thing if any posting has already snapshotted the row — a used
    mapping is history, and the path for one is close-then-create.
    """

    account = forms.ModelChoiceField(
        queryset=Account.objects.none(), label=_("الحساب"), required=False
    )
    effective_from = forms.DateField(
        label=_("يبدأ في"), widget=forms.DateInput(attrs={"type": "date"}), required=False
    )
    effective_to = forms.DateField(
        label=_("ينتهي في"), widget=forms.DateInput(attrs={"type": "date"}), required=False
    )
    clear_effective_to = forms.BooleanField(label=_("إلغاء تاريخ الانتهاء"), required=False)
    reason = forms.CharField(label=_("السبب"), max_length=200, required=False)

    def __init__(self, *args: Any, mapping: OrganizationAccountMapping, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Only postable, active accounts of the mapping's **own** organization.
        # A queryset is the scope here: an id from elsewhere finds nothing
        # rather than being fetched and then checked.
        self.fields["account"].queryset = Account.objects.filter(  # type: ignore[attr-defined]
            organization=mapping.organization, is_postable=True, is_active=True
        ).order_by("code")

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        if cleaned.get("clear_effective_to") and cleaned.get("effective_to"):
            raise forms.ValidationError(
                _("اختر تاريخ انتهاء أو ألغِ التاريخ، لا الاثنين معاً."),
                code="effective_to_conflict",
            )
        return cleaned


def manageable_organizations(actor: User) -> QuerySet[Organization]:
    """Organizations whose mappings this caller may manage."""
    return organizations_with_permission(actor, MANAGE_ACCOUNT_MAPPINGS).order_by("code")
