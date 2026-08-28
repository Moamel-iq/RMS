from __future__ import annotations

from typing import Any

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.supplier_quotes.models import SupplierQuote, SupplierQuoteAttachment, SupplierQuoteLine
from apps.users.models import User


@transaction.atomic
def add_line(*, quote: SupplierQuote, data: dict[str, Any]) -> SupplierQuoteLine:
    sequence = (
        quote.lines.order_by("-sequence").values_list("sequence", flat=True).first() or 0
    ) + 1
    line = SupplierQuoteLine(quote=quote, sequence=sequence, **data)
    line.full_clean()
    line.save()
    record_audit_event(action=AuditAction.CREATED, target=line, new_state=snapshot(line))
    return line


@transaction.atomic
def update_line(*, line: SupplierQuoteLine, data: dict[str, Any]) -> SupplierQuoteLine:
    previous = snapshot(line)
    for field, value in data.items():
        setattr(line, field, value)
    line.full_clean()
    line.save()
    record_audit_event(
        action=AuditAction.UPDATED, target=line, previous_state=previous, new_state=snapshot(line)
    )
    return line


@transaction.atomic
def remove_line(*, line: SupplierQuoteLine) -> None:
    previous = snapshot(line)
    line.delete()
    record_audit_event(action=AuditAction.DELETED, target=line, previous_state=previous)


@transaction.atomic
def replace_attachment(
    *, quote: SupplierQuote, uploaded: UploadedFile[Any], actor: User
) -> SupplierQuoteAttachment:
    for existing in quote.attachments.all():
        remove_attachment(attachment=existing)
    attachment = SupplierQuoteAttachment(
        quote=quote, file=uploaded, original_name=uploaded.name or "", uploaded_by=actor
    )
    attachment.full_clean()
    attachment.save()
    record_audit_event(
        action=AuditAction.CREATED, target=attachment, new_state=snapshot(attachment)
    )
    return attachment


@transaction.atomic
def remove_attachment(*, attachment: SupplierQuoteAttachment) -> None:
    previous = snapshot(attachment)
    attachment.file.delete(save=False)
    attachment.delete()
    record_audit_event(action=AuditAction.DELETED, target=attachment, previous_state=previous)
