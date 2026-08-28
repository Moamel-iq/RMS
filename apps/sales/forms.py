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
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.inventory.models import InventoryItem, ItemType, Warehouse
from apps.organizations.authorization import accessible_warehouses, organizations_with_permission
from apps.organizations.selectors import accessible_branches
from apps.sales.models import (
    CommissionBasis,
    DeliveryApplication,
    DiscountProgram,
    FulfillmentSource,
    MenuCategory,
    MenuItem,
    MenuPriceVersion,
    PriceScope,
    SalesChannel,
    SalesChannelCategory,
    TenderDestination,
)
from apps.sales.permissions import (
    MANAGE_DELIVERY_APPLICATIONS,
    MANAGE_MENU,
    MANAGE_SALES_AGREEMENTS,
    MANAGE_SALES_CHANNELS,
    MANAGE_SALES_DISCOUNTS,
)

if TYPE_CHECKING:
    from apps.organizations.models import Organization
    from apps.users.models import User

    # django-stubs types ModelChoiceField by its model, but the runtime class
    # is not subscriptable — `forms.ModelChoiceField[InventoryItem]` raises
    # TypeError while Django imports this module — and the project does not
    # monkeypatch that in (see apps/users/forms.py). So the parametrised base
    # exists for mypy only; at runtime the subclasses extend the plain field.
    _InventoryItemChoiceField = forms.ModelChoiceField[InventoryItem]
    _WarehouseChoiceField = forms.ModelChoiceField[Warehouse]
else:
    _InventoryItemChoiceField = forms.ModelChoiceField
    _WarehouseChoiceField = forms.ModelChoiceField


def canonical_code(value: str) -> str:
    return value.strip().upper()


class _DirectStockItemChoiceField(_InventoryItemChoiceField):
    """Show the base unit beside the item because the entered quantity uses it."""

    def label_from_instance(self, item: InventoryItem) -> str:
        return f"{item.code} — {item.name} ({item.base_unit.code})"


class _SourceWarehouseChoiceField(_WarehouseChoiceField):
    """Warehouse codes are branch-local, so include the branch in every option."""

    def label_from_instance(self, warehouse: Warehouse) -> str:
        return f"{warehouse.branch.code} / {warehouse.code} — {warehouse.name}"


class MenuCategoryForm(forms.Form):
    """Group the menu for the people reading it. Presentation only."""

    organization = forms.ModelChoiceField(queryset=MenuCategory.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(label=_("الرمز"), max_length=32)
    name = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
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
    name = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    category = forms.ModelChoiceField(
        queryset=MenuCategory.objects.none(), label=_("المجموعة"), required=False
    )
    fulfillment_source = forms.ChoiceField(
        label=_("مصدر التنفيذ"),
        choices=FulfillmentSource.choices,
        initial=FulfillmentSource.RECIPE_SERVING,
        widget=forms.Select(attrs={"data-fulfillment-source": ""}),
        help_text=_(
            "اختر حصة من وصفة للأطباق المحضّرة، أو صنفاً مخزنياً مباشراً "
            "للماء والمشروبات والبضاعة المشتراة لإعادة البيع."
        ),
    )
    recipe = forms.ModelChoiceField(
        queryset=MenuItem.objects.none(), label=_("الوصفة"), required=False
    )
    serving_code = forms.CharField(
        label=_("رمز الحصة"),
        max_length=32,
        required=False,
        help_text=_(
            "رمز الحصة كما هو معرَّف على نسخ الوصفة — مثل WHOLE أو HALF. "
            "يُحلّ إلى حصة النسخة السارية في تاريخ البيع، ولا يُثبَّت على نسخة بعينها."
        ),
    )
    inventory_item = _DirectStockItemChoiceField(
        queryset=InventoryItem.objects.none(),
        label=_("الصنف المخزني المباشر"),
        required=False,
        help_text=_(
            "تظهر هنا فقط الأصناف الفعّالة المصنفة «بضاعة لإعادة البيع» والتي لا تتطلب اختيار تشغيلة."
        ),
    )
    direct_stock_base_quantity = forms.DecimalField(
        label=_("كمية المخزون لكل وحدة مباعة"),
        required=False,
        min_value=Decimal("0.000001"),
        max_digits=21,
        decimal_places=6,
        help_text=_(
            "بالوحدة الأساسية للصنف المخزني؛ مثال: 1 لقنينة واحدة، أو 0.500 لنصف كيلوغرام."
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
        self.fields["inventory_item"].queryset = (  # type: ignore[attr-defined]
            InventoryItem.objects.filter(
                organization_id__in=organization_ids,
                is_active=True,
                item_type=ItemType.GOODS_FOR_RESALE,
                tracks_lots=False,
            )
            .select_related("base_unit")
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
        return canonical_code(self.cleaned_data.get("serving_code", ""))

    def clean(self) -> dict[str, Any]:
        """Validate the selected route and erase every field on the inactive route."""
        data = super().clean() or {}
        source = data.get("fulfillment_source")
        organization = (
            self.instance.organization if self.instance is not None else data.get("organization")
        )

        if source == FulfillmentSource.RECIPE_SERVING:
            recipe = data.get("recipe")
            if recipe is None:
                self.add_error("recipe", _("اختر الوصفة التي تنفّذ هذا الصنف."))
            elif organization is not None and recipe.organization_id != organization.pk:
                self.add_error("recipe", _("الوصفة المختارة تابعة لمؤسسة أخرى."))
            if not data.get("serving_code"):
                self.add_error("serving_code", _("أدخل رمز الحصة المعرّف على الوصفة."))
            data["inventory_item"] = None
            data["direct_stock_base_quantity"] = None
        elif source == FulfillmentSource.DIRECT_STOCK:
            inventory_item = data.get("inventory_item")
            if inventory_item is None:
                self.add_error("inventory_item", _("اختر الصنف الذي سيخرج من المخزون."))
            elif organization is not None and inventory_item.organization_id != organization.pk:
                self.add_error("inventory_item", _("الصنف المخزني المختار تابع لمؤسسة أخرى."))
            if data.get("direct_stock_base_quantity") is None:
                self.add_error(
                    "direct_stock_base_quantity",
                    _("أدخل كمية المخزون التي تخرج مقابل وحدة مباعة واحدة."),
                )
            data["recipe"] = None
            data["serving_code"] = ""
        return data

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
    name = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
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
        choices=PriceScope.choices,
        initial=PriceScope.BRANCH_DEFAULT,
        help_text=_("الأضيق يفوز: سعر التطبيق يسبق سعر القناة، وسعر القناة يسبق سعر الفرع."),
    )
    channel = forms.ModelChoiceField(
        queryset=SalesChannel.objects.none(), label=_("القناة"), required=False
    )
    delivery_application = forms.ModelChoiceField(
        queryset=DeliveryApplication.objects.none(), label=_("تطبيق التوصيل"), required=False
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
        self.fields["delivery_application"].queryset = DeliveryApplication.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True
        ).order_by("code")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        scope = data.get("scope")
        channel = data.get("channel")
        application = data.get("delivery_application")
        if scope == PriceScope.CHANNEL and channel is None:
            self.add_error("channel", _("سعر القناة يحتاج قناة."))
        if scope != PriceScope.CHANNEL and channel is not None:
            self.add_error("channel", _("القناة تُذكر في سعر القناة فقط."))
        if scope == PriceScope.APPLICATION and application is None:
            self.add_error("delivery_application", _("سعر التطبيق يحتاج تطبيق توصيل."))
        if scope != PriceScope.APPLICATION and application is not None:
            self.add_error("delivery_application", _("التطبيق يُذكر في سعر التطبيق فقط."))

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
    source_warehouse = _SourceWarehouseChoiceField(
        queryset=Warehouse.objects.none(),
        label=_("مخزن الصرف"),
        required=False,
        help_text=_("المخزن الفعلي الذي تُخصم منه مبيعات هذا الصنف في الفرع المختار."),
    )
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
        if menu_item.fulfillment_source == FulfillmentSource.DIRECT_STOCK:
            self.fields["source_warehouse"].required = True
            self.fields["source_warehouse"].queryset = (  # type: ignore[attr-defined]
                accessible_warehouses(actor)
                .filter(
                    branch__organization_id=menu_item.organization_id,
                    is_system=False,
                )
                .select_related("branch")
                .order_by("branch__code", "code")
            )
        else:
            del self.fields["source_warehouse"]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        branch = data.get("branch")
        warehouse = data.get("source_warehouse")
        if (
            self.menu_item.fulfillment_source == FulfillmentSource.DIRECT_STOCK
            and branch is not None
            and warehouse is not None
            and warehouse.branch_id != branch.pk
        ):
            self.add_error("source_warehouse", _("اختر مخزناً تابعاً للفرع المحدد."))
        return data


# ---------------------------------------------------------------------------
# Delivery applications, agreements and discounts — checkpoint 2
# ---------------------------------------------------------------------------


class DeliveryApplicationForm(forms.Form):
    """A delivery company. No rate here — rates are dated contract terms."""

    organization = forms.ModelChoiceField(
        queryset=DeliveryApplication.objects.none(), label=_("المؤسسة")
    )
    code = forms.CharField(label=_("الرمز"), max_length=32)
    name = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    settlement_cycle_days = forms.IntegerField(
        label=_("دورة التسوية (يوم)"),
        min_value=0,
        max_value=365,
        initial=30,
        help_text=_("للتقارير وتعمير الذمم. لا يمنع شيئاً — التأخر مشكلة تجارية لا خطأ إدخال."),
    )
    receivable_account = forms.ModelChoiceField(
        queryset=DeliveryApplication.objects.none(),
        label=_("حساب ذمة خاص"),
        required=False,
        help_text=_(
            "اتركه فارغاً لاستخدام ربط الدور DELIVERY_APP_RECEIVABLE للمؤسسة. "
            "تغييره لا يحرّك شيئاً مرحّلاً — القيد المرحّل يحمل الحساب الذي استُخدم فعلاً."
        ),
    )
    contact_name = forms.CharField(label=_("جهة الاتصال"), max_length=200, required=False)
    phone = forms.CharField(label=_("الهاتف"), max_length=20, required=False)
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(
        self,
        *args: Any,
        actor: User,
        instance: DeliveryApplication | None = None,
        **kwargs: Any,
    ) -> None:
        from apps.accounting.models import Account

        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance

        reachable = organizations_with_permission(actor, MANAGE_DELIVERY_APPLICATIONS)
        organization_ids = list(reachable.values_list("id", flat=True))
        if instance is not None:
            organization_ids = [instance.organization_id]
            del self.fields["organization"]
            self.fields["code"].disabled = True
            self.fields["code"].initial = instance.code
        else:
            self.fields["organization"].queryset = reachable.order_by("code")  # type: ignore[attr-defined]

        self.fields["receivable_account"].queryset = Account.objects.filter(  # type: ignore[attr-defined]
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
            and DeliveryApplication.objects.filter(
                organization_id=organization_id, code=code
            ).exists()
        ):
            raise forms.ValidationError(
                _("الرمز %(code)s مستخدم في هذه المؤسسة.") % {"code": code}, code="code_taken"
            )
        return code

    def selected_organization(self) -> Organization:
        organization: Organization = self.cleaned_data["organization"]
        return organization


class ApplicationBranchForm(forms.Form):
    """Whether one branch trades with one application."""

    branch = forms.ModelChoiceField(queryset=DeliveryApplication.objects.none(), label=_("الفرع"))
    is_active = forms.BooleanField(label=_("مفعّل"), required=False, initial=True)
    external_store_code = forms.CharField(
        label=_("رمز الفرع لدى التطبيق"),
        max_length=64,
        required=False,
        help_text=_("يُستخدم في مطابقة كشوف التطبيق. لا يُرحَّل عليه شيء."),
    )
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(
        self, *args: Any, actor: User, application: DeliveryApplication, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.application = application
        branches = accessible_branches(actor).filter(organization_id=application.organization_id)
        self.fields["branch"].queryset = branches.order_by("code")  # type: ignore[attr-defined]


class DeliveryAgreementForm(forms.Form):
    """
    What one application charges one branch, from a date.

    **Evidence is required** and the field is not optional, which no other
    master in this module insists on. An agreement accrues an expense on every
    order from the day it starts; an unevidenced rate is a number somebody
    typed that nobody can check against anything.

    There is no edit form. A rate that has accrued a commission is evidence,
    and correcting it in place would restate an expense that has already
    posted — so the only correction is ending it and recording the replacement.
    """

    branch = forms.ModelChoiceField(queryset=DeliveryApplication.objects.none(), label=_("الفرع"))
    delivery_application = forms.ModelChoiceField(
        queryset=DeliveryApplication.objects.none(), label=_("تطبيق التوصيل")
    )
    commission_percent = forms.DecimalField(
        label=_("نسبة العمولة %"), min_value=0, max_value=100, decimal_places=6, initial=0
    )
    fixed_fee_per_order = forms.DecimalField(
        label=_("رسم ثابت لكل طلب"),
        min_value=0,
        decimal_places=3,
        initial=0,
        help_text=_("صفر إذا لم ينص العقد على رسم ثابت."),
    )
    commission_basis = forms.ChoiceField(
        label=_("أساس احتساب العمولة"),
        choices=CommissionBasis.choices,
        initial=CommissionBasis.GROSS_LIST_AMOUNT,
        help_text=_(
            "الأساس يقرّر المبلغ الذي تُحتسب عليه النسبة، وهو بند تعاقدي — "
            "«بعد كل الخصومات» و«المبلغ المدفوع من الزبون» متطابقان رقمياً اليوم ويظلان قيمتين منفصلتين."
        ),
    )
    settlement_lag_days = forms.IntegerField(
        label=_("مهلة التسوية (يوم)"), min_value=0, max_value=365, initial=30
    )
    effective_from = forms.DateField(
        label=_("ساري من"), widget=forms.DateInput(attrs={"type": "date"})
    )
    effective_to = forms.DateField(
        label=_("ساري حتى"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("اتركه فارغاً للاتفاقية المفتوحة."),
    )
    evidence_reference = forms.CharField(
        label=_("العقد أو الموافقة"),
        max_length=200,
        help_text=_("إلزامي: الاتفاقية تُحمّل مصروفاً على كل طلب، ولا بد من مستند يُراجع عليه."),
    )
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, actor: User, instance: None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance

        organization_ids = list(
            organizations_with_permission(actor, MANAGE_SALES_AGREEMENTS).values_list(
                "id", flat=True
            )
        )
        branches = accessible_branches(actor).filter(organization_id__in=organization_ids)
        self.fields["branch"].queryset = branches.order_by("code")  # type: ignore[attr-defined]
        self.fields["delivery_application"].queryset = DeliveryApplication.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True
        ).order_by("code")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        starts: datetime.date | None = data.get("effective_from")
        ends: datetime.date | None = data.get("effective_to")
        if starts and ends and ends < starts:
            self.add_error("effective_to", _("تاريخ الانتهاء قبل تاريخ البدء."))
        percent = data.get("commission_percent")
        fee = data.get("fixed_fee_per_order")
        if percent == 0 and fee == 0:
            self.add_error(
                "commission_percent",
                _("اتفاقية بلا نسبة وبلا رسم ثابت لا تحمّل شيئاً. سجّل الشروط الفعلية."),
            )
        return data


class AgreementCloseForm(forms.Form):
    """End an agreement on a date. The rate is never touched."""

    effective_to = forms.DateField(
        label=_("تنتهي في"), widget=forms.DateInput(attrs={"type": "date"})
    )
    reason = forms.CharField(label=_("السبب"), max_length=300)


class DiscountProgramForm(forms.Form):
    """
    A discount, and who funds it.

    The funding fields are two, and the form checks that they close before the
    database does. Both checks exist and do different jobs: the constraint is
    what makes it true against a raw `INSERT`, and this is what makes it
    *explainable* on a form instead of a database error nobody can read.
    """

    organization = forms.ModelChoiceField(
        queryset=DiscountProgram.objects.none(), label=_("المؤسسة")
    )
    code = forms.CharField(label=_("الرمز"), max_length=32)
    name = forms.CharField(label=_("الاسم بالعربية"), max_length=200)

    discount_percent = forms.DecimalField(
        label=_("نسبة الخصم %"), min_value=0, max_value=100, decimal_places=6, required=False
    )
    discount_amount = forms.DecimalField(
        label=_("مبلغ الخصم"), min_value=0, decimal_places=3, required=False
    )
    maximum_amount = forms.DecimalField(
        label=_("حد أقصى للخصم"),
        min_value=0,
        decimal_places=3,
        required=False,
        help_text=_("اتركه فارغاً إذا لم يكن هناك حد."),
    )

    restaurant_funded_share = forms.DecimalField(
        label=_("حصة المطعم من التمويل %"),
        min_value=0,
        max_value=100,
        decimal_places=6,
        initial=100,
        help_text=_("يخفض إيراد المطعم — خصم على الإيراد، لا مصروف تسويق."),
    )
    application_funded_share = forms.DecimalField(
        label=_("حصة التطبيق من التمويل %"),
        min_value=0,
        max_value=100,
        decimal_places=6,
        initial=0,
        help_text=_(
            "يُعوَّض من التطبيق: لا يخفض الإيراد ولا يخفض ما يدين به التطبيق — بل هو جزء منه."
        ),
    )

    branch = forms.ModelChoiceField(
        queryset=DiscountProgram.objects.none(),
        label=_("الفرع"),
        required=False,
        help_text=_("اتركه فارغاً ليسري على كل الفروع."),
    )
    channel = forms.ModelChoiceField(
        queryset=SalesChannel.objects.none(), label=_("القناة"), required=False
    )
    delivery_application = forms.ModelChoiceField(
        queryset=DeliveryApplication.objects.none(),
        label=_("تطبيق التوصيل"),
        required=False,
        help_text=_("إلزامي إذا كانت هناك حصة تمويل من التطبيق."),
    )
    menu_item = forms.ModelChoiceField(
        queryset=MenuItem.objects.none(), label=_("صنف محدد"), required=False
    )

    effective_from = forms.DateField(
        label=_("ساري من"), widget=forms.DateInput(attrs={"type": "date"})
    )
    effective_to = forms.DateField(
        label=_("ساري حتى"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    evidence_reference = forms.CharField(label=_("المستند المرجعي"), max_length=200, required=False)
    notes = forms.CharField(
        label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args: Any, actor: User, instance: None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance

        reachable = organizations_with_permission(actor, MANAGE_SALES_DISCOUNTS)
        organization_ids = list(reachable.values_list("id", flat=True))
        self.fields["organization"].queryset = reachable.order_by("code")  # type: ignore[attr-defined]
        branches = accessible_branches(actor).filter(organization_id__in=organization_ids)
        self.fields["branch"].queryset = branches.order_by("code")  # type: ignore[attr-defined]
        self.fields["channel"].queryset = SalesChannel.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True
        ).order_by("code")
        self.fields["delivery_application"].queryset = DeliveryApplication.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True
        ).order_by("code")
        self.fields["menu_item"].queryset = MenuItem.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True
        ).order_by("code")

    def clean_code(self) -> str:
        code = canonical_code(self.cleaned_data["code"])
        if not code:
            raise forms.ValidationError(_("الرمز مطلوب."), code="code_required")
        organization_id = self.data.get("organization")
        if (
            organization_id
            and DiscountProgram.objects.filter(organization_id=organization_id, code=code).exists()
        ):
            raise forms.ValidationError(
                _("الرمز %(code)s مستخدم في هذه المؤسسة.") % {"code": code}, code="code_taken"
            )
        return code

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        percent = data.get("discount_percent")
        amount = data.get("discount_amount")
        if percent is None and amount is None:
            self.add_error("discount_percent", _("الخصم يحتاج نسبة أو مبلغاً."))
        if percent is not None and amount is not None:
            self.add_error("discount_amount", _("الخصم ينص على نسبة أو مبلغ، لا الاثنين معاً."))

        restaurant = data.get("restaurant_funded_share")
        application = data.get("application_funded_share")
        if restaurant is not None and application is not None:
            if restaurant + application != Decimal("100"):
                self.add_error(
                    "application_funded_share",
                    _("حصتا التمويل يجب أن تساويا الخصم كاملاً: %(r)s%% + %(a)s%% ليست ١٠٠%%.")
                    % {"r": restaurant, "a": application},
                )
            elif application > Decimal("0") and data.get("delivery_application") is None:
                self.add_error(
                    "delivery_application",
                    _("خصم يموّله تطبيق يجب أن يسمّي التطبيق الذي يموّله — وإلا فالذمة على لا أحد."),
                )

        starts: datetime.date | None = data.get("effective_from")
        ends: datetime.date | None = data.get("effective_to")
        if starts and ends and ends < starts:
            self.add_error("effective_to", _("تاريخ الانتهاء قبل تاريخ البدء."))
        return data

    def selected_organization(self) -> Organization:
        organization: Organization = self.cleaned_data["organization"]
        return organization


class DiscountCloseForm(forms.Form):
    """End a programme on a date. Its amounts and shares are never touched."""

    effective_to = forms.DateField(
        label=_("ينتهي في"), widget=forms.DateInput(attrs={"type": "date"})
    )
    reason = forms.CharField(label=_("السبب"), max_length=300)


__all__ = [
    "AgreementCloseForm",
    "ApplicationBranchForm",
    "BranchAvailabilityForm",
    "DeliveryAgreementForm",
    "DeliveryApplicationForm",
    "DiscountCloseForm",
    "DiscountProgramForm",
    "MenuCategoryForm",
    "MenuItemForm",
    "MenuPriceCloseForm",
    "MenuPriceForm",
    "SalesChannelForm",
    "canonical_code",
]
