from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from django import forms
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from apps.inventory.models import InventoryItem, PackageUnit
from apps.organizations.authorization import organizations_with_permission
from apps.organizations.models import Organization
from apps.supplier_quotes.models import SupplierQuote, SupplierQuoteLine
from apps.supplier_quotes.permissions import ADD
from apps.users.models import User
from apps.users.phone import normalize_iraqi_mobile

ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png"}
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


class SupplierQuoteForm(forms.ModelForm):  # type: ignore[type-arg]
    organization: forms.ModelChoiceField[Organization] = forms.ModelChoiceField(
        label="المنظمة", queryset=Organization.objects.none()
    )

    class Meta:
        model = SupplierQuote
        fields = ("supplier_name", "phone", "quote_date", "notes")
        widgets = {"quote_date": forms.DateInput(attrs={"type": "date"}), "notes": forms.Textarea}

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        organization_field = cast(
            "forms.ModelChoiceField[Organization]", self.fields["organization"]
        )
        if self.instance.pk:
            organization_field.queryset = Organization.objects.filter(
                pk=self.instance.organization_id
            )
            organization_field.initial = self.instance.organization_id
            organization_field.disabled = True
        else:
            organization_field.queryset = organizations_with_permission(actor, ADD).order_by("name")
        self.fields["quote_date"].initial = (
            self.instance.quote_date if self.instance.pk else timezone.localdate()
        )

    def clean_phone(self) -> str:
        value = self.cleaned_data.get("phone", "").strip()
        return normalize_iraqi_mobile(value) if value else ""


class SupplierQuoteLineForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = SupplierQuoteLine
        fields = ("item", "unit", "quantity", "unit_price", "notes")

    def __init__(self, *args: Any, quote: SupplierQuote, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        item_field = cast("forms.ModelChoiceField[InventoryItem]", self.fields["item"])
        unit_field = cast("forms.ModelChoiceField[PackageUnit]", self.fields["unit"])
        item_field.queryset = InventoryItem.objects.filter(
            organization=quote.organization, is_active=True
        ).order_by("code")
        unit_field.queryset = PackageUnit.objects.filter(
            organization=quote.organization, is_active=True
        ).order_by("code")


class SupplierQuoteAttachmentForm(forms.Form):
    file = forms.FileField(label="المستند")

    def clean_file(self) -> UploadedFile[Any]:
        uploaded = cast("UploadedFile[Any]", self.cleaned_data["file"])
        extension = Path(uploaded.name or "").suffix.lower()
        content_type = (uploaded.content_type or "").lower()
        if extension not in ALLOWED_EXTENSIONS or content_type not in ALLOWED_TYPES:
            raise ValidationError("ارفع ملف PDF أو صورة JPG/JPEG/PNG فقط.")
        if uploaded.size is not None and uploaded.size > MAX_ATTACHMENT_BYTES:
            raise ValidationError("حجم المرفق يجب ألا يتجاوز 10 MB.")
        return uploaded
