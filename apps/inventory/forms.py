"""
Forms for the inventory master-data screens.

Deliberately thin, and deliberately **not** the mutation path. A form here
collects input, narrows every selector to what the caller already reaches, and
renders errors in Arabic. The write itself is always a call to
`apps/inventory/services.py`, which re-reads the authoritative row, checks the
invariants, saves, and records the audit event.

That split is not ceremony. A bound `ModelForm` mutates `self.instance` during
validation, so by the time `form_valid` runs, the in-memory object already
holds the new values — an audit `previous_state` taken from it would record
before == after. The services take primitives and read "before" from the
database for exactly this reason.

Every queryset below is scoped by the acting user. A selector that offered a
foreign organization's category would turn a dropdown into a way to write
across a tenancy boundary, and the service refusing it afterwards would be the
only thing standing in the way.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django import forms
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import Account, AccountRole, CostCenter
from apps.inventory.models import (
    MAX_CATEGORY_DEPTH,
    ConversionType,
    InventoryItem,
    InventoryMovementDocumentLine,
    ItemCategory,
    ItemPackageConversion,
    ItemType,
    PackageUnit,
    Warehouse,
    WarehouseType,
)
from apps.inventory.permissions import (
    CREATE_DRAFT_MOVEMENT,
    CREATE_OPENING_STOCK,
    MANAGE_CATEGORIES,
    MANAGE_CONVERSIONS,
    MANAGE_ITEMS,
    MANAGE_PACKAGE_UNITS,
    MANAGE_WAREHOUSES,
)
from apps.inventory.selectors import (
    visible_categories,
    visible_items,
    visible_package_units,
)
from apps.organizations.authorization import (
    branches_with_permission,
    organizations_with_permission,
)
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User


class ScopedForm(forms.Form):
    """
    A form that knows who is filling it in.

    The actor is required, not optional. A default of `None` would let a new
    screen forget to pass it and silently offer every organization in the
    database.
    """

    #: Which permission the caller must hold for the places this form offers.
    scope_permission: str = ""

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        self.actor = actor
        super().__init__(*args, **kwargs)

    def organization_choices(self) -> QuerySet[Organization]:
        return organizations_with_permission(self.actor, self.scope_permission).order_by("code")

    def branch_choices(self) -> QuerySet[Branch]:
        return branches_with_permission(self.actor, self.scope_permission).order_by("code")


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class ItemCategoryForm(ScopedForm):
    """
    Create or rename a category.

    `parent` is limited to categories in the chosen organization that may
    legally take a child: shallow enough, and holding no items. The service
    checks all of it again — a select element is a convenience, never a
    control.
    """

    scope_permission = MANAGE_CATEGORIES

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(
        label=_("الرمز"),
        max_length=32,
        help_text=_("حروف إنجليزية كبيرة وأرقام. يُوحَّد تلقائياً ولا يمكن تغييره لاحقاً."),
    )
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنجليزية"), max_length=200, required=False)
    parent = forms.ModelChoiceField(
        queryset=ItemCategory.objects.none(),
        label=_("المجموعة الأعلى"),
        required=False,
        help_text=_("اتركه فارغاً لمجموعة رئيسية. ثلاثة مستويات كحد أقصى."),
    )
    is_active = forms.BooleanField(label=_("فعّالة"), required=False, initial=True)

    def __init__(
        self, *args: Any, actor: User, instance: ItemCategory | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.instance = instance

        parents = visible_categories(actor).filter(is_active=True, depth__lt=MAX_CATEGORY_DEPTH)
        if instance is None:
            self.fields["organization"].queryset = self.organization_choices()  # type: ignore[attr-defined]
        else:
            # The code names the category in reports; it is fixed once created.
            # A disabled field takes its value from `initial`, so the queryset
            # is pinned to the one row it can legitimately hold.
            self.fields["code"].disabled = True
            self.fields["organization"].disabled = True
            self.fields["organization"].queryset = Organization.objects.filter(  # type: ignore[attr-defined]
                pk=instance.organization_id
            )
            # Its own subtree cannot be its parent. The service checks the
            # cycle properly; this only keeps the obvious cases out of the list.
            parents = parents.filter(organization_id=instance.organization_id).exclude(
                pk=instance.pk
            )
        self.fields["parent"].queryset = parents.order_by("depth", "code")  # type: ignore[attr-defined]

    def clean_name_en(self) -> str:
        return str(self.cleaned_data.get("name_en") or "")


# ---------------------------------------------------------------------------
# Package units
# ---------------------------------------------------------------------------


class PackageUnitForm(ScopedForm):
    """
    Create or rename a package unit.

    **There is no factor field, and there must never be one.** A carton of
    chicken and a carton of oil share only the word; the factor belongs to
    `ItemPackageConversion`, per item. Offering a factor here would be an
    invitation to record a universal one that cannot exist.
    """

    scope_permission = MANAGE_PACKAGE_UNITS

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(
        label=_("الرمز"),
        max_length=32,
        help_text=_("مثل CARTON أو SACK. يُوحَّد تلقائياً ولا يمكن تغييره لاحقاً."),
    )
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=100)
    name_en = forms.CharField(label=_("الاسم بالإنجليزية"), max_length=100, required=False)
    is_active = forms.BooleanField(label=_("فعّالة"), required=False, initial=True)

    def __init__(
        self, *args: Any, actor: User, instance: PackageUnit | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.instance = instance
        if instance is None:
            self.fields["organization"].queryset = self.organization_choices()  # type: ignore[attr-defined]
        else:
            self.fields["code"].disabled = True
            self.fields["organization"].disabled = True
            self.fields["organization"].queryset = Organization.objects.filter(  # type: ignore[attr-defined]
                pk=instance.organization_id
            )

    def clean_name_en(self) -> str:
        return str(self.cleaned_data.get("name_en") or "")


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


class InventoryItemForm(ScopedForm):
    """
    Create or amend an item.

    Carries no account field and no negative-stock flag, by decision:
    account resolution belongs to `AccountRole`/`AccountMapping`, and an
    override of the negative-stock rule is a per-posting exception with an
    actor and a reason, never a standing property of an item.

    `base_unit` disappears once the item exists. Every stored quantity is
    expressed in it, so changing it would restate the item's whole history
    rather than correct it.
    """

    scope_permission = MANAGE_ITEMS

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(
        label=_("الرمز"),
        max_length=32,
        help_text=_("يُوحَّد تلقائياً إلى حروف كبيرة. لا يمكن تغييره لاحقاً."),
    )
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنجليزية"), max_length=200, required=False)
    category = forms.ModelChoiceField(
        queryset=ItemCategory.objects.none(),
        label=_("المجموعة"),
        help_text=_("المستوى الأخير فقط — المجموعة التي لها مجموعات فرعية لا تحمل أصنافاً."),
    )
    item_type = forms.ChoiceField(choices=ItemType.choices, label=_("النوع"))
    base_unit = forms.ModelChoiceField(
        queryset=UnitOfMeasure.objects.none(),
        label=_("وحدة الأساس"),
        help_text=_("كل كمية مخزنية تُقاس بها. لا يمكن تغييرها بعد أول حركة."),
    )
    tracks_lots = forms.BooleanField(label=_("يتتبع الدفعات"), required=False)
    tracks_expiry = forms.BooleanField(label=_("يتتبع الصلاحية"), required=False)
    shelf_life_days = forms.IntegerField(
        label=_("مدة الصلاحية (أيام)"), required=False, min_value=1
    )
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)
    is_active = forms.BooleanField(label=_("فعّال"), required=False, initial=True)

    def __init__(
        self, *args: Any, actor: User, instance: InventoryItem | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.instance = instance

        categories = visible_categories(actor).filter(is_active=True, children__isnull=True)
        if instance is None:
            self.fields["organization"].queryset = self.organization_choices()  # type: ignore[attr-defined]
            self.fields["base_unit"].queryset = UnitOfMeasure.objects.filter(  # type: ignore[attr-defined]
                is_active=True
            ).order_by("code")
        else:
            self.fields["code"].disabled = True
            self.fields["organization"].disabled = True
            self.fields["base_unit"].disabled = True
            self.fields["organization"].queryset = Organization.objects.filter(  # type: ignore[attr-defined]
                pk=instance.organization_id
            )
            self.fields["base_unit"].queryset = UnitOfMeasure.objects.filter(  # type: ignore[attr-defined]
                pk=instance.base_unit_id
            )
            categories = categories.filter(organization_id=instance.organization_id)
        self.fields["category"].queryset = categories.order_by("code")  # type: ignore[attr-defined]

    def clean_name_en(self) -> str:
        return str(self.cleaned_data.get("name_en") or "")

    def clean_notes(self) -> str:
        return str(self.cleaned_data.get("notes") or "")

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean()  # type: ignore[assignment]
        tracks_lots = bool(cleaned.get("tracks_lots"))
        tracks_expiry = bool(cleaned.get("tracks_expiry"))
        shelf_life = cleaned.get("shelf_life_days")

        # Checked here as well as in the database so the operator is told which
        # box to tick, rather than being handed an IntegrityError.
        if tracks_expiry and not tracks_lots:
            self.add_error(
                "tracks_expiry",
                _("تتبع الصلاحية يتطلب تتبع الدفعات — الصلاحية صفة الدفعة لا الصنف."),
            )
        if shelf_life and not tracks_expiry:
            self.add_error(
                "shelf_life_days", _("مدة الصلاحية بلا معنى ما لم يكن الصنف يتتبع الصلاحية.")
            )
        return cleaned


# ---------------------------------------------------------------------------
# Item package conversions
# ---------------------------------------------------------------------------


class ItemConversionForm(ScopedForm):
    """
    Record how many base units are in one package **of this item**.

    The factor resolves directly to the item's base unit; there is no field for
    an intermediate package, because chains are not modelled. It is typed as
    text and parsed as `Decimal` — a locale-aware number widget would accept a
    comma and change what the factor means.
    """

    scope_permission = MANAGE_CONVERSIONS

    item = forms.ModelChoiceField(queryset=InventoryItem.objects.none(), label=_("الصنف"))
    package_unit = forms.ModelChoiceField(
        queryset=PackageUnit.objects.none(), label=_("وحدة التعبئة")
    )
    conversion_type = forms.ChoiceField(
        choices=ConversionType.choices,
        label=_("نوع التحويل"),
        help_text=_("ثابت: المعامل دقيق. متغير: المعامل تقديري والوزن يُقاس عند الاستلام."),
    )
    factor_to_base = forms.CharField(
        label=_("عدد وحدات الأساس في العبوة"),
        help_text=_("رقم عشري بالنقطة، مثل 24 أو 0.8 — لا تستخدم الفاصلة."),
    )
    effective_from = forms.DateField(
        label=_("سارٍ من"), widget=forms.DateInput(attrs={"type": "date"})
    )
    effective_to = forms.DateField(
        label=_("سارٍ حتى"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("اتركه فارغاً إذا كان سارياً حتى إشعار آخر."),
    )
    allows_fractional = forms.BooleanField(
        label=_("يسمح بأجزاء العبوة"), required=False, initial=True
    )
    minimum_increment = forms.CharField(label=_("أقل مقدار"), required=False)
    is_default_purchase_package = forms.BooleanField(
        label=_("عبوة الشراء الافتراضية"), required=False
    )
    is_active = forms.BooleanField(label=_("فعّال"), required=False, initial=True)

    def __init__(
        self,
        *args: Any,
        actor: User,
        instance: ItemPackageConversion | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.instance = instance

        if instance is None:
            items = visible_items(actor).filter(is_active=True)
            packages = visible_package_units(actor).filter(is_active=True)
        else:
            # The pair identifies the conversion; changing it would silently
            # move a version history onto a different package.
            self.fields["item"].disabled = True
            self.fields["package_unit"].disabled = True
            items = InventoryItem.objects.filter(pk=instance.item_id)
            packages = PackageUnit.objects.filter(pk=instance.package_unit_id)
        self.fields["item"].queryset = items.order_by("code")  # type: ignore[attr-defined]
        self.fields["package_unit"].queryset = packages.order_by("code")  # type: ignore[attr-defined]

    def _decimal(self, raw: str, field: str) -> Decimal | None:
        """Parse an exact decimal, or record an Arabic error and return None."""
        text = (raw or "").strip()
        if not text:
            return None
        if "," in text:
            self.add_error(field, _("استخدم النقطة العشرية لا الفاصلة."))
            return None
        try:
            value = Decimal(text)
        except ArithmeticError, ValueError:
            self.add_error(field, _("قيمة عشرية غير صالحة."))
            return None
        if not value.is_finite():
            self.add_error(field, _("قيمة عشرية غير صالحة."))
            return None
        return value

    def clean_factor_to_base(self) -> Decimal | None:
        value = self._decimal(self.cleaned_data.get("factor_to_base", ""), "factor_to_base")
        if value is not None and value <= 0:
            self.add_error("factor_to_base", _("يجب أن يكون المعامل أكبر من صفر."))
            return None
        return value

    def clean_minimum_increment(self) -> Decimal | None:
        value = self._decimal(self.cleaned_data.get("minimum_increment", ""), "minimum_increment")
        if value is not None and value <= 0:
            self.add_error("minimum_increment", _("يجب أن يكون أكبر من صفر."))
            return None
        return value

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean()  # type: ignore[assignment]
        starts: datetime.date | None = cleaned.get("effective_from")
        ends: datetime.date | None = cleaned.get("effective_to")
        if starts and ends and ends < starts:
            self.add_error("effective_to", _("تاريخ الانتهاء قبل تاريخ البدء."))
        return cleaned


class SupersedeConversionForm(ScopedForm):
    """
    Close a conversion and open its successor.

    Separate from the edit form on purpose. A supplier moving from a 30 kg sack
    to a 25 kg sack is a **new packaging fact**, not a correction: editing the
    factor in place would restate every movement already posted under the old
    sack. This form only asks for the new factor and the day it starts.
    """

    scope_permission = MANAGE_CONVERSIONS

    factor_to_base = forms.CharField(
        label=_("المعامل الجديد"),
        help_text=_("رقم عشري بالنقطة. الإصدار الحالي يُغلق في اليوم السابق."),
    )
    effective_from = forms.DateField(
        label=_("سارٍ من"), widget=forms.DateInput(attrs={"type": "date"})
    )
    reason = forms.CharField(label=_("السبب"), max_length=200, required=False)

    def clean_factor_to_base(self) -> Decimal | None:
        text = str(self.cleaned_data.get("factor_to_base", "")).strip()
        if "," in text:
            self.add_error("factor_to_base", _("استخدم النقطة العشرية لا الفاصلة."))
            return None
        try:
            value = Decimal(text)
        except ArithmeticError, ValueError:
            self.add_error("factor_to_base", _("قيمة عشرية غير صالحة."))
            return None
        if not value.is_finite() or value <= 0:
            self.add_error("factor_to_base", _("يجب أن يكون المعامل أكبر من صفر."))
            return None
        return value

    def clean_reason(self) -> str:
        return str(self.cleaned_data.get("reason") or "")


# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------


#: The types an operator may choose. `IN_TRANSIT` is absent by construction:
#: it is created by `ensure_in_transit_warehouse` and never by hand, and a
#: user-creatable system warehouse would be a way to make an ordinary store
#: exempt from the rules that protect the ledger.
USER_CREATABLE_WAREHOUSE_TYPES = [
    (value, label) for value, label in WarehouseType.choices if value != WarehouseType.IN_TRANSIT
]


class WarehouseForm(ScopedForm):
    """Create or rename a warehouse in a branch the caller actually works at."""

    scope_permission = MANAGE_WAREHOUSES

    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    code = forms.CharField(
        label=_("الرمز"),
        max_length=32,
        help_text=_("يُوحَّد تلقائياً إلى حروف كبيرة. لا يمكن تغييره لاحقاً."),
    )
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنجليزية"), max_length=200, required=False)
    warehouse_type = forms.ChoiceField(choices=USER_CREATABLE_WAREHOUSE_TYPES, label=_("النوع"))
    is_active = forms.BooleanField(label=_("فعّال"), required=False, initial=True)

    def __init__(
        self, *args: Any, actor: User, instance: Warehouse | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.instance = instance
        if instance is None:
            self.fields["branch"].queryset = self.branch_choices()  # type: ignore[attr-defined]
        else:
            self.fields["code"].disabled = True
            self.fields["branch"].disabled = True
            # The type decides which rules a warehouse lives under. Changing it
            # after stock has a home would move that stock between rule sets.
            self.fields["warehouse_type"].disabled = True
            self.fields["branch"].queryset = Branch.objects.filter(pk=instance.branch_id)  # type: ignore[attr-defined]
            self.fields["warehouse_type"].choices = WarehouseType.choices  # type: ignore[attr-defined]

    def clean_name_en(self) -> str:
        return str(self.cleaned_data.get("name_en") or "")


# ---------------------------------------------------------------------------
# Task 1.3 — account-mapping overrides and opening stock
# ---------------------------------------------------------------------------


class InventoryMappingForm(ScopedForm):
    """
    An item- or category-specific override of the organization default.

    Exactly one target. The role list is limited to roles that permit
    overrides at all — offering INVENTORY_OPENING_EQUITY here would offer
    something the service refuses.
    """

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    account_role = forms.ModelChoiceField(
        queryset=AccountRole.objects.none(), label=_("الدور المحاسبي")
    )
    account = forms.ModelChoiceField(queryset=Account.objects.none(), label=_("الحساب"))
    item = forms.ModelChoiceField(
        queryset=InventoryItem.objects.none(),
        label=_("الصنف"),
        required=False,
        help_text=_("حدد صنفاً أو مجموعة — واحداً فقط."),
    )
    category = forms.ModelChoiceField(
        queryset=ItemCategory.objects.none(), label=_("المجموعة"), required=False
    )
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
        super().__init__(*args, actor=actor, **kwargs)
        from apps.accounting.models import AccountRoleMappingScope
        from apps.accounting.permissions import MANAGE_ACCOUNT_MAPPINGS

        organizations = organizations_with_permission(actor, MANAGE_ACCOUNT_MAPPINGS).order_by(
            "code"
        )
        self.fields["organization"].queryset = organizations  # type: ignore[attr-defined]
        self.fields["account_role"].queryset = AccountRole.objects.filter(  # type: ignore[attr-defined]
            is_active=True, mapping_scope=AccountRoleMappingScope.ITEM
        ).order_by("code")
        self.fields["account"].queryset = (  # type: ignore[attr-defined]
            Account.objects.filter(organization__in=organizations, is_postable=True, is_active=True)
            .select_related("organization")
            .order_by("organization__code", "code")
        )
        self.fields["item"].queryset = (  # type: ignore[attr-defined]
            InventoryItem.objects.filter(organization__in=organizations, is_active=True)
            .select_related("organization")
            .order_by("code")
        )
        self.fields["category"].queryset = (  # type: ignore[attr-defined]
            ItemCategory.objects.filter(organization__in=organizations, is_active=True)
            .select_related("organization")
            .order_by("code")
        )

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean()  # type: ignore[assignment]
        item = cleaned.get("item")
        category = cleaned.get("category")
        if (item is None) == (category is None):
            self.add_error(None, _("حدد صنفاً أو مجموعة — واحداً فقط، لا كليهما ولا لا شيء."))
        starts = cleaned.get("effective_from")
        ends = cleaned.get("effective_to")
        if starts and ends and ends < starts:
            self.add_error("effective_to", _("تاريخ الانتهاء قبل تاريخ البدء."))
        return cleaned


class OpeningDocumentForm(ScopedForm):
    """The opening document's header. The lines are added on the detail screen."""

    scope_permission = CREATE_OPENING_STOCK

    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    cutoff_at = forms.DateTimeField(
        label=_("لحظة الجرد"),
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text=_("اللحظة التي كان العدّ صحيحاً فيها. يُشتق منها يوم العمل."),
    )
    evidence_reference = forms.CharField(
        label=_("مرجع الإثبات"),
        max_length=200,
        help_text=_("رقم محضر الجرد الموقّع أو مرجع الملف."),
    )
    narration = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["branch"].queryset = self.branch_choices()  # type: ignore[attr-defined]

    def clean_narration(self) -> str:
        return str(self.cleaned_data.get("narration") or "")

    def clean_cutoff_at(self) -> datetime.datetime:
        from django.utils import timezone as django_timezone

        value: datetime.datetime = self.cleaned_data["cutoff_at"]
        if django_timezone.is_naive(value):
            # A datetime-local input carries no zone; Django's current zone —
            # the branches operate in Asia/Baghdad — is the honest reading.
            value = django_timezone.make_aware(value)
        return value


class OpeningLineForm(ScopedForm):
    """
    One counted position.

    Quantities and costs are typed as text and parsed as exact Decimals — a
    locale-aware number widget would accept a comma and change the count.
    A new lot may be named here, because opening stock legitimately introduces
    lots that predate the ledger.
    """

    scope_permission = CREATE_OPENING_STOCK

    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none(), label=_("المخزن"))
    item = forms.ModelChoiceField(queryset=InventoryItem.objects.none(), label=_("الصنف"))
    lot_code = forms.CharField(
        label=_("رمز الدفعة"),
        max_length=64,
        required=False,
        help_text=_("مطلوب للأصناف التي تتتبع الدفعات. الدفعة الجديدة تُنشأ من هنا."),
    )
    lot_expiry = forms.DateField(
        label=_("تاريخ انتهاء الدفعة"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    package_conversion = forms.ModelChoiceField(
        queryset=ItemPackageConversion.objects.none(),
        label=_("عبوة الإدخال"),
        required=False,
        help_text=_("اختياري: إن كان العدّ بالعبوات. يجب أن تعود للصنف نفسه."),
    )
    entered_package_quantity = forms.CharField(label=_("عدد العبوات"), required=False)
    measured_base_quantity = forms.CharField(
        label=_("الكمية الموزونة"),
        required=False,
        help_text=_("للعبوات المتغيرة فقط: قراءة الميزان بوحدة الأساس."),
    )
    base_quantity = forms.CharField(
        label=_("الكمية بوحدة الأساس"),
        required=False,
        help_text=_("للإدخال المباشر بلا عبوة. نقطة عشرية، لا فاصلة."),
    )
    unit_cost = forms.CharField(
        label=_("كلفة الوحدة"), help_text=_("دينار عراقي لوحدة الأساس الواحدة. نقطة عشرية.")
    )

    def __init__(self, *args: Any, actor: User, branch: Branch, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        from apps.organizations.authorization import accessible_warehouses

        self.fields["warehouse"].queryset = (  # type: ignore[attr-defined]
            accessible_warehouses(actor).filter(branch=branch, is_system=False).order_by("code")
        )
        self.fields["item"].queryset = (  # type: ignore[attr-defined]
            visible_items(actor)
            .filter(organization_id=branch.organization_id, is_active=True)
            .order_by("code")
        )
        self.fields["package_conversion"].queryset = (  # type: ignore[attr-defined]
            ItemPackageConversion.objects.filter(
                organization_id=branch.organization_id, is_active=True
            )
            .select_related("item", "package_unit")
            .order_by("item__code", "package_unit__code")
        )

    def _decimal(self, raw: str, field: str) -> Decimal | None:
        text = (raw or "").strip()
        if not text:
            return None
        if "," in text:
            self.add_error(field, _("استخدم النقطة العشرية لا الفاصلة."))
            return None
        try:
            value = Decimal(text)
        except ArithmeticError, ValueError:
            self.add_error(field, _("قيمة عشرية غير صالحة."))
            return None
        if not value.is_finite():
            self.add_error(field, _("قيمة عشرية غير صالحة."))
            return None
        return value

    def clean_entered_package_quantity(self) -> Decimal | None:
        return self._decimal(
            self.cleaned_data.get("entered_package_quantity", ""), "entered_package_quantity"
        )

    def clean_measured_base_quantity(self) -> Decimal | None:
        return self._decimal(
            self.cleaned_data.get("measured_base_quantity", ""), "measured_base_quantity"
        )

    def clean_base_quantity(self) -> Decimal | None:
        return self._decimal(self.cleaned_data.get("base_quantity", ""), "base_quantity")

    def clean_unit_cost(self) -> Decimal | None:
        value = self._decimal(self.cleaned_data.get("unit_cost", ""), "unit_cost")
        if value is not None and value <= 0:
            self.add_error("unit_cost", _("يجب أن تكون الكلفة أكبر من صفر."))
            return None
        return value


class ReasonForm(forms.Form):
    """A stated reason, for return-to-draft and reversal."""

    reason = forms.CharField(label=_("السبب"), widget=forms.Textarea, max_length=500)


# ---------------------------------------------------------------------------
# Task 1.4 — operational documents
# ---------------------------------------------------------------------------


class OperationalDocumentForm(ScopedForm):
    """
    The header of a receipt, an issue, or a return.

    One warehouse, one moment, one evidence reference. The cost-centre field
    appears only for an issue: a receipt has no consumption destination, and a
    return reuses the destination its original issue used.
    """

    scope_permission = CREATE_DRAFT_MOVEMENT

    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none(), label=_("المخزن"))
    effective_at = forms.DateTimeField(
        label=_("لحظة الحركة"),
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text=_("اللحظة الفعلية لدخول أو خروج البضاعة. يُشتق منها يوم العمل."),
    )
    evidence_reference = forms.CharField(
        label=_("مرجع الإثبات"),
        max_length=200,
        help_text=_("رقم إشعار التسليم أو طلب الصرف أو إشعار الإرجاع."),
    )
    cost_center = forms.ModelChoiceField(
        queryset=CostCenter.objects.none(),
        label=_("مركز الكلفة"),
        required=False,
        help_text=_("وجهة الاستهلاك. مطلوب إذا كان حساب الاستهلاك يستلزمه."),
    )
    narration = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(
        self, *args: Any, actor: User, document_type: str, branch: Branch, **kwargs: Any
    ) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        from apps.organizations.authorization import accessible_warehouses

        self.document_type = document_type
        self.branch = branch
        self.fields["warehouse"].queryset = (  # type: ignore[attr-defined]
            accessible_warehouses(actor).filter(branch=branch, is_system=False).order_by("code")
        )
        from apps.inventory.models import InventoryDocumentType

        if document_type == InventoryDocumentType.ISSUE:
            self.fields["cost_center"].queryset = CostCenter.objects.filter(  # type: ignore[attr-defined]
                organization_id=branch.organization_id, is_active=True
            ).order_by("code")
        else:
            # Not merely hidden: removed, so a hand-made POST cannot set one
            # on a document type the service would refuse it for.
            del self.fields["cost_center"]

    def clean_narration(self) -> str:
        return str(self.cleaned_data.get("narration") or "")

    def clean_effective_at(self) -> datetime.datetime:
        from django.utils import timezone as django_timezone

        value: datetime.datetime = self.cleaned_data["effective_at"]
        if django_timezone.is_naive(value):
            value = django_timezone.make_aware(value)
        return value


class OperationalLineForm(ScopedForm):
    """
    One line on an operational document.

    The cost field appears for a receipt only. An issue and a return are
    valued by the ledger, so offering a box for it would be offering an
    opinion the posting will ignore — or worse, refuse.

    The conversion selector is narrowed to the chosen item where the caller
    has already picked one, so the list is what applies rather than every
    conversion the organization has.
    """

    scope_permission = CREATE_DRAFT_MOVEMENT

    item = forms.ModelChoiceField(queryset=InventoryItem.objects.none(), label=_("الصنف"))
    lot_code = forms.CharField(
        label=_("رمز الدفعة"),
        max_length=64,
        required=False,
        help_text=_("مطلوب للأصناف التي تتتبع الدفعات."),
    )
    lot_expiry = forms.DateField(
        label=_("تاريخ انتهاء الدفعة"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    package_conversion = forms.ModelChoiceField(
        queryset=ItemPackageConversion.objects.none(),
        label=_("عبوة الإدخال"),
        required=False,
        help_text=_("اختياري: إن كان العدّ بالعبوات."),
    )
    entered_package_quantity = forms.CharField(label=_("عدد العبوات"), required=False)
    measured_base_quantity = forms.CharField(
        label=_("الكمية الموزونة"),
        required=False,
        help_text=_("للعبوات المتغيرة فقط: قراءة الميزان بوحدة الأساس."),
    )
    base_quantity = forms.CharField(
        label=_("الكمية بوحدة الأساس"),
        required=False,
        help_text=_("للإدخال المباشر بلا عبوة. نقطة عشرية، لا فاصلة."),
    )
    unit_cost = forms.CharField(
        label=_("كلفة الوحدة"),
        required=False,
        help_text=_("دينار عراقي لوحدة الأساس الواحدة. نقطة عشرية."),
    )
    source_issue_line = forms.ModelChoiceField(
        queryset=InventoryMovementDocumentLine.objects.none(),
        label=_("سطر الصرف الأصلي"),
        required=False,
        help_text=_("الصرف الذي تعود منه البضاعة."),
    )

    def __init__(
        self,
        *args: Any,
        actor: User,
        document: Any,
        selected_item: InventoryItem | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        from apps.inventory.commands import returnable_issue_lines
        from apps.inventory.models import InventoryDocumentType

        self.document = document
        self.fields["item"].queryset = (  # type: ignore[attr-defined]
            visible_items(actor)
            .filter(organization_id=document.organization_id, is_active=True)
            .order_by("code")
        )

        conversions = ItemPackageConversion.objects.filter(
            organization_id=document.organization_id, is_active=True
        ).select_related("item", "package_unit")
        if selected_item is not None:
            # Narrowed to the chosen item: a list of every conversion in the
            # organization is a list of mostly wrong answers.
            conversions = conversions.filter(item=selected_item)
        self.fields["package_conversion"].queryset = conversions.order_by(  # type: ignore[attr-defined]
            "item__code", "package_unit__code"
        )

        if document.document_type == InventoryDocumentType.RECEIPT:
            del self.fields["source_issue_line"]
        else:
            del self.fields["unit_cost"]

        if document.document_type == InventoryDocumentType.RETURN_IN:
            sources = returnable_issue_lines(actor).filter(
                document__warehouse_id=document.warehouse_id
            )
            if selected_item is not None:
                sources = sources.filter(item=selected_item)
            self.fields["source_issue_line"].queryset = sources  # type: ignore[attr-defined]
            self.fields["source_issue_line"].required = True
        elif "source_issue_line" in self.fields:
            del self.fields["source_issue_line"]

    def _decimal(self, raw: str, field: str) -> Decimal | None:
        text = (raw or "").strip()
        if not text:
            return None
        if "," in text:
            self.add_error(field, _("استخدم النقطة العشرية لا الفاصلة."))
            return None
        try:
            value = Decimal(text)
        except ArithmeticError, ValueError:
            self.add_error(field, _("قيمة عشرية غير صالحة."))
            return None
        if not value.is_finite():
            self.add_error(field, _("قيمة عشرية غير صالحة."))
            return None
        return value

    def clean_entered_package_quantity(self) -> Decimal | None:
        return self._decimal(
            self.cleaned_data.get("entered_package_quantity", ""), "entered_package_quantity"
        )

    def clean_measured_base_quantity(self) -> Decimal | None:
        return self._decimal(
            self.cleaned_data.get("measured_base_quantity", ""), "measured_base_quantity"
        )

    def clean_base_quantity(self) -> Decimal | None:
        return self._decimal(self.cleaned_data.get("base_quantity", ""), "base_quantity")

    def clean_unit_cost(self) -> Decimal | None:
        value = self._decimal(self.cleaned_data.get("unit_cost", ""), "unit_cost")
        if value is not None and value <= 0:
            self.add_error("unit_cost", _("يجب أن تكون الكلفة أكبر من صفر."))
            return None
        return value
