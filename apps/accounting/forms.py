"""
Forms for the account-mapping screens.

Thin, like the inventory forms, and not the mutation path: the POST goes to
`apps/accounting/commands.py`, which checks organization authority and calls
the kernel service. Every queryset is narrowed to what the caller already
manages, because a selector offering a foreign organization's account would
turn a dropdown into a tenancy hole.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django import forms
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    Account,
    AccountRole,
    CostCenter,
    ManualPostingPolicy,
    OrganizationAccountMapping,
    PresentationSection,
    StatementGroup,
)
from apps.accounting.permissions import (
    CREATE_DRAFT,
    MANAGE_ACCOUNT_MAPPINGS,
    MANAGE_CHART_OF_ACCOUNTS,
)
from apps.core.money import MONEY_PLACES
from apps.organizations.authorization import (
    branches_with_permission,
    organizations_with_permission,
)
from apps.organizations.models import Branch, Organization
from apps.users.models import User

#: Arabic-Indic digits to ASCII. An account code is a technical identity and
#: must be locale-independent (CLAUDE.md), but an Arabic keyboard produces the
#: Arabic-Indic forms by default and they are visually unmistakable for the
#: ASCII ones in a short code.
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


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


class AccountForm(forms.Form):
    """
    A new account in one organization's chart.

    The code carries the level (ADR-014), so the form asks for a code and
    derives nothing: `C`, `C-GG`, `C-GG-SS`, `C-GG-SS-AAA`. The service works
    out the class, the parent and postability from it and refuses a code whose
    parent does not exist — which is why there is no parent field here. A
    parent dropdown would be a second way to say the same thing, and the two
    could disagree.
    """

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(
        label=_("الرمز"),
        max_length=20,
        help_text=_("الصيغة: ١ أو ١-٠١ أو ١-٠١-٠١ أو ١-٠١-٠١-٠٠١. الأخير وحده قابل للترحيل."),
        widget=forms.TextInput(attrs={"dir": "ltr", "placeholder": "1-01-01-001"}),
    )
    name = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    requires_cost_center = forms.BooleanField(
        label=_("يتطلب مركز كلفة"),
        required=False,
        help_text=_(
            "اتركه للنظام ما لم يكن هناك سبب: الإيرادات والكلفة والمصروفات تتطلبه تلقائياً."
        ),
    )
    manual_posting_policy = forms.ChoiceField(
        label=_("سياسة القيد اليدوي"),
        choices=ManualPostingPolicy.choices,
        initial=ManualPostingPolicy.ALLOWED,
    )

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        self.actor = actor
        super().__init__(*args, **kwargs)
        self.fields["organization"].queryset = organizations_with_permission(  # type: ignore[attr-defined]
            actor, MANAGE_CHART_OF_ACCOUNTS
        ).order_by("code")

    def clean_code(self) -> str:
        # Stored and compared as a locale-independent string. Arabic-Indic
        # digits typed into the field would pass a human's eye and fail every
        # regex the model applies, so they are translated here rather than
        # rejected with a message about characters nobody can see.
        raw = str(self.cleaned_data["code"]).strip()
        return raw.translate(_ARABIC_DIGITS)


class AccountMetadataForm(forms.Form):
    """
    What an account with journal history may still change.

    Only these four fields exist here, and that is the point: the code, the
    class, the parent and postability decide what the account *means*, and an
    account somebody has posted to cannot change its meaning without silently
    restating history. The service refuses those anyway; leaving them off the
    form means nobody is invited to try.
    """

    name = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    requires_cost_center = forms.BooleanField(label=_("يتطلب مركز كلفة"), required=False)
    manual_posting_policy = forms.ChoiceField(
        label=_("سياسة القيد اليدوي"), choices=ManualPostingPolicy.choices
    )
    reason = forms.CharField(label=_("السبب"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        self.actor = actor
        self.instance = instance
        super().__init__(*args, **kwargs)


class ReportMappingForm(forms.Form):
    """Which financial-statement group an account presents under (ADR-031)."""

    statement_group = forms.ChoiceField(
        label=_("مجموعة القوائم المالية"), choices=StatementGroup.choices
    )
    presentation_section = forms.ChoiceField(
        label=_("القسم"),
        choices=PresentationSection.choices,
        initial=PresentationSection.NOT_APPLICABLE,
        help_text=_("متداول أو غير متداول للأصول والالتزامات فقط."),
    )
    display_order = forms.IntegerField(label=_("ترتيب العرض"), initial=0, min_value=0)


class ReasonForm(forms.Form):
    """A stated reason, for the transitions that require one."""

    reason = forms.CharField(label=_("السبب"), max_length=200, required=False)


class JournalDraftForm(forms.Form):
    """
    Open a manual draft, with its first line.

    The first line is here rather than on the detail page because the kernel
    refuses an entry with no lines at all — so a draft has to be born with one.
    Everything after it is added on the detail page, where a line that fails to
    resolve can say why without losing the lines beside it.

    The branch decides the organization. Asking for both would let the two
    disagree, and the branch is the one a person actually knows.
    """

    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    accounting_date = forms.DateField(
        label=_("تاريخ العملية"), widget=forms.DateInput(attrs={"type": "date"})
    )
    document_date = forms.DateField(
        label=_("تاريخ المستند"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("اتركه فارغاً ليطابق تاريخ العملية."),
    )
    narration = forms.CharField(
        label=_("الشرح"), max_length=500, widget=forms.Textarea(attrs={"rows": 2})
    )
    is_adjustment = forms.BooleanField(label=_("قيد تسوية سنوية"), required=False)

    opening_account = forms.ModelChoiceField(
        queryset=Account.objects.none(), label=_("حساب السطر الأول")
    )
    opening_cost_center = forms.ModelChoiceField(
        queryset=CostCenter.objects.none(), label=_("مركز الكلفة"), required=False
    )
    opening_debit = forms.DecimalField(
        label=_("مدين"), max_digits=18, decimal_places=MONEY_PLACES, initial=Decimal("0")
    )
    opening_credit = forms.DecimalField(
        label=_("دائن"), max_digits=18, decimal_places=MONEY_PLACES, initial=Decimal("0")
    )

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        self.actor = actor
        super().__init__(*args, **kwargs)
        branches = branches_with_permission(actor, CREATE_DRAFT).order_by("code")
        organization_ids = list(branches.values_list("organization_id", flat=True))
        self.fields["branch"].queryset = branches  # type: ignore[attr-defined]
        self.fields["opening_account"].queryset = (  # type: ignore[attr-defined]
            Account.objects.filter(
                organization_id__in=organization_ids, is_postable=True, is_active=True
            )
            .select_related("organization")
            .order_by("organization__code", "code")
        )
        self.fields["opening_cost_center"].queryset = CostCenter.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True
        ).order_by("code")

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        debit = cleaned.get("opening_debit") or Decimal("0")
        credit = cleaned.get("opening_credit") or Decimal("0")
        if debit and credit:
            raise forms.ValidationError(_("السطر إمّا مدين أو دائن، لا الاثنان."), code="both_sides")
        if not debit and not credit:
            raise forms.ValidationError(_("السطر بلا مبلغ. أدخل مديناً أو دائناً."), code="no_amount")
        branch = cleaned.get("branch")
        account = cleaned.get("opening_account")
        if (
            branch is not None
            and account is not None
            and account.organization_id != branch.organization_id
        ):
            self.add_error("opening_account", _("الحساب من مؤسسة أخرى."))
        return cleaned


class JournalLineForm(forms.Form):
    """
    One more line on an open draft.

    Every queryset is narrowed to the **entry's own organization**, not to
    everything the caller reaches. The entry already has an organization, a
    line from another one would be refused by the posting validators anyway,
    and offering it in a dropdown invites somebody to try.
    """

    account = forms.ModelChoiceField(queryset=Account.objects.none(), label=_("الحساب"))
    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    cost_center = forms.ModelChoiceField(
        queryset=CostCenter.objects.none(), label=_("مركز الكلفة"), required=False
    )
    debit = forms.DecimalField(
        label=_("مدين"), max_digits=18, decimal_places=MONEY_PLACES, initial=Decimal("0")
    )
    credit = forms.DecimalField(
        label=_("دائن"), max_digits=18, decimal_places=MONEY_PLACES, initial=Decimal("0")
    )
    narration = forms.CharField(label=_("بيان السطر"), max_length=255, required=False)

    def __init__(self, *args: Any, actor: User, entry: Any, **kwargs: Any) -> None:
        self.actor = actor
        self.entry = entry
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(  # type: ignore[attr-defined]
            organization_id=entry.organization_id, is_postable=True, is_active=True
        ).order_by("code")
        # Assigned through a local so `ruff format` cannot move the ignore
        # comment off the line it belongs to — it did exactly that here once.
        branches = (
            branches_with_permission(actor, CREATE_DRAFT)
            .filter(organization_id=entry.organization_id)
            .order_by("code")
        )
        self.fields["branch"].queryset = branches  # type: ignore[attr-defined]
        self.fields["cost_center"].queryset = CostCenter.objects.filter(  # type: ignore[attr-defined]
            organization_id=entry.organization_id, is_active=True
        ).order_by("code")

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        debit = cleaned.get("debit") or Decimal("0")
        credit = cleaned.get("credit") or Decimal("0")
        if debit and credit:
            raise forms.ValidationError(_("السطر إمّا مدين أو دائن، لا الاثنان."), code="both_sides")
        if not debit and not credit:
            raise forms.ValidationError(_("السطر بلا مبلغ. أدخل مديناً أو دائناً."), code="no_amount")
        account = cleaned.get("account")
        if account is not None and account.requires_cost_center and not cleaned.get("cost_center"):
            self.add_error("cost_center", _("هذا الحساب يتطلب مركز كلفة."))
        return cleaned
