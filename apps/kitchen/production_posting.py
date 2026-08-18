"""
Posting a production batch, and reversing one.

Task 3.4 drafted; this posts. The boundary it crosses was a check constraint
named `production_batch_is_draft_only_until_task_3_5`, and migration 0017
removes it — deliberately, in its own migration, rather than by editing the one
that added it.

## What one posting is

One transaction consumes **every positive actual row**, produces the batch's one
output, draws the gapless number, writes the audit event, and writes the journal
if the per-account nets need one — or none of it. There is no half-posted state
and no partial completion: a Release 1 batch satisfies all seven conditions of
RCP-094, and the ones a service can enforce are enforced here by refusal rather
than by approximation (RCP-095).

## Each actual row is its own economic fact

Three kilos of the planned rice and one of an approved stand-in are two
consumptions, not one line with an adjustment. They are posted as separate
`PRODUCTION_OUT` movements, valued separately, and reported separately —
including when their units are in different dimensions, where the only honest
answer to "what was the variance" is that the quantities are not comparable
(Task 3.4 §28.3, unchanged here). Nothing in this module invents a KG-to-L
ratio, and nothing adds two rows whose base units disagree.

Rows are **not** aggregated by item either. The same item reached by two
component paths stays two requirements and two consumptions, because "was the
overspend in the dish or in the blend?" is the batch variance report's entire
subject and an aggregate answers it with a shrug.

## What this module does not decide

The arithmetic of valuation, the exact-depletion rule, negative-stock refusal,
lot expiry and the account an outbound leaves through all belong to the stock
ledger and stay there. This module says *what* was consumed and *what* was
produced; `apps.inventory.production` is the narrow public interface it says it
through, and that module is the only inventory posting service kitchen imports.

The journal likewise: production introduces **no new account role** (spec §15),
no WIP account, and no yield-variance account. Yield loss is absorbed into the
output's unit cost, which is the whole of RCP-035 — 50 kg of inputs worth 70,000
becoming 42 kg of rice makes the rice worth 70,000, and the unit cost says so.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import InventoryItem, InventoryLot, StockLocation, Warehouse
from apps.inventory.production import (
    ProductionConsumption,
    ProductionYield,
    post_production_entry,
    production_period,
    resolve_output_control_account,
    reverse_production_entry,
)
from apps.kitchen.models import (
    KitchenDocumentSequence,
    ProductionBatch,
    ProductionBatchActualLine,
    ProductionBatchAllocation,
    ProductionBatchStatus,
)

# The locking helpers and the refusal builder are `apps.kitchen.production`'s
# module-private names, and they are imported here rather than reimplemented on
# purpose: the underscore marks them as not part of the *package's* public API,
# and posting is the one caller that must take the batch, its requirements and
# its actual rows in exactly the order that module documents. A second copy of
# a lock order is a second chance to get it wrong.
from apps.kitchen.production import (  # noqa: PLC2701
    _lock_actuals,
    _lock_batch,
    _lock_lines,
    _refuse,
    validate_production_batch_ready,
)

if TYPE_CHECKING:
    from apps.organizations.models import Organization
    from apps.users.models import User

ZERO = Decimal("0")

#: The source document type this module writes on every stock entry and every
#: journal. Owned by `apps.kitchen`, following the module-constant pattern the
#: procurement and inventory modules use (spec §15).
SOURCE_DOCUMENT_TYPE = "KITCHEN_PRODUCTION_BATCH"

#: The sequence key, and the human prefix its numbers carry.
DOCUMENT_TYPE = "PRODUCTION_BATCH"
DOCUMENT_NUMBER_PREFIX = "PRD"

#: Named so a later change to the netting rule is visible on every entry it
#: produced, exactly as the inventory and procurement documents name theirs.
POSTING_RULE_VERSION = "kitchen-production-1"


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------


def next_production_number(*, organization: Organization, year: int) -> str:
    """
    The next gapless production number for this organization and year.

    Drawn **only** once every domain reason to refuse has already been checked,
    so a refused posting consumes nothing. A gapless sequence with gaps in it
    is worse than an honest one: it invites somebody to look for the missing
    document.
    """
    sequence, _created = KitchenDocumentSequence.objects.get_or_create(
        organization=organization, document_type=DOCUMENT_TYPE, year=year
    )
    locked = KitchenDocumentSequence.objects.select_for_update().get(pk=sequence.pk)
    locked.last_number += 1
    locked.save(update_fields=["last_number"])
    return f"{DOCUMENT_NUMBER_PREFIX}-{year}-{locked.last_number:06d}"


# ---------------------------------------------------------------------------
# Allocations — which lot, out of which bin
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllocationInput:
    """One line of an allocation request."""

    base_quantity: Decimal
    lot: InventoryLot | None = None
    location: StockLocation | None = None


def _lock_allocations(
    actuals: Sequence[ProductionBatchActualLine],
) -> list[ProductionBatchAllocation]:
    """
    Every allocation of these rows, in primary-key order, locked.

    `of=("self",)` because `lot` and `location` are both nullable: joining them
    for the `select_related` puts them on the outer side of a LEFT JOIN, and
    PostgreSQL refuses `FOR UPDATE` there. Only these rows need locking anyway
    — a lot row is master data this command does not change.
    """
    return list(
        ProductionBatchAllocation.objects.select_for_update(of=("self",))
        .select_related("lot", "location")
        .filter(actual__in=[row.pk for row in actuals])
        .order_by("pk")
    )


def _lock_actual_and_batch(actual_pk: int) -> tuple[ProductionBatch, ProductionBatchActualLine]:
    """
    The batch first, then the actual row — the module's documented lock order.

    Task 3.4 found three commands taking these the other way round and
    deadlocking against a concurrent rescale. The order is the batch, then its
    requirements, then their actual rows, then their allocations, always.
    """
    owner = (
        ProductionBatchActualLine.objects.filter(pk=actual_pk)
        .values_list("line__batch_id", flat=True)
        .first()
    )
    if owner is None:
        raise _refuse(_("سطر الاستهلاك لم يعد موجوداً."), "production_actual_no_longer_exists")
    batch = _lock_batch(owner)
    locked = (
        ProductionBatchActualLine.objects.select_for_update()
        .select_related("line", "line__batch", "item", "item__base_unit")
        .filter(pk=actual_pk)
        .first()
    )
    if locked is None:
        raise _refuse(_("سطر الاستهلاك لم يعد موجوداً."), "production_actual_no_longer_exists")
    return batch, locked


def _require_draft(batch: ProductionBatch) -> None:
    if batch.status == ProductionBatchStatus.POSTED:
        raise _refuse(_("هذه الدفعة مرحّلة ولا يمكن تعديلها."), "production_batch_already_posted")
    if batch.status == ProductionBatchStatus.REVERSED:
        raise _refuse(_("هذه الدفعة معكوسة ولا يمكن تعديلها."), "production_batch_is_reversed")


@transaction.atomic
def set_production_allocations(
    *,
    actual: ProductionBatchActualLine,
    rows: Sequence[AllocationInput],
) -> list[ProductionBatchAllocation]:
    """
    Replace one actual row's allocations with exactly these.

    Replace rather than append, because an allocation set is a single answer to
    a single question — "where did this quantity come from" — and appending
    would make a correction indistinguishable from a second consumption.

    The rows must sum to the actual row's own `base_quantity`. Allowing them to
    sum to less would let a batch post part of what it recorded consuming,
    which is a partial completion by another name (RCP-094 condition 3).
    """
    batch, locked = _lock_actual_and_batch(actual.pk)
    _require_draft(batch)
    _lock_allocations([locked])

    cleaned: list[AllocationInput] = []
    seen: set[tuple[int, int]] = set()
    for row in rows:
        quantity = quantize_quantity(row.base_quantity)
        if quantity <= ZERO:
            raise _refuse(
                _("كمية التخصيص يجب أن تكون أكبر من صفر."),
                "production_allocation_quantity_not_positive",
            )
        if row.lot is not None and row.lot.item_id != locked.item_id:
            raise _refuse(
                _("اللوط المختار لا يخص هذا الصنف."),
                "production_allocation_lot_item_mismatch",
            )
        if row.location is not None and row.location.warehouse_id != batch.warehouse_id:
            raise _refuse(
                _("الموقع المختار لا يخص مخزن هذه الدفعة."),
                "production_allocation_location_warehouse_mismatch",
            )
        position = (row.lot.pk if row.lot else 0, row.location.pk if row.location else 0)
        if position in seen:
            raise _refuse(
                _("لا يمكن تخصيص نفس اللوط والموقع مرتين لنفس السطر."),
                "production_allocation_position_repeated",
            )
        seen.add(position)
        cleaned.append(AllocationInput(base_quantity=quantity, lot=row.lot, location=row.location))

    total = quantize_quantity(sum((row.base_quantity for row in cleaned), ZERO))
    if cleaned and total != quantize_quantity(locked.base_quantity):
        raise _refuse(
            _("مجموع التخصيصات يجب أن يساوي الكمية المستهلكة تماماً."),
            "production_allocation_total_mismatch",
        )

    previous = snapshot(locked)
    locked.allocations.all().delete()
    written = [
        ProductionBatchAllocation.objects.create(
            actual=locked,
            allocation_order=order,
            lot=row.lot,
            location=row.location,
            base_quantity=row.base_quantity,
        )
        for order, row in enumerate(cleaned, start=1)
    ]
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=batch.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason="production allocation",
        metadata={"allocations": len(written), "batch": str(batch.public_id)},
    )
    return written


def allocation_is_required(actual: ProductionBatchActualLine) -> bool:
    """
    Whether this row must name its lots before the batch may post.

    A lot-tracked item, always: you cannot produce from a lot you did not name,
    and "roughly which batch" is not an answer a recall can use. Anything else,
    never — requiring an empty formality for an untracked item would be a form
    to fill in for the sake of the schema.
    """
    return bool(actual.item.tracks_lots) and quantize_quantity(actual.base_quantity) > ZERO


# ---------------------------------------------------------------------------
# The posting plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedConsumption:
    """One `PRODUCTION_OUT` this posting will make."""

    actual: ProductionBatchActualLine
    allocation: ProductionBatchAllocation | None
    item: InventoryItem
    quantity: Decimal
    lot: InventoryLot | None
    location: StockLocation | None
    effect_key: str


@dataclass(frozen=True)
class PostingPlan:
    """Everything a posting needs, resolved and refusable before anything moves."""

    batch: ProductionBatch
    warehouse: Warehouse
    consumptions: list[PlannedConsumption]
    output_item: InventoryItem
    output_quantity: Decimal

    @property
    def is_empty(self) -> bool:
        return not self.consumptions


def build_posting_plan(batch: ProductionBatch) -> PostingPlan:
    """
    What this batch would consume and produce, without moving anything.

    Public because the readiness panel shows it and the posting uses it, and a
    screen that previewed one plan while the command executed another would be
    the worst kind of surprise.
    """
    lines = list(
        batch.lines.select_related("item", "item__base_unit").prefetch_related(
            "actuals__item", "actuals__allocations__lot", "actuals__allocations__location"
        )
    )
    consumptions: list[PlannedConsumption] = []
    for line in lines:
        for actual in sorted(line.actuals.all(), key=lambda row: row.entry_order):
            quantity = quantize_quantity(actual.base_quantity)
            if quantity <= ZERO:
                # An optional requirement the kitchen skipped. Zero actual rows
                # create no movement — a zero-quantity effect would be a
                # movement that says nothing and reconciles against nothing.
                continue
            allocations = sorted(actual.allocations.all(), key=lambda row: row.allocation_order)
            if not allocations:
                consumptions.append(
                    PlannedConsumption(
                        actual=actual,
                        allocation=None,
                        item=actual.item,
                        quantity=quantity,
                        lot=None,
                        location=None,
                        effect_key=f"production-actual:{actual.public_id}",
                    )
                )
                continue
            for allocation in allocations:
                consumptions.append(
                    PlannedConsumption(
                        actual=actual,
                        allocation=allocation,
                        item=actual.item,
                        quantity=quantize_quantity(allocation.base_quantity),
                        lot=allocation.lot,
                        location=allocation.location,
                        effect_key=f"production-allocation:{allocation.public_id}",
                    )
                )

    output_item = batch.recipe.output_item
    if output_item is None:  # pragma: no cover - refused by readiness first
        raise _refuse(
            _("هذه الوصفة لا تنتج صنفاً مخزنياً."),
            "production_recipe_has_no_output_item",
        )
    return PostingPlan(
        batch=batch,
        warehouse=batch.warehouse,
        consumptions=consumptions,
        output_item=output_item,
        output_quantity=quantize_quantity(batch.actual_output_base_quantity or ZERO),
    )


def _validate_plan(plan: PostingPlan) -> None:
    """Everything about the plan that can be refused before any lock is taken."""
    if plan.is_empty:
        raise _refuse(
            _("لا يمكن ترحيل دفعة بلا استهلاك فعلي."),
            "production_batch_has_no_consumption",
        )
    if plan.output_quantity <= ZERO:
        raise _refuse(
            _("كمية الإنتاج الفعلية يجب أن تكون أكبر من صفر قبل الترحيل."),
            "production_output_not_positive",
        )
    for consumption in plan.consumptions:
        if consumption.item.tracks_lots and consumption.lot is None:
            raise _refuse(
                _("الصنف %(code)s يتتبع اللوطات ويجب تخصيص لوط قبل الترحيل.")
                % {"code": consumption.item.code},
                "production_lot_allocation_required",
            )
        if consumption.item.organization_id != plan.batch.organization_id:
            raise _refuse(  # pragma: no cover - refused at drafting
                _("صنف من منظمة أخرى."),
                "production_item_out_of_organization",
            )

    # Every allocation set sums to its actual row, checked again here against
    # persisted state. `set_production_allocations` checks it at write time;
    # this checks it at posting time, because between the two an actual row's
    # quantity may legitimately have been edited.
    totals: dict[int, Decimal] = {}
    for consumption in plan.consumptions:
        if consumption.allocation is None:
            continue
        totals[consumption.actual.pk] = (
            totals.get(consumption.actual.pk, ZERO) + consumption.quantity
        )
    for actual_pk, allocated in totals.items():
        recorded = next(
            quantize_quantity(row.actual.base_quantity)
            for row in plan.consumptions
            if row.actual.pk == actual_pk
        )
        if quantize_quantity(allocated) != recorded:
            raise _refuse(
                _("مجموع التخصيصات لا يساوي الكمية المستهلكة."),
                "production_allocation_total_mismatch",
            )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def posting_fingerprint(plan: PostingPlan) -> str:
    """
    What "the same posting request" means.

    The batch, the event, the business date, the entered output, and every
    immutable consumption fact this posting is about to move — so a retry after
    a timeout returns the posted batch, and a *changed* batch presented under
    the same key is a conflict rather than a silent no-op returning the wrong
    result.
    """
    payload = {
        "command": "post_production_batch",
        "batch": str(plan.batch.public_id),
        "event": "POSTED",
        "business_date": plan.batch.planned_business_date.isoformat(),
        "output_item": plan.output_item.pk,
        "output_quantity": str(plan.output_quantity),
        "rule": POSTING_RULE_VERSION,
        "consumptions": [
            {
                "actual": str(row.actual.public_id),
                "allocation": str(row.allocation.public_id) if row.allocation else None,
                "item": row.item.pk,
                "quantity": str(row.quantity),
                "lot": row.lot.pk if row.lot else None,
                "location": row.location.pk if row.location else None,
            }
            for row in sorted(plan.consumptions, key=lambda row: row.effect_key)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# The output lot
# ---------------------------------------------------------------------------


def _output_lot(batch: ProductionBatch, item: InventoryItem) -> InventoryLot | None:
    """
    The lot the produced goods enter, created here or returned if it exists.

    Creation is legitimate here and almost nowhere else: a produced lot is
    *caused* by this batch, which is why Phase 1 declared
    `produced_by_document_type` and `produced_by_document_id` with the comment
    that nothing wrote them yet. This writes them.

    Expiry follows the item's approved `shelf_life_days` from the batch's
    **business date**, never from today: a batch drafted on Monday for Sunday's
    production expires from Sunday.

    A second call returns the same lot rather than creating a second one, which
    is what makes a retried posting idempotent all the way down.
    """
    if not item.tracks_lots:
        return None
    code = f"{DOCUMENT_NUMBER_PREFIX}-{batch.public_id.hex[:12].upper()}"
    expiry: datetime.date | None = None
    if item.tracks_expiry and item.shelf_life_days is not None:
        expiry = batch.planned_business_date + datetime.timedelta(days=int(item.shelf_life_days))
    lot, created = InventoryLot.objects.get_or_create(
        item=item,
        code=code,
        defaults={
            "organization_id": item.organization_id,
            "expiry_date": expiry,
            "received_on": batch.planned_business_date,
            "produced_by_document_type": SOURCE_DOCUMENT_TYPE,
            "produced_by_document_id": str(batch.public_id),
        },
    )
    if created:
        record_audit_event(action=AuditAction.CREATED, target=lot, new_state=snapshot(lot))
    return lot


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@transaction.atomic
def post_production_batch(
    *,
    batch: ProductionBatch,
    idempotency_key: str,
    actor: User | None = None,
    reason: str = "",
) -> ProductionBatch:
    """
    Consume, produce, number, journal if needed, freeze — or none of it.

    The lock order is the module's own, taken outwards-in: the batch, its
    requirements, its actual rows, their allocations; then the account mapping
    and the stock keys, which `apps.inventory.production` takes in the kernel's
    canonical order. Nothing here takes a stock key before a kitchen row, so a
    posting racing an ordinary inventory issue cannot deadlock with it.

    Nothing is trusted from the caller except the batch's identity and the
    idempotency key. Status, quantities, output and values are all re-read from
    the database under lock.
    """
    if not idempotency_key.strip():
        raise _refuse(_("الترحيل يحتاج مفتاح تكرار."), "idempotency_key_required")

    locked = _lock_batch(batch.pk, require_draft=False)
    lines = _lock_lines(locked)
    actuals = _lock_actuals(lines)
    _lock_allocations(actuals)

    plan = build_posting_plan(locked)
    fingerprint = posting_fingerprint(plan)

    if locked.status != ProductionBatchStatus.DRAFT:
        return _replay_posting(locked, idempotency_key=idempotency_key, fingerprint=fingerprint)

    conflicting = (
        ProductionBatch.objects.filter(
            organization=locked.organization, post_idempotency_key=idempotency_key.strip()
        )
        .exclude(pk=locked.pk)
        .first()
    )
    if conflicting is not None:
        raise _refuse(
            _("هذا المفتاح مستخدم لترحيل دفعة أخرى."),
            "idempotency_key_conflict",
        )

    problems = validate_production_batch_ready(locked)
    if problems:
        raise ValidationError(
            _("هذه الدفعة غير جاهزة للترحيل: %(first)s") % {"first": str(problems[0].message)},
            code="production_batch_not_ready",
        )
    _validate_plan(plan)

    # Before the number is drawn, so a closed period costs no sequence value.
    period = production_period(
        organization=locked.organization, business_date=locked.planned_business_date
    )

    output_lot = _output_lot(locked, plan.output_item)
    control_account = resolve_output_control_account(
        organization=locked.organization,
        item=plan.output_item,
        on_date=locked.planned_business_date,
    )

    moment = timezone.now()
    posting = post_production_entry(
        organization=locked.organization,
        branch=locked.branch,
        consumptions=[
            ProductionConsumption(
                warehouse=plan.warehouse,
                item=row.item,
                quantity=row.quantity,
                effect_key=row.effect_key,
                lot=row.lot,
                location=row.location,
            )
            for row in plan.consumptions
        ],
        produced=ProductionYield(
            warehouse=plan.warehouse,
            item=plan.output_item,
            quantity=plan.output_quantity,
            effect_key=f"production-output:{locked.public_id}",
            lot=output_lot,
            control_account=control_account,
        ),
        business_date=locked.planned_business_date,
        effective_at=moment,
        idempotency_key=f"production:{locked.public_id}",
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        posting_rule_version=POSTING_RULE_VERSION,
        reference=locked.recipe.code,
        reason=reason or str(_("ترحيل أمر إنتاج")),
    )

    # Written back **while the batch is still DRAFT**, which is what the 0018
    # allocation guard permits; the header moves to POSTED below and freezes
    # them.
    for row in plan.consumptions:
        if row.allocation is None:
            continue
        movement = posting.movements[row.effect_key]
        row.allocation.movement = movement
        row.allocation.consumed_value = quantize_money(-movement.inventory_value)
        row.allocation.save(update_fields=["movement", "consumed_value", "updated_at"])

    previous = snapshot(locked)
    locked.number = next_production_number(
        organization=locked.organization, year=period.fiscal_year.year
    )
    locked.status = ProductionBatchStatus.POSTED
    locked.posted_at = moment
    locked.posted_by = actor
    locked.stock_entry = posting.entry
    locked.journal_entry = posting.journal
    locked.output_item = plan.output_item
    locked.output_lot = output_lot
    locked.output_movement = posting.output_movement
    locked.input_value = posting.consumed_value
    locked.output_value = posting.output_value
    locked.post_idempotency_key = idempotency_key.strip()
    locked.post_request_fingerprint = fingerprint
    locked.posting_rule_version = POSTING_RULE_VERSION
    locked.save(
        update_fields=[
            "number",
            "status",
            "posted_at",
            "posted_by",
            "stock_entry",
            "journal_entry",
            "output_item",
            "output_lot",
            "output_movement",
            "input_value",
            "output_value",
            "post_idempotency_key",
            "post_request_fingerprint",
            "posting_rule_version",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.POSTED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason or "production posting",
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "number": locked.number,
            "stock_entry": posting.entry.pk,
            "journal_entry": posting.journal.entry_number if posting.journal else None,
            "consumed_value": str(posting.consumed_value),
            "output_value": str(posting.output_value),
            "movements": len(plan.consumptions) + 1,
        },
    )
    return locked


def _replay_posting(
    batch: ProductionBatch, *, idempotency_key: str, fingerprint: str
) -> ProductionBatch:
    """
    A retry, a conflict, or an attempt to post twice — told apart by the key.

    Matching on the key **and** the fingerprint, never on the key alone: a
    caller that reused a key with corrected quantities and received the
    uncorrected posting would believe the correction had gone through.
    """
    if batch.status == ProductionBatchStatus.REVERSED:
        raise _refuse(_("هذه الدفعة معكوسة."), "production_batch_is_reversed")
    if batch.post_idempotency_key != idempotency_key.strip():
        raise _refuse(_("هذه الدفعة مرحّلة بالفعل."), "production_batch_already_posted")
    if batch.post_request_fingerprint != fingerprint:
        raise _refuse(
            _("نفس المفتاح مع طلب مختلف."),
            "idempotency_key_conflict",
        )
    return batch


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@transaction.atomic
def reverse_production_batch(
    *,
    batch: ProductionBatch,
    idempotency_key: str,
    reason: str,
    actor: User | None = None,
) -> ProductionBatch:
    """
    Mirror the posting exactly, once, with a reason.

    Every ingredient returns at the value it left at and the output leaves at
    the value it entered at. The kernel refuses when the produced goods are no
    longer there to take back, which is exactly the refusal a production
    reversal needs — and it refuses rather than driving the position negative,
    because "reverse the batch" must not become the standard way to create
    negative stock.

    A batch that correctly wrote no journal writes no reversal journal.
    """
    if not reason.strip():
        raise _refuse(_("العكس يحتاج سبباً."), "reason_required")
    if not idempotency_key.strip():
        raise _refuse(_("العكس يحتاج مفتاح تكرار."), "idempotency_key_required")

    locked = _lock_batch(batch.pk, require_draft=False)
    lines = _lock_lines(locked)
    _lock_actuals(lines)

    if locked.status == ProductionBatchStatus.REVERSED:
        raise _refuse(_("هذه الدفعة معكوسة بالفعل."), "production_batch_already_reversed")
    if locked.status != ProductionBatchStatus.POSTED:
        raise _refuse(_("لا يمكن عكس دفعة غير مرحّلة."), "production_batch_not_posted")
    assert locked.stock_entry is not None  # noqa: S101 - the evidence constraint holds it

    moment = timezone.now()
    business_date = timezone.localdate(moment)
    production_period(organization=locked.organization, business_date=business_date)

    reversal = reverse_production_entry(
        entry=locked.stock_entry,
        journal=locked.journal_entry,
        idempotency_key=idempotency_key.strip(),
        reason=reason.strip(),
        business_date=business_date,
        effective_at=moment,
    )

    previous = snapshot(locked)
    locked.status = ProductionBatchStatus.REVERSED
    locked.reversed_at = moment
    locked.reversed_by = actor
    locked.reversal_reason = reason.strip()
    locked.reversal_stock_entry = reversal.entry
    locked.reversal_journal_entry = reversal.journal
    locked.save(
        update_fields=[
            "status",
            "reversed_at",
            "reversed_by",
            "reversal_reason",
            "reversal_stock_entry",
            "reversal_journal_entry",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.REVERSED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "reversal_stock_entry": reversal.entry.pk,
            "reversal_journal": reversal.journal.entry_number if reversal.journal else None,
        },
    )
    return locked


__all__ = [
    "DOCUMENT_NUMBER_PREFIX",
    "DOCUMENT_TYPE",
    "POSTING_RULE_VERSION",
    "SOURCE_DOCUMENT_TYPE",
    "AllocationInput",
    "PlannedConsumption",
    "PostingPlan",
    "allocation_is_required",
    "build_posting_plan",
    "next_production_number",
    "post_production_batch",
    "posting_fingerprint",
    "reverse_production_batch",
    "set_production_allocations",
]
