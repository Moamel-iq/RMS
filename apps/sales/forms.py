"""
Sales forms.

Every form here validates and then hands off; none of them calls `save()`. The
service is where the rule lives, and a form that wrote directly would be a
second write path with a second set of rules (see `apps/sales/services.py`).

The organization field appears only when creating. Moving an existing menu item
or channel between organizations would carry its whole sales history across a
tenancy boundary, so on edit the field is **absent** rather than present and
disabled — a disabled field is still submitted, and the view would have to
remember to ignore it.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.organizations.authorization import organizations_with_permission
from apps.organizations.selectors import accessible_branches
from apps.sales.models import (
    MenuCategory,
    MenuItem,
    MenuPriceVersion,
    PriceScope,
    SalesChannel,
    SalesChannelCategory,
    TenderDestination,
)
from apps.sales.permissions import MANAGE_MENU, MANAGE_SALES_CHANNELS

if TYPE_CHECKING:
    from apps.organizations.models import Organization
    from apps.users.models import User


def canonical_code(value: str) -> str:
    return value.strip().upper()


class MenuCategoryForm(forms.Form):
    """Group the menu for the people reading it. Presentation only."""

    organization = forms.ModelChoiceField(queryset=MenuCategory.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(label=_("الرمز"), max_length=32)
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنكليزية"), max_length=200, required=False)
    display_order = forms.IntegerField(label=_("ترتيب العرض"), min_value=1, initial=1)

    def __init__(
        self, *args: Any, actor: User, instance: MenuCategory | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        if instance is not None:
            del self.fields["organization"]
            self.fields["code"].disabled = True
            self.fields["code"].initial = instance.code
            return
        self.fields["organization"].queryset = organizations_with_permission(  # type: ignore[attr-defined]
            actor, MANAGE_MENU
        ).order_by("code")

    def clean_code(self) -> str:
        code = canonical_code(self.cleaned_data["code"])
        if not code:
            raise forms.ValidationError(_("الرمز مطلوب."), code="code_required")
        if self.instance is not None:
            return self.instance.code
        organization_id = self.data.get("organization")
        if (
            organization_id
            and MenuCategory.objects.filter(organization_id=organization_id, code=code).exists()
        ):
            raise forms.ValidationError(
                _("الرمز %(code)s مستخدم في هذه المؤسسة.") % {"code": code}, code="code_taken"
            )
        return code

    def selected_organization(self) -> Organization:
        organization: Organization = self.cleaned_data["organization"]
        return organization


class MenuItemForm(forms.Form):
    """
    One sellable thing.

    `serving_code` is a plain text field rather than a select, and that is the
    consequence of servings belonging to a *version* rather than to a recipe: a
    select would have to pick a version to enumerate, and whichever it picked
    would be wrong for some business date. The service checks the code against
    every version of the chosen recipe, which refuses a typo without refusing a
    serving that starts next Sunday.
    """

    organization = forms.ModelChoiceField(queryset=MenuItem.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(label=_("الرمز"), max_length=32)
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنكليزية"), max_length=200, required=False)
    category = forms.ModelChoiceField(
        queryset=MenuCategory.objects.none(), label=_("المجموعة"), required=False
    )
    recipe = forms.ModelChoiceField(queryset=MenuItem.objects.none(), label=_("الوصفة"))
    serving_code = forms.CharField(
        label=_("رمز الحصة"),
        max_length=32,
        help_text=_(
            "رمز الحصة كما هو معرَّف على نسخ الوصفة — مثل WHOLE أو HALF. "
            "يُحلّ إلى حصة النسخة السارية في تاريخ البيع، ولا يُثبَّت على نسخة بعينها."
        ),
    )
    description_ar = forms.CharField(
        label=_("الوصف"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    display_order = forms.IntegerField(label=_("ترتيب العرض"), min_value=1, initial=1)
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(
        self, *args: Any, actor: User, instance: MenuItem | None = None, **kwargs: Any
    ) -> None:
        from apps.kitchen.models import Recipe

        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance

        reachable = organizations_with_permission(actor, MANAGE_MENU)
        organization_ids = list(reachable.values_list("id", flat=True))
        if instance is not None:
            organization_ids = [instance.organization_id]
            del self.fields["organization"]
            self.fields["code"].disabled = True
            self.fields["code"].initial = instance.code
        else:
            self.fields["organization"].queryset = reachable.order_by("code")  # type: ignore[attr-defined]

        self.fields["category"].queryset = MenuCategory.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True
        ).order_by("display_order", "code")
        # Only recipes that actually have a serving are offerable: an item
        # whose recipe defines no serving could never resolve one at sale time,
        # and offering it here would move the failure to the till.
        self.fields["recipe"].queryset = (  # type: ignore[attr-defined]
            Recipe.objects.filter(
                organization_id__in=organization_ids,
                versions__servings__isnull=False,
            )
            .distinct()
            .order_by("code")
        )

    def clean_code(self) -> str:
        code = canonical_code(self.cleaned_data["code"])
        if not code:
            raise forms.ValidationError(_("الرمز مطلوب."), code="code_required")
        if self.instance is not None:
            return self.instance.code
        organization_id = self.data.get("organization")
        if (
            organization_id
            and MenuItem.objects.filter(organization_id=organization_id, code=code).exists()
        ):
            raise forms.ValidationError(
                _("الرمز %(code)s مستخدم في هذه المؤسسة.") % {"code": code}, code="code_taken"
            )
        return code

    def clean_serving_code(self) -> str:
        return canonical_code(self.cleaned_data["serving_code"])

    def selected_organization(self) -> Organization:
        organization: Organization = self.cleaned_data["organization"]
        return organization


class SalesChannelForm(forms.Form):
    """
    A route sales arrive by.

    The category is disabled on edit rather than removed, because unlike an
    organization it is worth *showing*: it explains why the tender field is
    fixed. It is still never read on edit — `update_sales_channel` has no
    category argument at all, so a tampered POST changes nothing.
    """

    organization = forms.ModelChoiceField(queryset=SalesChannel.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(label=_("الرمز"), max_length=32)
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنكليزية"), max_length=200, required=False)
    category = forms.ChoiceField(label=_("نوع القناة"), choices=SalesChannelCategory.choices)
    default_tender = forms.ChoiceField(
        label=_("وجهة التحصيل"),
        choices=TenderDestination.choices,
        help_text=_("قنوات تطبيقات التوصيل تُحصَّل كذمة دائماً، ولا تُعدّ في الصندوق."),
    )
    cost_center = forms.ModelChoiceField(
        queryset=SalesChannel.objects.none(),
        label=_("مركز الكلفة"),
        help_text=_(
            "إلزامي: حسابات الإيراد والخصم والعمولة تتطلب مركز كلفة، "
            "والقناة هي المستوى الذي يحمل المعنى — الصالة تكسب في الصالة، والتطبيقات في التوصيل."
        ),
    )
    revenue_account = forms.ModelChoiceField(
        queryset=SalesChannel.objects.none(),
        label=_("حساب إيراد خاص"),
        required=False,
        help_text=_("اتركه فارغاً لاستخدام ربط الدور SALES_REVENUE للمؤسسة."),
    )
    requires_cashier = forms.BooleanField(label=_("تمر عبر الصندوق"), required=False, initial=True)
    display_order = forms.IntegerField(label=_("ترتيب العرض"), min_value=1, initial=1)
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(
        self, *args: Any, actor: User, instance: SalesChannel | None = None, **kwargs: Any
    ) -> None:
        from apps.accounting.models import Account, CostCenter

        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance

        reachable = organizations_with_permission(actor, MANAGE_SALES_CHANNELS)
        organization_ids = list(reachable.values_list("id", flat=True))
        if instance is not None:
            organization_ids = [instance.organization_id]
            del self.fields["organization"]
            self.fields["code"].disabled = True
            self.fields["code"].initial = instance.code
            self.fields["category"].disabled = True
            self.fields["category"].initial = instance.category
        else:
            self.fields["organization"].queryset = reachable.order_by("code")  # type: ignore[attr-defined]

        self.fields["cost_center"].queryset = CostCenter.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True
        ).order_by("code")
        self.fields["revenue_account"].queryset = Account.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_postable=True, is_active=True
        ).order_by("code")

    def clean_code(self) -> str:
        code = canonical_code(self.cleaned_data["code"])
        if not code:
            raise forms.ValidationError(_("الرمز مطلوب."), code="code_required")
        if self.instance is not None:
            return self.instance.code
        organization_id = self.data.get("organization")
        if (
            organization_id
            and SalesChannel.objects.filter(organization_id=organization_id, code=code).exists()
        ):
            raise forms.ValidationError(
                _("الرمز %(code)s مستخدم في هذه المؤسسة.") % {"code": code}, code="code_taken"
            )
        return code

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        category = self.instance.category if self.instance is not None else data.get("category")
        tender = data.get("default_tender")
        if (
            category == SalesChannelCategory.DELIVERY_APPLICATION
            and tender != TenderDestination.APPLICATION_RECEIVABLE
        ):
            self.add_error(
                "default_tender",
                _("قناة تطبيق التوصيل تُحصَّل كذمة تطبيق، لا نقداً ولا ببطاقة."),
            )
        if (
            tender == TenderDestination.APPLICATION_RECEIVABLE
            and category != SalesChannelCategory.DELIVERY_APPLICATION
        ):
            self.add_error("default_tender", _("الذمة وجهة تحصيل لقنوات تطبيقات التوصيل وحدها."))
        return data

    def selected_organization(self) -> Organization:
        organization: Organization = self.cleaned_data["organization"]
        return organization


class MenuPriceForm(forms.Form):
    """
    What an item costs the customer, from a date.

    There is no edit form for a price and that is deliberate: a price that has
    sold something is evidence. Correcting it is ending it and creating a
    replacement, which is what `close_menu_price` and this form do together.
    """

    menu_item = forms.ModelChoiceField(queryset=MenuItem.objects.none(), label=_("الصنف"))
    branch = forms.ModelChoiceField(queryset=MenuItem.objects.none(), label=_("الفرع"))
    scope = forms.ChoiceField(
        label=_("نطاق السعر"),
        choices=[
            (PriceScope.BRANCH_DEFAULT.value, PriceScope.BRANCH_DEFAULT.label),
            (PriceScope.CHANNEL.value, PriceScope.CHANNEL.label),
        ],
        initial=PriceScope.BRANCH_DEFAULT,
        help_text=_("الأضيق يفوز: سعر القناة يسبق سعر الفرع الافتراضي."),
    )
    channel = forms.ModelChoiceField(
        queryset=SalesChannel.objects.none(), label=_("القناة"), required=False
    )
    unit_price = forms.DecimalField(label=_("سعر الوحدة"), min_value=0, decimal_places=6)
    effective_from = forms.DateField(
        label=_("ساري من"), widget=forms.DateInput(attrs={"type": "date"})
    )
    effective_to = forms.DateField(
        label=_("ساري حتى"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("اتركه فارغاً للسعر المفتوح."),
    )
    evidence_reference = forms.CharField(label=_("المستند المرجعي"), max_length=200, required=False)
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(
        self, *args: Any, actor: User, instance: MenuPriceVersion | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance

        organization_ids = list(
            organizations_with_permission(actor, MANAGE_MENU).values_list("id", flat=True)
        )
        self.fields["menu_item"].queryset = MenuItem.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True
        ).order_by("code")
        branches = accessible_branches(actor).filter(organization_id__in=organization_ids)
        self.fields["branch"].queryset = branches.order_by("code")  # type: ignore[attr-defined]
        self.fields["channel"].queryset = SalesChannel.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True
        ).order_by("code")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        scope = data.get("scope")
        channel = data.get("channel")
        if scope == PriceScope.CHANNEL and channel is None:
            self.add_error("channel", _("سعر القناة يحتاج قناة."))
        if scope != PriceScope.CHANNEL and channel is not None:
            self.add_error("channel", _("سعر الفرع الافتراضي لا يُربط بقناة."))

        starts: datetime.date | None = data.get("effective_from")
        ends: datetime.date | None = data.get("effective_to")
        if starts and ends and ends < starts:
            self.add_error("effective_to", _("تاريخ الانتهاء قبل تاريخ البدء."))
        return data


class MenuPriceCloseForm(forms.Form):
    """End a price on a date. The amount is never touched."""

    effective_to = forms.DateField(
        label=_("ينتهي في"), widget=forms.DateInput(attrs={"type": "date"})
    )
    reason = forms.CharField(label=_("السبب"), max_length=300)


class BranchAvailabilityForm(forms.Form):
    """Whether one branch sells one item."""

    branch = forms.ModelChoiceField(queryset=MenuItem.objects.none(), label=_("الفرع"))
    is_available = forms.BooleanField(label=_("متاح للبيع"), required=False, initial=True)
    local_name_ar = forms.CharField(label=_("اسم محلي"), max_length=200, required=False)
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, actor: User, menu_item: MenuItem, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.menu_item = menu_item
        branches = accessible_branches(actor).filter(organization_id=menu_item.organization_id)
        self.fields["branch"].queryset = branches.order_by("code")  # type: ignore[attr-defined]


__all__ = [
    "BranchAvailabilityForm",
    "MenuCategoryForm",
    "MenuItemForm",
    "MenuPriceCloseForm",
    "MenuPriceForm",
    "SalesChannelForm",
    "canonical_code",
]
