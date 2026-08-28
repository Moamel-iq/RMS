from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TimeStampedModel
from apps.core.money import MONEY_PLACES, UNIT_PRICE_PLACES, quantize_money
from apps.core.quantity import QUANTITY_PLACES

MONEY_DIGITS = MONEY_PLACES + 18


def quote_attachment_path(instance: SupplierQuoteAttachment, filename: str) -> str:
    return f"supplier-quotes/{instance.quote.organization_id}/{instance.quote.public_id}/{filename}"


class SupplierQuote(TimeStampedModel):
    """A supplier offer with a free-text supplier identity, never a Supplier FK."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="independent_supplier_quotes",
    )
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    supplier_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, blank=True)
    quote_date = models.DateField(default=timezone.localdate, db_index=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_independent_supplier_quotes",
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["-quote_date", "-id"]
        permissions = [
            ("download_supplier_quote_attachment", _("Can download supplier quotation attachments"))
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(supplier_name=""), name="supplier_quote_name_not_empty"
            )
        ]

    @property
    def total_amount(self) -> Decimal:
        return quantize_money(sum((line.line_total for line in self.lines.all()), Decimal("0.000")))

    @property
    def status_label(self) -> str:
        if self.lines.exists():
            return "عرض أصناف"
        if self.attachments.exists():
            return "مستند فقط"
        return "مسودة غير مكتملة"

    def __str__(self) -> str:
        return f"{self.supplier_name} · {self.quote_date}"


class SupplierQuoteLine(TimeStampedModel):
    quote = models.ForeignKey(SupplierQuote, on_delete=models.CASCADE, related_name="lines")
    sequence = models.PositiveIntegerField()
    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="independent_supplier_quote_lines",
    )
    unit = models.ForeignKey(
        "inventory.PackageUnit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="independent_supplier_quote_lines",
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=QUANTITY_PLACES)
    unit_price = models.DecimalField(max_digits=21, decimal_places=UNIT_PRICE_PLACES)
    line_total = models.DecimalField(max_digits=MONEY_DIGITS, decimal_places=MONEY_PLACES)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["quote", "sequence"], name="supplier_quote_line_sequence_unique"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="supplier_quote_line_quantity_positive"
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0), name="supplier_quote_line_unit_price_nonnegative"
            ),
        ]

    def clean(self) -> None:
        if (
            self.item_id
            and self.quote_id
            and self.item.organization_id != self.quote.organization_id
        ):
            raise ValidationError({"item": _("The item belongs to another organization.")})
        if self.unit_id and self.quote_id:
            unit = self.unit
            if unit is not None and unit.organization_id != self.quote.organization_id:
                raise ValidationError({"unit": _("The unit belongs to another organization.")})

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.line_total = quantize_money(self.quantity * self.unit_price)
        super().save(*args, **kwargs)


class SupplierQuoteAttachment(TimeStampedModel):
    quote = models.ForeignKey(SupplierQuote, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=quote_attachment_path)
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_supplier_quote_attachments",
    )

    class Meta:
        ordering = ["-created_at"]
