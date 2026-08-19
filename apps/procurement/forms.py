"""
Procurement forms. They validate and normalise; they never save.

Every form takes the `actor` and narrows its own choices from that caller's
scope, so a submitted id cannot widen access. `save()` is deliberately absent:
the view calls a service, which is the only place a write happens.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from django import forms
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import Account, CostCenter
from apps.inventory.models import (
    InventoryItem,
    InventoryLot,
    InventoryReasonCode,
    PackageUnit,
    StockLocation,
    Warehouse,
)
from apps.inventory.selectors import reachable_organization_ids
from apps.organizations.authorization import (
    accessible_warehouses,
    organizations_with_permission,
)
from apps.organizations.models import Branch, Organization
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchaseRequest,
    PurchaseRequestStatus,
    Supplier,
    SupplierCreditTerm,
    SupplierCreditTermStatus,
    SupplierInvoice,
    SupplierInvoiceCharge,
    SupplierInvoiceChargeAllocationBasis,
    SupplierInvoiceChargeCategory,
    SupplierInvoiceChargeTreatment,
    SupplierInvoiceLine,
    SupplierItem,
    SupplierPaymentMethod,
    SupplierQuotation,
    SupplierQuotationStatus,
    SupplierReturn,
    SupplierReturnLine,
)
from apps.procurement.permissions import (
    CREATE_SUPPLIER_CREDIT_NOTE,
    CREATE_SUPPLIER_CREDIT_TERM,
    CREATE_SUPPLIER_INVOICE,
    CREATE_SUPPLIER_PAYMENT,
    MANAGE_SUPPLIERS,
)
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
            # Terms are versioned and maker-checker controlled in their own
            # workspace. Leaving this integer editable here would create a
            # second source of truth beside SupplierCreditTerm.
            del self.fields["payment_terms_days"]
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


class SupplierCreditTermForm(forms.Form):
    """Create or edit one DRAFT effective-dated credit-term version."""

    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), label=_("المورد"))
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنكليزية"), max_length=200, required=False)
    net_days = forms.IntegerField(label=_("عدد أيام الائتمان"), min_value=0, max_value=3650)
    effective_from = forms.DateField(
        label=_("ساري من"), widget=forms.DateInput(attrs={"type": "date"})
    )
    effective_to = forms.DateField(
        label=_("ساري إلى"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(
        self,
        *args: Any,
        actor: User,
        instance: SupplierCreditTerm | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        if instance is None:
            organizations = organizations_with_permission(actor, CREATE_SUPPLIER_CREDIT_TERM)
            self.fields["supplier"].queryset = visible_suppliers(actor).filter(  # type: ignore[attr-defined]
                organization__in=organizations,
                is_active=True,
            )
            self.fields["effective_from"].initial = timezone.localdate()
        else:
            del self.fields["supplier"]

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        start = cleaned.get("effective_from")
        end = cleaned.get("effective_to")
        if start and end and end < start:
            self.add_error(
                "effective_to",
                forms.ValidationError(
                    _("تاريخ النهاية لا يمكن أن يسبق تاريخ البداية."),
                    code="credit_term_period_invalid",
                ),
            )
        return cleaned

    def selected_supplier(self) -> Supplier:
        if self.instance is not None:
            return self.instance.supplier
        return cast(Supplier, self.cleaned_data["supplier"])

    def selected_supersedes(self) -> SupplierCreditTerm | None:
        return (
            SupplierCreditTerm.objects.filter(
                supplier=self.selected_supplier(),
                status=SupplierCreditTermStatus.ACTIVE,
            )
            .order_by("-effective_from", "-version")
            .first()
        )


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


class PurchaseOrderForm(forms.Form):
    """The header of a draft order. Lines are agreed on the detail screen."""

    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), label=_("المورد"))
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none(), label=_("المخزن المستلم"))
    location = forms.ModelChoiceField(
        queryset=StockLocation.objects.none(), label=_("الموقع"), required=False
    )
    request = forms.ModelChoiceField(
        queryset=PurchaseRequest.objects.none(),
        label=_("طلب الشراء"),
        required=False,
        help_text=_("اختياري. الشراء المباشر من السوق لا يمر بطلب رسمي."),
    )
    quotation = forms.ModelChoiceField(
        queryset=SupplierQuotation.objects.none(),
        label=_("العرض المُرسى"),
        required=False,
    )
    ordered_on = forms.DateField(
        label=_("تاريخ الأمر"), widget=forms.DateInput(attrs={"type": "date"})
    )
    expected_on = forms.DateField(
        label=_("التسليم المتوقع"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    supplier_reference = forms.CharField(label=_("مرجع المورد"), max_length=64, required=False)
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        reachable = reachable_organization_ids(actor)
        warehouses = accessible_warehouses(actor).filter(is_active=True, is_system=False)

        self.fields["supplier"].queryset = Supplier.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=reachable, is_active=True
        ).order_by("code")
        self.fields["warehouse"].queryset = warehouses.order_by("code")  # type: ignore[attr-defined]
        self.fields["location"].queryset = StockLocation.objects.filter(  # type: ignore[attr-defined]
            warehouse__in=warehouses, is_active=True
        ).order_by("warehouse__code", "code")
        self.fields["request"].queryset = PurchaseRequest.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=reachable, status=PurchaseRequestStatus.APPROVED
        ).order_by("-id")
        # Only awarded offers. An order raised against an offer nobody chose
        # would make the award record meaningless.
        self.fields["quotation"].queryset = SupplierQuotation.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=reachable, status=SupplierQuotationStatus.AWARDED
        ).order_by("-id")

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        warehouse, location = data.get("warehouse"), data.get("location")
        if warehouse and location and location.warehouse_id != warehouse.pk:
            raise forms.ValidationError(
                _("الموقع لا يتبع المخزن المختار."), code="location_warehouse_mismatch"
            )
        return data


class PurchaseOrderLineForm(forms.Form):
    """One agreed item at one agreed price."""

    item = forms.ModelChoiceField(queryset=InventoryItem.objects.none(), label=_("الصنف"))
    package_unit = forms.ModelChoiceField(
        queryset=PackageUnit.objects.none(), label=_("العبوة"), required=False
    )
    ordered_quantity = forms.DecimalField(label=_("الكمية"), min_value=Decimal("0.001"))
    unit_price = forms.DecimalField(label=_("السعر المتفق"), min_value=0)
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, order: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.order = order
        organization_id = order.organization_id
        self.fields["item"].queryset = InventoryItem.objects.filter(  # type: ignore[attr-defined]
            organization_id=organization_id, is_active=True
        ).order_by("code")
        self.fields["package_unit"].queryset = PackageUnit.objects.filter(  # type: ignore[attr-defined]
            organization_id=organization_id, is_active=True
        ).order_by("code")


class GoodsReceiptForm(forms.Form):
    """The header of a draft receipt. Lines are entered on the detail screen."""

    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), label=_("المورد"))
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none(), label=_("المخزن المستلم"))
    location = forms.ModelChoiceField(
        queryset=StockLocation.objects.none(), label=_("الموقع"), required=False
    )
    order = forms.ModelChoiceField(
        queryset=PurchaseOrder.objects.none(),
        label=_("أمر الشراء"),
        required=False,
        help_text=_("اختياري. الشراء المباشر من السوق يصل بلا أمر."),
    )
    received_at = forms.DateField(
        label=_("تاريخ الاستلام"), widget=forms.DateInput(attrs={"type": "date"})
    )
    delivery_reference = forms.CharField(
        label=_("رقم إشعار التسليم"),
        max_length=64,
        required=False,
        help_text=_("رقم المورد كما كتبه. لا يتكرر لنفس المورد."),
    )
    evidence_reference = forms.CharField(label=_("مرجع الإثبات"), max_length=200, required=False)
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        reachable = reachable_organization_ids(actor)
        warehouses = accessible_warehouses(actor).filter(is_active=True, is_system=False)

        self.fields["supplier"].queryset = Supplier.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=reachable, is_active=True
        ).order_by("code")
        self.fields["warehouse"].queryset = warehouses.order_by("code")  # type: ignore[attr-defined]
        self.fields["location"].queryset = StockLocation.objects.filter(  # type: ignore[attr-defined]
            warehouse__in=warehouses, is_active=True
        ).order_by("warehouse__code", "code")
        # Only issued orders. A draft order has not been agreed with anybody,
        # and goods arriving against one mean somebody skipped a step.
        self.fields["order"].queryset = PurchaseOrder.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=reachable, status=PurchaseOrderStatus.ISSUED
        ).order_by("-id")

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        warehouse, location = data.get("warehouse"), data.get("location")
        if warehouse and location and location.warehouse_id != warehouse.pk:
            raise forms.ValidationError(
                _("الموقع لا يتبع المخزن المختار."), code="location_warehouse_mismatch"
            )
        return data


class GoodsReceiptLineForm(forms.Form):
    """One delivered item. The price comes from the order or is entered here."""

    item = forms.ModelChoiceField(queryset=InventoryItem.objects.none(), label=_("الصنف"))
    package_unit = forms.ModelChoiceField(
        queryset=PackageUnit.objects.none(), label=_("العبوة"), required=False
    )
    delivered_quantity = forms.DecimalField(label=_("الكمية المستلمة"), min_value=Decimal("0.001"))
    measured_base_quantity = forms.DecimalField(
        label=_("الوزن المقاس"),
        min_value=Decimal("0.001"),
        required=False,
        help_text=_("مطلوب للعبوات متغيرة الوزن: الميزان هو الكمية، لا المعامل."),
    )
    unit_price = forms.DecimalField(
        label=_("سعر الوحدة"),
        min_value=0,
        required=False,
        help_text=_("يُؤخذ من أمر الشراء عند ربطه، ويُدخل يدوياً بدونه."),
    )
    lot = forms.ModelChoiceField(
        queryset=InventoryLot.objects.none(), label=_("اللوط"), required=False
    )
    expiry_date = forms.DateField(
        label=_("تاريخ الانتهاء"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, receipt: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.receipt = receipt
        organization_id = receipt.organization_id
        self.fields["item"].queryset = InventoryItem.objects.filter(  # type: ignore[attr-defined]
            organization_id=organization_id, is_active=True
        ).order_by("code")
        self.fields["package_unit"].queryset = PackageUnit.objects.filter(  # type: ignore[attr-defined]
            organization_id=organization_id, is_active=True
        ).order_by("code")
        self.fields["lot"].queryset = InventoryLot.objects.filter(  # type: ignore[attr-defined]
            item__organization_id=organization_id
        ).order_by("item__code", "code")


class InspectLineForm(forms.Form):
    """
    How much of a delivered line is accepted.

    Rejected quantity is not a field: it is delivered minus accepted, derived
    by the service. Two numbers that must sum to a third is two chances to
    disagree with it.
    """

    accepted_base_quantity = forms.DecimalField(label=_("الكمية المقبولة"), min_value=0)
    rejection_reason = forms.ModelChoiceField(
        queryset=InventoryReasonCode.objects.none(),
        label=_("سبب الرفض"),
        required=False,
    )
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, receipt: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.fields["rejection_reason"].queryset = InventoryReasonCode.objects.filter(  # type: ignore[attr-defined]
            organization_id=receipt.organization_id, is_active=True
        ).order_by("code")


class SupplierInvoiceForm(forms.Form):
    """
    The header of a draft invoice. Lines are entered on the detail screen.

    `supplier` and `branch` narrow to organizations the caller holds real
    authority over, not merely reaches: an invoice commits the organization to
    paying somebody, and a branch membership is custody of a store.
    """

    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), label=_("المورد"))
    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع المحاسبي"))
    supplier_invoice_number = forms.CharField(
        label=_("رقم فاتورة المورد"),
        max_length=64,
        help_text=_("رقمهم كما كتبوه. لا يتكرر لنفس المورد — الحماية من الدفع مرتين."),
    )
    invoice_date = forms.DateField(
        label=_("تاريخ الفاتورة"), widget=forms.DateInput(attrs={"type": "date"})
    )
    business_date = forms.DateField(
        label=_("التاريخ المحاسبي"),
        widget=forms.DateInput(attrs={"type": "date"}),
        required=False,
        help_text=_("يوم العمل الذي يُرحّل فيه. يُترك فارغاً ليأخذ يوم الفرع الحالي."),
    )
    supplier_reference = forms.CharField(label=_("مرجع المورد"), max_length=200, required=False)
    currency_code = forms.ChoiceField(
        label=_("العملة"),
        choices=(("IQD", _("الدينار العراقي (IQD)")),),
        initial="IQD",
        help_text=_("المرحلة الحالية تعمل بالدينار العراقي فقط."),
    )
    freight_amount = forms.DecimalField(label=_("أجور النقل"), min_value=0, required=False)
    discount_amount = forms.DecimalField(label=_("الخصم"), min_value=0, required=False)
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        organizations = organizations_with_permission(actor, CREATE_SUPPLIER_INVOICE)
        supplier_scope = Q(organization__in=organizations, is_active=True)
        branch_scope = Q(organization__in=organizations, is_active=True)
        if instance is not None:
            supplier_scope |= Q(pk=instance.supplier_id)
            branch_scope |= Q(pk=instance.branch_id)
        self.fields["supplier"].queryset = Supplier.objects.filter(  # type: ignore[attr-defined]
            supplier_scope
        ).order_by("code")
        self.fields["branch"].queryset = Branch.objects.filter(  # type: ignore[attr-defined]
            branch_scope
        ).order_by("code")
        if instance is not None:
            # Changing either party changes the identity and accounting scope
            # of the document. Corrections may change only the draft header.
            self.fields["supplier"].disabled = True
            self.fields["branch"].disabled = True
            self.fields["freight_amount"].disabled = True
            self.fields["freight_amount"].help_text = _(
                "قيمة تاريخية فقط. أضف النقل الجديد من مساحة التكاليف الإضافية."
            )
        else:
            # New actual freight is a structured charge with explicit direct
            # or landed-cost treatment, never an ambiguous header amount.
            del self.fields["freight_amount"]

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        supplier, branch = data.get("supplier"), data.get("branch")
        if supplier and branch and branch.organization_id != supplier.organization_id:
            raise forms.ValidationError(
                _("الفرع لا يتبع مؤسسة المورد."), code="organization_mismatch"
            )
        return data


class InvoiceInventoryLineForm(forms.Form):
    """A charge for goods. References a delivery as evidence, never as a match."""

    item = forms.ModelChoiceField(queryset=InventoryItem.objects.none(), label=_("الصنف"))
    receipt_line = forms.ModelChoiceField(
        queryset=GoodsReceiptLine.objects.none(),
        label=_("سطر الاستلام"),
        required=False,
        help_text=_("إسناد فقط. المطابقة الثلاثية وتسوية الفروق تأتي في مهمة لاحقة."),
    )
    base_quantity = forms.DecimalField(
        label=_("الكمية بالوحدة الأساسية"), min_value=Decimal("0.001")
    )
    unit_price = forms.DecimalField(label=_("سعر الوحدة"), min_value=0)
    description = forms.CharField(label=_("البيان"), max_length=200, required=False)
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, invoice: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.invoice = invoice
        self.fields["item"].queryset = InventoryItem.objects.filter(  # type: ignore[attr-defined]
            organization_id=invoice.organization_id, is_active=True
        ).order_by("code")
        # Only posted deliveries from this supplier. A draft receipt has not
        # reached stock, and an invoice citing one would claim goods nobody
        # has confirmed arrived.
        self.fields["receipt_line"].queryset = (  # type: ignore[attr-defined]
            GoodsReceiptLine.objects.filter(
                receipt__organization_id=invoice.organization_id,
                receipt__supplier_id=invoice.supplier_id,
                receipt__status="POSTED",
            )
            .select_related("receipt", "item")
            .order_by("-receipt__id", "sequence")
        )


class InvoiceAccountLineForm(forms.Form):
    """A charge for something that never entered stock."""

    account = forms.ModelChoiceField(queryset=Account.objects.none(), label=_("الحساب"))
    cost_center = forms.ModelChoiceField(
        queryset=CostCenter.objects.none(), label=_("مركز التكلفة"), required=False
    )
    description = forms.CharField(label=_("البيان"), max_length=200)
    quantity = forms.DecimalField(
        label=_("الكمية"), min_value=Decimal("0.001"), initial=Decimal("1.000")
    )
    unit_price = forms.DecimalField(label=_("السعر"), min_value=0)
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, invoice: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.invoice = invoice
        from apps.procurement.invoices import DIRECT_LINE_ACCOUNT_CLASSES

        self.fields["account"].queryset = (  # type: ignore[attr-defined]
            Account.objects.filter(
                organization_id=invoice.organization_id,
                is_active=True,
                is_postable=True,
                account_class__in=DIRECT_LINE_ACCOUNT_CLASSES,
                role_mappings__isnull=True,
                inventory_mappings__isnull=True,
            )
            .order_by("code")
            .distinct()
        )
        self.fields["cost_center"].queryset = CostCenter.objects.filter(  # type: ignore[attr-defined]
            organization_id=invoice.organization_id, is_active=True
        ).order_by("code")


class InvoiceReversalForm(forms.Form):
    """A reversal states why. An unexplained one is a hole in the record."""

    reason = forms.CharField(label=_("السبب"), max_length=500)


class SupplierInvoiceChargeForm(forms.Form):
    """One structured actual cost; the service owns every posting rule."""

    category = forms.ChoiceField(
        label=_("نوع التكلفة"), choices=SupplierInvoiceChargeCategory.choices
    )
    treatment = forms.ChoiceField(
        label=_("المعالجة"), choices=SupplierInvoiceChargeTreatment.choices
    )
    description = forms.CharField(label=_("البيان"), max_length=200)
    amount = forms.DecimalField(label=_("المبلغ"), min_value=Decimal("0.001"))
    direct_account = forms.ModelChoiceField(
        queryset=Account.objects.none(), label=_("حساب التكلفة المباشرة"), required=False
    )
    cost_center = forms.ModelChoiceField(
        queryset=CostCenter.objects.none(), label=_("مركز التكلفة"), required=False
    )
    allocation_basis = forms.ChoiceField(
        label=_("أساس التوزيع"),
        choices=SupplierInvoiceChargeAllocationBasis.choices,
        initial=SupplierInvoiceChargeAllocationBasis.RECEIPT_VALUE,
    )
    evidence_reference = forms.CharField(label=_("مرجع الإثبات"), max_length=200, required=False)
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(
        self,
        *args: Any,
        actor: User,
        invoice: SupplierInvoice,
        instance: SupplierInvoiceCharge | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.invoice = invoice
        self.instance = instance
        from apps.procurement.invoices import DIRECT_LINE_ACCOUNT_CLASSES

        self.fields["direct_account"].queryset = (  # type: ignore[attr-defined]
            Account.objects.filter(
                organization_id=invoice.organization_id,
                is_active=True,
                is_postable=True,
                account_class__in=DIRECT_LINE_ACCOUNT_CLASSES,
                role_mappings__isnull=True,
                inventory_mappings__isnull=True,
            )
            .order_by("code")
            .distinct()
        )
        self.fields["cost_center"].queryset = CostCenter.objects.filter(  # type: ignore[attr-defined]
            organization_id=invoice.organization_id, is_active=True
        ).order_by("code")
        if instance is not None and not self.is_bound:
            self.initial.update(
                {
                    "category": instance.category,
                    "treatment": instance.treatment,
                    "description": instance.description,
                    "amount": instance.amount,
                    "direct_account": instance.direct_account_id,
                    "cost_center": instance.cost_center_id,
                    "allocation_basis": instance.allocation_basis,
                    "evidence_reference": instance.evidence_reference,
                    "notes": instance.notes,
                }
            )

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        if data.get("treatment") == SupplierInvoiceChargeTreatment.DIRECT_EXPENSE:
            if data.get("direct_account") is None:
                self.add_error("direct_account", _("اختر حساب التكلفة المباشرة."))
            if data.get("cost_center") is None:
                self.add_error("cost_center", _("اختر مركز التكلفة."))
        else:
            data["direct_account"] = None
            data["cost_center"] = None
        return data


class MatchAllocationForm(forms.Form):
    """
    One allocation: this much of that delivery covers this much of this line.

    Both selects narrow to the match's own invoice and supplier, so a
    submitted id cannot reach a delivery from somebody else — the service
    re-checks, but a form that offered the choice at all would be inviting the
    attempt.
    """

    invoice_line = forms.ModelChoiceField(
        queryset=SupplierInvoiceLine.objects.none(), label=_("سطر الفاتورة")
    )
    receipt_line = forms.ModelChoiceField(
        queryset=GoodsReceiptLine.objects.none(), label=_("سطر الاستلام")
    )
    matched_base_quantity = forms.DecimalField(
        label=_("الكمية المطابَقة بالوحدة الأساسية"),
        min_value=Decimal("0.001"),
        help_text=_("بالوحدة الأساسية دائماً — الكراتين والكيلوغرامات لا تُقارن مباشرة."),
    )
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, match: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.match = match
        self.fields["invoice_line"].queryset = (  # type: ignore[attr-defined]
            SupplierInvoiceLine.objects.filter(
                invoice=match.supplier_invoice, line_type="INVENTORY"
            )
            .select_related("item")
            .order_by("sequence")
        )
        # Posted deliveries from this supplier only. A draft receipt has no
        # value to match against, and a reversed one gave its stock back.
        self.fields["receipt_line"].queryset = (  # type: ignore[attr-defined]
            GoodsReceiptLine.objects.filter(
                receipt__organization_id=match.organization_id,
                receipt__supplier_id=match.supplier_id,
                receipt__status="POSTED",
                accepted_base_quantity__gt=Decimal("0.000"),
            )
            .select_related("receipt", "item")
            .order_by("-receipt__id", "sequence")
        )


class MatchCancellationForm(forms.Form):
    """Withdrawing an agreed answer states why."""

    reason = forms.CharField(label=_("السبب"), max_length=500)


class SupplierReturnForm(forms.Form):
    """
    The header of a draft return. Lines are entered on the detail screen.

    The delivery is the field, not the supplier: a return is always *of
    something that arrived*, and the service refuses one that cites no posted
    delivery. The supplier, warehouse and branch all follow from the receipt.
    """

    receipt = forms.ModelChoiceField(
        queryset=GoodsReceipt.objects.none(),
        label=_("التسليم المرتجع منه"),
        help_text=_("تسليم مرحّل فقط — المسودة لم تُدخل شيئاً إلى المخزون بعد."),
    )
    returned_at = forms.DateField(
        label=_("تاريخ الإرجاع"), widget=forms.DateInput(attrs={"type": "date"})
    )
    reason_code = forms.ModelChoiceField(
        queryset=InventoryReasonCode.objects.none(),
        label=_("رمز السبب"),
        required=False,
    )
    reason = forms.CharField(label=_("السبب"), required=False, widget=forms.Textarea)
    evidence_reference = forms.CharField(
        label=_("مرجع الإثبات"),
        max_length=120,
        required=False,
        help_text=_("وصل الاستلام أو توقيع السائق. مطلوب قبل الترحيل، لا قبل الحفظ."),
    )
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        warehouses = accessible_warehouses(actor)
        self.fields["receipt"].queryset = (  # type: ignore[attr-defined]
            GoodsReceipt.objects.filter(warehouse__in=warehouses, status=GoodsReceiptStatus.POSTED)
            .select_related("supplier", "warehouse")
            .order_by("-id")
        )
        self.fields["reason_code"].queryset = InventoryReasonCode.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=reachable_organization_ids(actor), is_active=True
        ).order_by("code")

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        receipt, reason_code = data.get("receipt"), data.get("reason_code")
        if receipt and reason_code and reason_code.organization_id != receipt.organization_id:
            raise forms.ValidationError(
                _("رمز السبب لا يتبع مؤسسة هذا التسليم."), code="reason_code_organization_mismatch"
            )
        return data


class SupplierCreditNoteForm(forms.Form):
    """
    The header of a draft credit note. Allocations are entered on the detail
    screen.

    The return is the field, not the supplier: a Release 1 note is always the
    answer to a claim, and the claim is a posted return's clearing balance.
    Everything else follows from it.
    """

    supplier_return = forms.ModelChoiceField(
        queryset=SupplierReturn.objects.none(),
        label=_("المرتجع المُسوّى"),
        help_text=_("مرتجع مرحّل لم يُسوَّ بعد — لكل مرتجع إشعار دائن قائم واحد."),
    )
    supplier_document_number = forms.CharField(
        label=_("رقم مستند المورد"),
        max_length=64,
        help_text=_("رقم الإشعار كما كتبه المورد. لا يتكرر لنفس المورد."),
    )
    credit_date = forms.DateField(
        label=_("تاريخ الإشعار"), widget=forms.DateInput(attrs={"type": "date"})
    )
    amount = forms.DecimalField(
        label=_("المبلغ المعتمد"),
        min_value=Decimal("0.001"),
        help_text=_("ما اعتمده المورد على ورقته — لا القيمة الدفترية ولا التوقع."),
    )
    reason = forms.CharField(label=_("السبب"), required=False, widget=forms.Textarea)
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        allowed = organizations_with_permission(actor, CREATE_SUPPLIER_CREDIT_NOTE)
        # Posted returns with no standing note: the one-note-per-return rule
        # offered rather than discovered on submit.
        self.fields["supplier_return"].queryset = (  # type: ignore[attr-defined]
            SupplierReturn.objects.filter(organization__in=allowed, status="POSTED")
            .exclude(credit_notes__status__in=("DRAFT", "POSTED"))
            .select_related("supplier", "receipt")
            .order_by("-id")
        )


class CreditAllocationForm(forms.Form):
    """This much of the note nets against that posted invoice."""

    invoice = forms.ModelChoiceField(queryset=SupplierInvoice.objects.none(), label=_("الفاتورة"))
    allocated_amount = forms.DecimalField(
        label=_("المبلغ المخصص"),
        min_value=Decimal("0.001"),
        help_text=_("لا يتجاوز رصيد الفاتورة ولا مبلغ الإشعار."),
    )
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, credit_note: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.credit_note = credit_note
        # This supplier's posted invoices only. The service re-checks; the
        # form simply does not offer somebody else's debt.
        self.fields["invoice"].queryset = (  # type: ignore[attr-defined]
            SupplierInvoice.objects.filter(supplier_id=credit_note.supplier_id, status="POSTED")
            .select_related("supplier")
            .order_by("-id")
        )


class SupplierPaymentForm(forms.Form):
    """
    The header of a draft payment. Allocation is explicit and lives on the
    detail screen (PRC-057: oldest-first is a visible default, never silent).
    """

    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), label=_("المورد"))
    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    paid_at = forms.DateField(
        label=_("تاريخ الدفع"), widget=forms.DateInput(attrs={"type": "date"})
    )
    method = forms.ChoiceField(label=_("طريقة الدفع"), choices=SupplierPaymentMethod.choices)
    amount = forms.DecimalField(
        label=_("المبلغ"),
        min_value=Decimal("0.001"),
        help_text=_("ما يُخصص منه على الفواتير يخفض الذمة؛ الباقي يقف سلفة للمورد."),
    )
    reference = forms.CharField(label=_("رقم الصك أو الحوالة"), max_length=64, required=False)
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea)

    def __init__(self, *args: Any, actor: User, instance: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.instance = instance
        allowed = organizations_with_permission(actor, CREATE_SUPPLIER_PAYMENT)
        self.fields["supplier"].queryset = (  # type: ignore[attr-defined]
            Supplier.objects.filter(organization__in=allowed, is_active=True).order_by("code")
        )
        self.fields["branch"].queryset = (  # type: ignore[attr-defined]
            Branch.objects.filter(organization__in=allowed, is_active=True).order_by("code")
        )

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        supplier, branch = data.get("supplier"), data.get("branch")
        if supplier and branch and supplier.organization_id != branch.organization_id:
            raise forms.ValidationError(
                _("المورد لا يتبع مؤسسة هذا الفرع."), code="supplier_organization_mismatch"
            )
        return data


class PaymentAllocationForm(forms.Form):
    """This much of the payment settles that posted invoice."""

    invoice = forms.ModelChoiceField(queryset=SupplierInvoice.objects.none(), label=_("الفاتورة"))
    allocated_amount = forms.DecimalField(
        label=_("المبلغ المخصص"),
        min_value=Decimal("0.001"),
        help_text=_("لا يتجاوز رصيد الفاتورة ولا مبلغ الدفعة."),
    )
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, payment: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.payment = payment
        # This supplier's posted invoices, oldest due first — PRC-057's
        # visible default, offered by ordering and never applied silently.
        self.fields["invoice"].queryset = (  # type: ignore[attr-defined]
            SupplierInvoice.objects.filter(supplier_id=payment.supplier_id, status="POSTED")
            .select_related("supplier")
            .order_by("due_date", "id")
        )


class CreditReturnAllocationForm(forms.Form):
    """This much of the note settles that much of one return line."""

    return_line = forms.ModelChoiceField(
        queryset=SupplierReturnLine.objects.none(), label=_("سطر المرتجع")
    )
    credited_base_quantity = forms.DecimalField(
        label=_("الكمية المعتمدة بالوحدة الأساسية"),
        min_value=Decimal("0.001"),
        help_text=_("لا تتجاوز المتبقي من كمية السطر المرتجعة."),
    )
    allocated_credit_amount = forms.DecimalField(
        label=_("المبلغ المعتمد لهذا السطر"),
        min_value=Decimal("0.001"),
        help_text=_("مجموع ما يُنسب للأسطر يجب أن يساوي مبلغ الإشعار عند الترحيل."),
    )
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, credit_note: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.credit_note = credit_note
        self.fields["return_line"].queryset = (  # type: ignore[attr-defined]
            SupplierReturnLine.objects.filter(supplier_return_id=credit_note.supplier_return_id)
            .select_related("item")
            .order_by("sequence")
        )


class SupplierReturnLineForm(forms.Form):
    """
    One delivery line going back, in part or in whole.

    The choices narrow to the return's own delivery, so a submitted id cannot
    cite somebody else's receipt — the service re-checks, but a form that
    offered the choice at all would be inviting the attempt.
    """

    receipt_line = forms.ModelChoiceField(
        queryset=GoodsReceiptLine.objects.none(), label=_("سطر التسليم")
    )
    returned_base_quantity = forms.DecimalField(
        label=_("الكمية المرتجعة بالوحدة الأساسية"),
        min_value=Decimal("0.001"),
        help_text=_("لا تتجاوز ما قُبل من ذلك السطر ناقص ما أُرجع منه سابقاً."),
    )
    expected_credit_value = forms.DecimalField(
        label=_("الائتمان المتوقع"),
        min_value=0,
        required=False,
        help_text=_("ما يُتوقع أن يقيّده المورد. معلومة للمطالبة، لا رقم القيد."),
    )
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, supplier_return: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.supplier_return = supplier_return
        # Lines that brought something into stock. A wholly rejected line
        # never entered and cannot be returned from it.
        self.fields["receipt_line"].queryset = (  # type: ignore[attr-defined]
            GoodsReceiptLine.objects.filter(
                receipt_id=supplier_return.receipt_id,
                accepted_base_quantity__gt=Decimal("0.000"),
            )
            .select_related("item")
            .order_by("sequence")
        )
