"""
Attributing an Inventory document to a production batch, and un-attributing it.

Two commands, and neither one changes a number that anybody accounts for.

## What a link is for

A kitchen manager reading a batch asks "why did this cost more than the recipe
says?" and the answer is often a document standing next to the batch — the
spoiled trimmings written off that afternoon, the unopened spice carried back to
the store that evening. Both are real Phase 1 events with their own movements,
their own values and their own journals. Neither is discoverable *from the
batch*, because nothing joins them.

A link is that join, and it is only that join. It is explanatory attribution: it
makes a document findable from a batch and a batch findable from a document.

## What a link is emphatically not

**It does not correct a ledger.** `apps.inventory` does not import this module,
does not know it exists, and behaves identically whether every row is present or
every row is deleted (RCP-101).

**It does not change actual consumption.** `batch_actual_consumption` reads the
posted `PRODUCTION_OUT` movements and the recorded actual rows, and it does not
consult this table. Task 3.0 §11.2 originally defined batch consumption as
`consumed − linked returns + linked waste`; ADR-026 supersedes that. The reason
is arithmetic rather than taste: a posted batch's input value already equals its
output value to the fils (RCP-034), and the linked documents already moved their
own stock and wrote their own journals. Subtracting a custody transfer from the
batch's `PRODUCTION_OUT` would credit the same kilogram twice — once in the
transfer's ledger effect, once again in the report.

The one case where a linked document *does* change consumption arithmetic is
when it is a **genuine reversal** of the economic-use movement — and that case
needs no link at all, because `consumption.py` classifies reversals from
`StockMovement.reverses` and nets them automatically.

**It does not rewrite a posted batch.** If a batch's inputs were genuinely
wrong, the correction is to reverse it, fix the draft and repost (ADR-025 §7).

## The name says so

The link types are `CUSTODY_RETURN_CONTEXT` and `ABNORMAL_WASTE_CONTEXT`, not
`MATERIAL_RETURN` and `LINKED_WASTE`. Calling a custody transfer a "production
return" would tell every future reader that it reverses `PRODUCTION_OUT`, and
the first person to act on that reading would build a report that double-counts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.quantity import quantize_calculation
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import (
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryMovementDocumentLine,
    StockTransferLine,
    StockTransferStatus,
)
from apps.kitchen.models import (
    BatchDocumentLink,
    BatchDocumentLinkStatus,
    BatchLinkType,
    ProductionBatch,
    ProductionBatchActualLine,
    ProductionBatchLine,
    ProductionBatchStatus,
)

if TYPE_CHECKING:
    from apps.users.models import User

ZERO = Decimal("0")

#: A transfer whose dispatch actually moved stock. A draft transfer is somebody
#: planning to move goods, and a plan has attributed nothing.
_POSTED_TRANSFER_STATUSES = frozenset(
    {
        StockTransferStatus.DISPATCHED,
        StockTransferStatus.PARTIALLY_RECEIVED,
        StockTransferStatus.COMPLETED,
        StockTransferStatus.CLOSED_WITH_SHORTAGE,
    }
)

#: A batch that has actually posted. Attribution to a draft would be attribution
#: to something that consumed nothing.
_ATTRIBUTABLE_BATCH_STATUSES = frozenset(
    {ProductionBatchStatus.POSTED, ProductionBatchStatus.REVERSED}
)


def _refuse(message: Any, code: str) -> ValidationError:
    """A domain refusal with a stable code — 422 at the API, never a 500."""
    return ValidationError(message, code=code)


def _positive(value: object) -> Decimal:
    if isinstance(value, float):
        raise _refuse(_("استخدم Decimal وليس عدداً عشرياً ثنائياً."), "float_not_permitted")
    amount = Decimal(str(value))
    if amount <= ZERO:
        raise _refuse(_("الكمية المنسوبة يجب أن تكون أكبر من صفر."), "link_quantity_not_positive")
    return quantize_calculation(amount)


def _lock_transfer_line(line_id: int) -> StockTransferLine:
    """
    The transfer line, locked, with its parent readable.

    Locked because the attribution cap is read-then-write: without the lock two
    concurrent links each see room and together exceed the line. `of=("self",)`
    keeps the lock off the joined parent rows — PostgreSQL refuses `FOR UPDATE`
    on the nullable side of an outer join, and locking a whole transfer to
    attribute one of its lines would serialise unrelated work.
    """
    line = (
        StockTransferLine.objects.select_for_update(of=("self",))
        .select_related("transfer", "item", "transfer__source_warehouse")
        .filter(pk=line_id)
        .first()
    )
    if line is None:
        raise _refuse(_("سطر التحويل غير موجود."), "link_transfer_line_not_found")
    return line


def _lock_waste_line(line_id: int) -> InventoryMovementDocumentLine:
    """The waste document line, locked, for the same reason."""
    line = (
        InventoryMovementDocumentLine.objects.select_for_update(of=("self",))
        .select_related("document", "item", "document__warehouse")
        .filter(pk=line_id)
        .first()
    )
    if line is None:
        raise _refuse(_("سطر مستند الإتلاف غير موجود."), "link_waste_line_not_found")
    return line


def _attributed_so_far(*, transfer_line_id: int | None, waste_line_id: int | None) -> Decimal:
    """
    What every **live** link already claims from this source line.

    Cancelled links are excluded: a withdrawn claim releases its quantity, which
    is the whole reason cancellation exists rather than deletion.
    """
    rows = BatchDocumentLink.objects.filter(status=BatchDocumentLinkStatus.ACTIVE)
    rows = (
        rows.filter(transfer_line_id=transfer_line_id)
        if transfer_line_id is not None
        else rows.filter(waste_line_id=waste_line_id)
    )
    return quantize_calculation(
        sum((row.attributed_quantity for row in rows.only("attributed_quantity")), ZERO)
    )


def _check_batch_target(
    *,
    batch: ProductionBatch,
    line: ProductionBatchLine | None,
    actual_line: ProductionBatchActualLine | None,
) -> None:
    """A narrowing target must belong to the batch it narrows, and be alone."""
    if line is not None and actual_line is not None:
        raise _refuse(
            _("اربط السطر المخطط أو السطر الفعلي، لا الاثنين."),
            "link_batch_target_is_ambiguous",
        )
    if line is not None and line.batch_id != batch.pk:
        raise _refuse(_("السطر لا يخص هذه الدفعة."), "link_line_batch_mismatch")
    if actual_line is not None and actual_line.line.batch_id != batch.pk:
        raise _refuse(_("السطر الفعلي لا يخص هذه الدفعة."), "link_actual_batch_mismatch")


@transaction.atomic
def create_batch_document_link(
    *,
    batch: ProductionBatch,
    link_type: str,
    attributed_quantity: Decimal,
    reason: str,
    transfer_line: StockTransferLine | None = None,
    waste_line: InventoryMovementDocumentLine | None = None,
    line: ProductionBatchLine | None = None,
    actual_line: ProductionBatchActualLine | None = None,
    note: str = "",
    actor: User | None = None,
) -> BatchDocumentLink:
    """
    Attribute one posted Inventory line to one posted batch.

    Every check here is about whether the *claim* is coherent. None of them is
    about accounting, because the row changes no account: this command writes
    one table and touches no ledger, and the audit event says so explicitly so
    that the person who later asks "did this move stock?" finds the answer
    recorded rather than inferred.
    """
    if link_type not in BatchLinkType.values:
        raise _refuse(_("نوع ربط غير معروف."), "link_type_unknown")
    if not reason.strip():
        raise _refuse(_("الربط يحتاج سبباً."), "link_reason_required")
    if batch.status not in _ATTRIBUTABLE_BATCH_STATUSES:
        raise _refuse(
            _("لا يمكن الربط إلا بدفعة مرحّلة أو معكوسة."),
            "link_batch_is_not_posted",
        )
    quantity = _positive(attributed_quantity)
    _check_batch_target(batch=batch, line=line, actual_line=actual_line)

    # The type says which source family, and exactly one must arrive. The
    # database says the same thing in a constraint; this says it in Arabic.
    wants_transfer = link_type == BatchLinkType.CUSTODY_RETURN_CONTEXT
    if wants_transfer and (transfer_line is None or waste_line is not None):
        raise _refuse(
            _("سياق إرجاع العهدة يحتاج سطر تحويل واحداً فقط."),
            "link_expects_transfer_line",
        )
    if not wants_transfer and (waste_line is None or transfer_line is not None):
        raise _refuse(
            _("سياق الهالك يحتاج سطر مستند إتلاف واحداً فقط."),
            "link_expects_waste_line",
        )

    if transfer_line is not None:
        locked_transfer = _lock_transfer_line(transfer_line.pk)
        transfer = locked_transfer.transfer
        if transfer.organization_id != batch.organization_id:
            raise _refuse(_("التحويل لا يتبع منظمة هذه الدفعة."), "link_organization_mismatch")
        if transfer.status not in _POSTED_TRANSFER_STATUSES:
            raise _refuse(_("التحويل غير مرحّل."), "link_source_is_not_posted")
        # A *return* of custody leaves the kitchen store. An inbound transfer is
        # a different event and would need a different link type to describe it.
        if transfer.source_warehouse_id != batch.warehouse_id:
            raise _refuse(
                _("سطر التحويل لا يصدر من مخزن هذه الدفعة."),
                "link_source_warehouse_mismatch",
            )
        if transfer.source_branch_id != batch.branch_id:
            raise _refuse(_("فرع التحويل لا يطابق فرع الدفعة."), "link_branch_mismatch")
        source_line: Any = locked_transfer
        source_item = locked_transfer.item
        available = locked_transfer.base_quantity
        already = _attributed_so_far(transfer_line_id=locked_transfer.pk, waste_line_id=None)
    else:
        assert waste_line is not None  # noqa: S101 - guarded above
        locked_waste = _lock_waste_line(waste_line.pk)
        document = locked_waste.document
        if document.organization_id != batch.organization_id:
            raise _refuse(_("المستند لا يتبع منظمة هذه الدفعة."), "link_organization_mismatch")
        if document.document_type != InventoryDocumentType.WASTE:
            raise _refuse(_("المستند ليس مستند إتلاف."), "link_source_is_not_waste")
        if document.status != InventoryDocumentStatus.POSTED:
            raise _refuse(_("مستند الإتلاف غير مرحّل."), "link_source_is_not_posted")
        if document.warehouse_id != batch.warehouse_id:
            raise _refuse(
                _("مستند الإتلاف ليس على مخزن هذه الدفعة."),
                "link_source_warehouse_mismatch",
            )
        if document.branch_id != batch.branch_id:
            raise _refuse(_("فرع المستند لا يطابق فرع الدفعة."), "link_branch_mismatch")
        source_line = locked_waste
        source_item = locked_waste.item
        available = locked_waste.base_quantity
        already = _attributed_so_far(transfer_line_id=None, waste_line_id=locked_waste.pk)

    # Where the link narrows to a recorded actual, the items must be the same
    # thing. Attributing an onion waste line to a rice actual would put a
    # number in a row that cannot have produced it.
    if actual_line is not None and actual_line.item_id != source_item.pk:
        raise _refuse(_("صنف السطر الفعلي لا يطابق صنف المستند."), "link_item_mismatch")

    if already + quantity > available:
        raise _refuse(
            _("الكمية المنسوبة تتجاوز كمية سطر المستند المتاحة."),
            "link_attribution_exceeds_source",
        )

    if BatchDocumentLink.objects.filter(
        batch=batch,
        status=BatchDocumentLinkStatus.ACTIVE,
        **(
            {"transfer_line": source_line}
            if transfer_line is not None
            else {"waste_line": source_line}
        ),
    ).exists():
        raise _refuse(
            _("هذا السطر مرتبط بهذه الدفعة بالفعل. ألغِ الربط القائم ثم أنشئ ربطاً جديداً."),
            "link_already_exists_for_batch",
        )

    link = BatchDocumentLink.objects.create(
        organization_id=batch.organization_id,
        branch_id=batch.branch_id,
        warehouse_id=batch.warehouse_id,
        batch=batch,
        line=line,
        actual_line=actual_line,
        link_type=link_type,
        transfer_line=source_line if transfer_line is not None else None,
        waste_line=source_line if transfer_line is None else None,
        item=source_item,
        attributed_quantity=quantity,
        reason=reason.strip(),
        note=note.strip(),
        created_by=actor,
    )
    record_audit_event(
        action=AuditAction.CREATED,
        target=link,
        branch=batch.branch,
        new_state=snapshot(link),
        reason=reason.strip(),
        metadata={
            "link_type": link_type,
            "batch": batch.number,
            "item": source_item.code,
            "attributed_quantity": str(quantity),
            # Recorded rather than merely documented: this is what somebody
            # reads when they ask whether an attribution moved anything.
            "stock_effect": "none",
            "journal_effect": "none",
            "changes_batch_consumption": "no",
        },
    )
    return link


@transaction.atomic
def cancel_batch_document_link(
    *, link: BatchDocumentLink, reason: str, actor: User | None = None
) -> BatchDocumentLink:
    """
    Withdraw an attribution, with a reason that stays on the row forever.

    Cancellation, never deletion, and never an edit. The row is evidence that
    somebody made a claim; removing it would erase that there was ever a claim
    to withdraw, and editing it would silently restate a variance report that
    has already been read.

    The quantity returns to the source line's available attribution as soon as
    this commits, which is why `_attributed_so_far` counts `ACTIVE` rows only.
    """
    if not reason.strip():
        raise _refuse(_("الإلغاء يحتاج سبباً."), "reason_required")

    locked = BatchDocumentLink.objects.select_for_update().filter(pk=link.pk).first()
    if locked is None:
        raise _refuse(_("هذا الربط لم يعد موجوداً."), "link_no_longer_exists")
    if locked.status == BatchDocumentLinkStatus.CANCELLED:
        raise _refuse(_("هذا الربط ملغى بالفعل."), "link_already_cancelled")

    previous = snapshot(locked)
    locked.status = BatchDocumentLinkStatus.CANCELLED
    locked.cancelled_by = actor
    locked.cancelled_at = timezone.now()
    locked.cancellation_reason = reason.strip()
    locked.save(
        update_fields=[
            "status",
            "cancelled_by",
            "cancelled_at",
            "cancellation_reason",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.CANCELLED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
        metadata={"stock_effect": "none", "journal_effect": "none"},
    )
    return locked


def links_for_batch(batch: ProductionBatch) -> list[BatchDocumentLink]:
    """Every attribution on one batch, live and withdrawn, newest first."""
    return list(
        BatchDocumentLink.objects.filter(batch=batch)
        .select_related(
            "item",
            "item__base_unit",
            "transfer_line",
            "transfer_line__transfer",
            "waste_line",
            "waste_line__document",
            "created_by",
            "cancelled_by",
        )
        .order_by("-created_at", "-pk")
    )


def attribution_remaining(
    *,
    transfer_line: StockTransferLine | None = None,
    waste_line: InventoryMovementDocumentLine | None = None,
) -> Decimal:
    """
    How much of one source line is still unattributed.

    A read for a screen, so an operator sees the room before typing rather than
    after being refused. The command re-checks under a lock; this is a hint and
    is documented as one.
    """
    if transfer_line is not None:
        already = _attributed_so_far(transfer_line_id=transfer_line.pk, waste_line_id=None)
        return quantize_calculation(transfer_line.base_quantity - already)
    if waste_line is not None:
        already = _attributed_so_far(transfer_line_id=None, waste_line_id=waste_line.pk)
        return quantize_calculation(waste_line.base_quantity - already)
    return ZERO


__all__ = [
    "attribution_remaining",
    "cancel_batch_document_link",
    "create_batch_document_link",
    "links_for_batch",
]
