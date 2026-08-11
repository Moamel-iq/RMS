"""
Procurement forms. They validate and normalise; they never save.

Every form takes the `actor` and narrows its own choices from that caller's
scope, so a submitted id cannot widen access. `save()` is deliberately absent:
the view calls a service, which is the only place a write happens.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.inventory.models import InventoryItem, PackageUnit, StockLocation, Warehouse
from apps.inventory.selectors import reachable_organization_ids
from apps.organizations.authorization import (
    accessible_warehouses,
    organizations_with_permission,
)
from apps.organizations.models import Organization
from apps.procurement.models import (
    PurchaseRequest,
    PurchaseRequestStatus,
    Supplier,
    SupplierItem,
)
from apps.procurement.permissions import MANAGE_SUPPLIERS
from apps.procurement.selectors import visible_suppliers
from apps.procurement.services import canonical_code
from apps.users.models import User
from apps.users.phone import normalize_iraqi_mobile


class SupplierForm(forms.Form):
    """
    Create or correct a supplier.

    The organization field appears only when creating. Moving an existing
    supplier between organizations would carry its whole document history
    across a tenancy boundary, so the field is simply absent on edit rather
    than present and disabled — a disabled field is still submitted, and the
    view would have to remember to ignore it.
    """

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(label=_("الرمز"), max_length=32)
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنكليزية"), max_length=200, required=False)
    contact_name = forms.CharField(label=_("جهة الاتصال"), max_length=200, required=False)
    phone = forms.CharField(label=_("الهاتف"), max_length=20, required=False)
    email = forms.EmailField(label=_("البريد الإلكتروني"), required=False)
    address = forms.CharField(label=_("العنوان"), required=False, widget=forms.Textarea)
    payment_terms_days = forms.IntegerField(
        label=_("مهلة السداد (يوم)"),
        min_value=0,
        max_value=365,
        initial=0,
        help_text=_("صفر يعني الدفع عند الاستلام."),
    )
    credit_limit = forms.DecimalField(
        label=_("سقف الائتمان"),
        min_value=0,
        required=False,
        help_text=_("اتركه فارغاً إذا لم يكن هناك سقف متفق عليه."),
    )
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(
        self,
        *args: Any,
        actor: User,
        instance: Supplier | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance

        if instance is not None:
            # The code identifies a supplier every posted document already
            # points at. Correcting a typo is a data migration, not an edit.
            del self.fields["organization"]
            self.fields["code"].disabled = True
            self.fields["code"].initial = instance.code
            return

        self.fields["organization"].queryset = organizations_with_permission(  # type: ignore[attr-defined]
            actor, MANAGE_SUPPLIERS
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
            and Supplier.objects.filter(organization_id=organization_id, code=code).exists()
        ):
            raise forms.ValidationError(
                _("الرمز %(code)s مستخدم في هذه المؤسسة.") % {"code": code},
                code="code_taken",
            )
        return code

    def clean_phone(self) -> str:
        value = self.cleaned_data.get("phone", "").strip()
        if not value:
            return ""
        # Raises a ValidationError in Arabic, which the form renders inline.
        return normalize_iraqi_mobile(value)

    def selected_organization(self) -> Organization:
        organization: Organization = self.cleaned_data["organization"]
        return organization


class SupplierActionForm(forms.Form):
    """Archive or reactivate. A reason is required and is audited."""

    reason = forms.CharField(label=_("السبب"), max_length=500)

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor

    def visible(self) -> Any:
        return visible_suppliers(self.actor)


class SupplierItemForm(forms.Form):
    """
    One supplier terms row for one item.

    The package choices are narrowed to packages the **item** has a conversion
    for, because a row naming any other package could never be received. The
    service checks it again: a select element is a convenience, never a
    control.
    """

    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), label=_("المورد"))
    item = forms.ModelChoiceField(queryset=InventoryItem.objects.none(), label=_("الصنف"))
    package_unit = forms.ModelChoiceField(
        queryset=PackageUnit.objects.none(),
        label=_("وحدة الشراء"),
        required=False,
        help_text=_("اتركها فارغة إذا كان الشراء بوحدة الصنف الأساسية."),
    )
    supplier_sku = forms.CharField(
        label=_("رمز المورد للصنف"),
        max_length=64,
        required=False,
        help_text=_("يُحفظ كما كتبه المورد. لا يُحوَّل إلى أحرف كبيرة."),
    )
    supplier_description = forms.CharField(label=_("وصف المورد"), max_length=200, required=False)
    last_quoted_price = forms.DecimalField(
        label=_("آخر سعر معروض"),
        min_value=0,
        required=False,
        help_text=_("للتخطيط فقط. لا يُستخدم في تقييم المخزون إطلاقاً."),
    )
    lead_time_days = forms.IntegerField(
        label=_("مهلة التوريد (يوم)"), min_value=0, max_value=365, required=False
    )
    minimum_order_quantity = forms.DecimalField(
        label=_("أقل كمية طلب"), min_value=0, required=False
    )
    is_preferred = forms.BooleanField(label=_("المورد المفضل لهذا الصنف"), required=False)
    effective_from = forms.DateField(
        label=_("ساري من"), widget=forms.DateInput(attrs={"type": "date"})
    )
    effective_to = forms.DateField(
        label=_("ساري حتى"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("اتركه فارغاً إذا لم يكن هناك تاريخ انتهاء."),
    )
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(
        self,
        *args: Any,
        actor: User,
        instance: SupplierItem | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance

        reachable = reachable_organization_ids(actor)
        self.fields["supplier"].queryset = Supplier.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=reachable, is_active=True
        ).order_by("code")
        self.fields["item"].queryset = InventoryItem.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=reachable, is_active=True
        ).order_by("code")
        # Only packages some item in reach actually converts. The service
        # narrows it again to the chosen item, which is the check that counts.
        self.fields["package_unit"].queryset = (  # type: ignore[attr-defined]
            PackageUnit.objects.filter(
                organization_id__in=reachable,
                is_active=True,
                item_conversions__is_active=True,
            )
            .distinct()
            .order_by("code")
        )

        if instance is not None:
            # Supplier, item, package and start date identify the row. Changing
            # one makes it a different row, which is what superseding is for.
            for name in ("supplier", "item", "package_unit", "effective_from"):
                self.fields[name].disabled = True

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        start, end = data.get("effective_from"), data.get("effective_to")
        if start and end and end < start:
            raise forms.ValidationError(
                _("تاريخ الانتهاء قبل تاريخ البداية."), code="period_reversed"
            )
        return data


class PurchaseRequestForm(forms.Form):
    """The header of a draft request. Lines are added on the detail screen."""

    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none(), label=_("المخزن المستلم"))
    location = forms.ModelChoiceField(
        queryset=StockLocation.objects.none(),
        label=_("الموقع داخل المخزن"),
        required=False,
        help_text=_("اختياري. اتركه فارغاً إذا لم تكن المواقع مستخدمة."),
    )
    required_date = forms.DateField(
        label=_("مطلوب بتاريخ"), widget=forms.DateInput(attrs={"type": "date"})
    )
    purpose = forms.CharField(label=_("الغرض"), max_length=200)
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        warehouses = accessible_warehouses(actor).filter(is_active=True, is_system=False)
        self.fields["warehouse"].queryset = warehouses.order_by("code")  # type: ignore[attr-defined]
        self.fields["location"].queryset = StockLocation.objects.filter(  # type: ignore[attr-defined]
            warehouse__in=warehouses, is_active=True
        ).order_by("warehouse__code", "code")

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        warehouse, location = data.get("warehouse"), data.get("location")
        if warehouse and location and location.warehouse_id != warehouse.pk:
            raise forms.ValidationError(
                _("الموقع لا يتبع المخزن المختار."), code="location_warehouse_mismatch"
            )
        return data


class PurchaseRequestLineForm(forms.Form):
    """
    One wanted item, in the unit the requester thinks in.

    The package list is every package in reach; the service narrows it to one
    the chosen item can actually convert, and rejects the rest. A select is a
    convenience, never a control.
    """

    item = forms.ModelChoiceField(queryset=InventoryItem.objects.none(), label=_("الصنف"))
    package_unit = forms.ModelChoiceField(
        queryset=PackageUnit.objects.none(),
        label=_("العبوة"),
        required=False,
        help_text=_("اتركها فارغة للطلب بوحدة الصنف الأساسية."),
    )
    entered_quantity = forms.DecimalField(label=_("الكمية"), min_value=Decimal("0.001"))
    preferred_supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.none(), label=_("المورد المقترح"), required=False
    )
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, request_document: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.request_document = request_document
        organization_id = request_document.organization_id

        self.fields["item"].queryset = InventoryItem.objects.filter(  # type: ignore[attr-defined]
            organization_id=organization_id, is_active=True
        ).order_by("code")
        self.fields["package_unit"].queryset = PackageUnit.objects.filter(  # type: ignore[attr-defined]
            organization_id=organization_id, is_active=True
        ).order_by("code")
        self.fields["preferred_supplier"].queryset = Supplier.objects.filter(  # type: ignore[attr-defined]
            organization_id=organization_id, is_active=True
        ).order_by("code")


class SupplierQuotationForm(forms.Form):
    """The header of a draft quotation. Lines are priced on the detail screen."""

    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), label=_("المورد"))
    request = forms.ModelChoiceField(
        queryset=PurchaseRequest.objects.none(),
        label=_("طلب الشراء"),
        required=False,
        help_text=_("اختياري. يمكن تسجيل سعر قبل وجود طلب رسمي."),
    )
    supplier_reference = forms.CharField(
        label=_("رقم عرض المورد"),
        max_length=64,
        required=False,
        help_text=_("يُحفظ كما كتبه المورد. لا يتكرر لنفس المورد."),
    )
    quoted_at = forms.DateField(
        label=_("تاريخ العرض"), widget=forms.DateInput(attrs={"type": "date"})
    )
    valid_until = forms.DateField(
        label=_("صالح حتى"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    freight_amount = forms.DecimalField(
        label=_("أجور النقل"), min_value=0, initial=Decimal("0.000"), required=False
    )
    other_charges = forms.DecimalField(
        label=_("رسوم أخرى"), min_value=0, initial=Decimal("0.000"), required=False
    )
    evidence_reference = forms.CharField(
        label=_("مرجع الإثبات"),
        max_length=200,
        required=False,
        help_text=_("مطلوب قبل الاستلام: سعر لا يمكن ردّه إلى مستند هو إشاعة."),
    )
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        reachable = reachable_organization_ids(actor)
        self.fields["supplier"].queryset = Supplier.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=reachable, is_active=True
        ).order_by("code")
        # Only approved requests: quoting against something nobody agreed to
        # need is how a comparison ends up justifying a purchase that was never
        # asked for.
        self.fields["request"].queryset = PurchaseRequest.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=reachable, status=PurchaseRequestStatus.APPROVED
        ).order_by("-id")


class SupplierQuotationLineForm(forms.Form):
    """One priced item on a draft quotation."""

    item = forms.ModelChoiceField(queryset=InventoryItem.objects.none(), label=_("الصنف"))
    package_unit = forms.ModelChoiceField(
        queryset=PackageUnit.objects.none(),
        label=_("العبوة"),
        required=False,
        help_text=_("اتركها فارغة إذا كان السعر بوحدة الصنف الأساسية."),
    )
    quantity = forms.DecimalField(label=_("الكمية"), min_value=Decimal("0.001"))
    unit_price = forms.DecimalField(label=_("سعر الوحدة"), min_value=0)
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, quotation: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.quotation = quotation
        organization_id = quotation.organization_id
        self.fields["item"].queryset = InventoryItem.objects.filter(  # type: ignore[attr-defined]
            organization_id=organization_id, is_active=True
        ).order_by("code")
        self.fields["package_unit"].queryset = PackageUnit.objects.filter(  # type: ignore[attr-defined]
            organization_id=organization_id, is_active=True
        ).order_by("code")
