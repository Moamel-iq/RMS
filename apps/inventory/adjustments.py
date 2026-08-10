"""
Manual inventory adjustments: authorized exceptions, in three shapes.

An adjustment does not record something that happened to the goods. It records
that the **books were wrong**, which is a different claim and a more sensitive
one. Every other document in this module says where stock went; this one says
only that it was never where the ledger thought. So each line names a reason
code, the document names an evidence reference and a reason, and posting needs
`inventory.post_adjustment` — which a storekeeper does not hold.

## Why this is not an InventoryMovementDocument

One adjustment carries lines that go in **different directions**: a gain, a
loss, and a revaluation that moves no quantity at all. `InventoryMovementDocument`
maps one document type to exactly one movement type, and the only way to reuse
it would be to push the discriminator down onto the lines — which is the
type-field leak Task 1.4 avoided by keeping the type on the document.

`VALUE_ONLY` settles it on its own. There is no signed movement of goods that
expresses "this stock was always worth 40,000 less than we said", and forcing
it into a quantity document would mean inventing a quantity of zero and a
`unit_cost` of infinity.

## The three kinds

    QUANTITY_GAIN   +quantity, explicit unit cost, inbound moving average
    QUANTITY_LOSS   -quantity, standing average, full depletion at zero
    VALUE_ONLY       0 quantity, signed value, average recomputed over what is
                     already there

A gain needs its cost stated because there is no average to borrow for goods
the books do not have; defaulting to zero would book free stock. A zero cost is
allowed, but only when it is **confirmed** — an omitted answer and a deliberate
"worth nothing" must not be the same null.

## Accounting

    inventory up     Dr Inventory Control        Cr Inventory Adjustment
    inventory down   Dr Inventory Adjustment     Cr Inventory Control

One bidirectional variance account, resolved from `INVENTORY_ADJUSTMENT`. A
separate gain account and loss account were considered and rejected: the only
interesting question is what the adjustments came to, and a split pair has to
be netted in every report that asks it.

## Locking

    1. the document row                      select_for_update
    2. the warehouse freeze lock             advisory, shared, in the kernel
    3. the organization's mapping lock       shared
    4. every stock key, canonically          advisory
    5. the posted-order counter              inside the kernel
    6. the document-number sequence
    7. the journal-number sequence           inside post_entry
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    INVENTORY_ADJUSTMENT,
    INVENTORY_CONTROL,
    Account,
    AccountingPeriod,
    CostCenter,
    SourceEvent,
)
from apps.accounting.services import post_entry, resolve_period, reverse_entry
from apps.accounting.validators import PostingLine, validate_period_accepts_postings
from apps.core.context import get_actor
from apps.core.locks import lock_account_mappings_shared
from apps.core.models import AuditAction
from apps.core.money import quantize_money, quantize_unit_price
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.inventory.accounts import resolve_inventory_account
from apps.inventory.ledger import (
    EffectDirection,
    MovementInput,
    acquire_stock_key_locks,
    link_journal_entry,
    post_stock_entry,
    reverse_stock_entry,
)
from apps.inventory.models import (
    MANUAL_ADJUSTMENT_SOURCE_TYPE,
    AdjustmentLineKind,
    ConversionType,
    InventoryAdjustmentDocument,
    InventoryAdjustmentLine,
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryItem,
    InventoryLot,
    InventoryReasonCode,
    ItemPackageConversion,
    MovementType,
    ReasonCodeApplication,
    StockBalance,
    Warehouse,
)
from apps.inventory.operations import (
    next_document_number,
    require_cost_center_where_the_account_demands_one,
)
from apps.organizations.business_dates import resolve_business_day
from apps.organizations.models import Branch, Organization
from apps.users.models import User

ZERO = Decimal("0")

POSTING_RULE_ADJUSTMENT = "inventory-adjustment-v1"

#: The kinds that add value to a position. Used to pick the journal side and
#: nothing else — the stock arithmetic is chosen by `EffectDirection`.
_INCREASING_KINDS = frozenset({AdjustmentLineKind.QUANTITY_GAIN})


@dataclass(frozen=True)
class AdjustmentLineInput:
    """One requested correction, before validation derives its base quantity."""

    kind: str
    item: InventoryItem
    reason_code: InventoryReasonCode
    lot: InventoryLot | None = None
    package_conversion: ItemPackageConversion | None = None
    entered_package_quantity: Decimal | None = None
    measured_base_quantity: Decimal | None = None
    base_quantity: Decimal | None = None
    #: QUANTITY_GAIN only.
    unit_cost: Decimal | None = None
    zero_cost_confirmed: bool = False
    #: VALUE_ONLY only. Signed: positive writes the position up.
    value_adjustment: Decimal | None = None
    line_comment: str = ""


@dataclass
class _Resolved:
    """One line's resolved effect and account."""

    line: InventoryAdjustmentLine
    effect: MovementInput
    control_account: Account


def _actor() -> User:
    actor = get_actor()
    if actor is None:
        raise ValidationError(
            _("An adjustment needs a signed-in actor to record."), code="actor_required"
        )
    return actor


def _require_status(document: InventoryAdjustmentDocument, status: str, code: str) -> None:
    if document.status != status:
        raise ValidationError(
            _("Adjustment %(doc)s is %(actual)s."),
            code=code,
            params={
                "doc": document.document_number or str(document.public_id),
                "actual": document.get_status_display(),
            },
        )


def _period_for(*, organization: Organization, on_date: datetime.date) -> AccountingPeriod:
    period = resolve_period(organization=organization, accounting_date=on_date)
    validate_period_accepts_postings(period)
    return period


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


@transaction.atomic
def create_adjustment(
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    effective_at: datetime.datetime,
    evidence_reference: str,
    reason: str,
    cost_center: CostCenter | None = None,
) -> InventoryAdjustmentDocument:
    """Start a draft adjustment for one warehouse."""
    if branch.organization_id != organization.pk:
        raise ValidationError(
            _("Branch %(code)s belongs to another organization."),
            code="branch_organization_mismatch",
            params={"code": branch.code},
        )
    if warehouse.branch_id != branch.pk:
        raise ValidationError(
            _("Warehouse %(code)s belongs to another branch."),
            code="warehouse_branch_mismatch",
            params={"code": warehouse.code},
        )
    if not warehouse.is_active:
        raise ValidationError(
            _("Warehouse %(code)s is archived."),
            code="warehouse_inactive",
            params={"code": warehouse.code},
        )
    if warehouse.is_system:
        # In-transit value is corrected by reversing the transfer event that
        # produced it, which keeps the two branches' books in step. A manual
        # adjustment here would move value out of a position the transfer
        # aggregate still believes it owns (ADR-020 §4).
        raise ValidationError(
            _("Warehouse %(code)s is system-controlled and takes no manual adjustment."),
            code="adjustment_into_system_warehouse",
            params={"code": warehouse.code},
        )
    if not evidence_reference.strip():
        raise ValidationError(
            _("An adjustment needs its evidence reference."),
            code="evidence_reference_required",
        )
    if not reason.strip():
        raise ValidationError(_("An adjustment needs a reason."), code="adjustment_reason_required")
    if timezone.is_naive(effective_at):
        raise ValidationError(
            _("The effective moment must state its timezone."),
            code="effective_at_must_be_aware",
        )
    if cost_center is not None and cost_center.organization_id != organization.pk:
        raise ValidationError(
            _("Cost center %(code)s belongs to another organization."),
            code="cost_center_organization_mismatch",
            params={"code": cost_center.code},
        )

    day = resolve_business_day(branch, effective_at)
    document = InventoryAdjustmentDocument(
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        effective_at=effective_at,
        business_date=day.business_date,
        evidence_reference=evidence_reference.strip(),
        reason=reason.strip(),
        cost_center=cost_center,
        created_by=get_actor(),
    )
    document.full_clean()
    document.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=document,
        branch=branch,
        new_state=snapshot(document),
        source_document_type=MANUAL_ADJUSTMENT_SOURCE_TYPE,
        source_document_id=str(document.public_id),
    )
    return document


@transaction.atomic
def update_adjustment(
    *,
    document: InventoryAdjustmentDocument,
    effective_at: datetime.datetime | None = None,
    evidence_reference: str | None = None,
    reason: str | None = None,
    cost_center: CostCenter | None = None,
) -> InventoryAdjustmentDocument:
    """Amend a draft."""
    locked = InventoryAdjustmentDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    before = snapshot(locked)

    if effective_at is not None:
        if timezone.is_naive(effective_at):
            raise ValidationError(
                _("The effective moment must state its timezone."),
                code="effective_at_must_be_aware",
            )
        locked.effective_at = effective_at
        locked.business_date = resolve_business_day(locked.branch, effective_at).business_date
    if evidence_reference is not None:
        locked.evidence_reference = evidence_reference.strip()
    if reason is not None:
        locked.reason = reason.strip()
    locked.cost_center = cost_center
    locked.full_clean()
    locked.save(
        update_fields=[
            "effective_at",
            "business_date",
            "evidence_reference",
            "reason",
            "cost_center",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        previous_state=before,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def delete_adjustment(*, document: InventoryAdjustmentDocument, reason: str = "") -> None:
    """Discard a draft. A posted adjustment is reversed, never removed."""
    locked = InventoryAdjustmentDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.branch,
        previous_state=snapshot(locked),
        reason=reason or "adjustment draft discarded",
    )
    locked.lines.all().delete()
    locked.delete()


@transaction.atomic
def add_adjustment_line(
    *, document: InventoryAdjustmentDocument, line: AdjustmentLineInput
) -> InventoryAdjustmentLine:
    """Add one correction to a draft."""
    locked = InventoryAdjustmentDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")

    if line.kind not in AdjustmentLineKind.values:
        raise ValidationError(
            _("%(kind)s is not a known adjustment kind."),
            code="unknown_adjustment_kind",
            params={"kind": line.kind},
        )
    _validate_target(locked, line)
    _validate_reason_code(locked, line)

    base_quantity = ZERO
    unit_cost: Decimal | None = None
    value_adjustment: Decimal | None = None

    if line.kind == AdjustmentLineKind.VALUE_ONLY:
        base_quantity, unit_cost, value_adjustment = _validate_value_only(locked, line)
    else:
        base_quantity = _derive_base_quantity(locked, line)
        if base_quantity <= ZERO:
            raise ValidationError(
                _("The quantity must be greater than zero."), code="quantity_not_positive"
            )
        if line.value_adjustment is not None:
            raise ValidationError(
                _("A quantity adjustment takes no signed value; that is a revaluation."),
                code="value_adjustment_not_accepted",
            )
        if line.kind == AdjustmentLineKind.QUANTITY_GAIN:
            unit_cost = _validate_gain_cost(line)
        elif line.unit_cost is not None:
            raise ValidationError(
                _("A quantity loss is valued at the standing average and takes no cost."),
                code="unit_cost_not_accepted",
            )

    if locked.lines.filter(item=line.item, lot=line.lot).exists():
        raise ValidationError(
            _("This item and lot already have a line in this adjustment."),
            code="duplicate_valuation_key",
        )

    last = locked.lines.order_by("-sequence").first()
    stored = InventoryAdjustmentLine(
        document=locked,
        sequence=(last.sequence + 1) if last is not None else 1,
        kind=line.kind,
        item=line.item,
        lot=line.lot,
        package_conversion=line.package_conversion,
        entered_package_quantity=line.entered_package_quantity,
        measured_base_quantity=line.measured_base_quantity,
        base_quantity=base_quantity,
        unit_cost=unit_cost,
        zero_cost_confirmed=(
            line.zero_cost_confirmed and line.kind == AdjustmentLineKind.QUANTITY_GAIN
        ),
        value_adjustment=value_adjustment,
        reason_code=line.reason_code,
        line_comment=line.line_comment.strip(),
    )
    stored.full_clean()
    stored.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(stored),
        reason="adjustment line added",
        metadata={"line": stored.sequence, "kind": line.kind, "item": line.item.code},
    )
    return stored


def _validate_gain_cost(line: AdjustmentLineInput) -> Decimal:
    """
    A gain states what the goods cost, and says so on purpose when that is zero.

    Nothing in the books can answer this: the position may hold no stock at
    all, and even where it does, goods that appeared from nowhere did not
    necessarily cost what the rest of the shelf cost. Defaulting to the average
    would be a guess presented as a fact, and defaulting to zero would book an
    asset that arrived free.
    """
    if line.unit_cost is None:
        raise ValidationError(
            _("A quantity gain needs an explicit unit cost."), code="unit_cost_required"
        )
    unit_cost = quantize_unit_price(line.unit_cost)
    if unit_cost < ZERO:
        raise ValidationError(_("A unit cost cannot be negative."), code="unit_cost_negative")
    if unit_cost == ZERO and not line.zero_cost_confirmed:
        raise ValidationError(
            _(
                "A zero unit cost must be confirmed explicitly. Stock booked at nothing is a "
                "decision, not a default."
            ),
            code="zero_cost_not_confirmed",
        )
    return unit_cost


def _validate_value_only(
    document: InventoryAdjustmentDocument, line: AdjustmentLineInput
) -> tuple[Decimal, None, Decimal]:
    """
    A revaluation moves no goods, so everything about quantity must be absent.

    The position is checked here **and** in the kernel. Here so the operator
    finds out while editing a draft, and there because between drafting and
    posting the stock may have gone — and the kernel holds the position lock
    that makes the answer true.
    """
    if (
        line.base_quantity is not None
        or line.package_conversion is not None
        or line.entered_package_quantity is not None
        or line.measured_base_quantity is not None
    ):
        raise ValidationError(
            _("A value-only revaluation moves no goods and takes no quantity."),
            code="value_only_carries_quantity",
        )
    if line.unit_cost is not None:
        raise ValidationError(
            _("A value-only revaluation states a total, not a unit cost."),
            code="unit_cost_not_accepted",
        )
    if line.value_adjustment is None or line.value_adjustment == ZERO:
        raise ValidationError(
            _("A value-only revaluation needs a non-zero signed amount."),
            code="value_adjustment_required",
        )
    adjustment = quantize_money(line.value_adjustment)
    if adjustment == ZERO:
        raise ValidationError(
            _("That amount rounds to zero, which corrects nothing."),
            code="value_adjustment_required",
        )

    balance = StockBalance.objects.filter(
        warehouse_id=document.warehouse_id, item=line.item, lot=line.lot
    ).first()
    quantity = balance.quantity if balance is not None else ZERO
    if quantity <= ZERO:
        raise ValidationError(
            _(
                "%(item)s holds no stock in %(warehouse)s, so there is nothing to revalue. "
                "Stock that is genuinely there is a quantity gain with a cost."
            ),
            code="value_only_needs_quantity",
            params={"item": line.item.code, "warehouse": document.warehouse.code},
        )
    value = balance.value if balance is not None else ZERO
    if value + adjustment < ZERO:
        raise ValidationError(
            _(
                "%(item)s is carried at %(value)s; writing it down by %(amount)s would leave a "
                "negative inventory value."
            ),
            code="value_only_would_go_negative",
            params={
                "item": line.item.code,
                "value": str(value),
                "amount": str(abs(adjustment)),
            },
        )
    return ZERO, None, adjustment


def _validate_target(document: InventoryAdjustmentDocument, line: AdjustmentLineInput) -> None:
    if line.item.organization_id != document.organization_id:
        raise ValidationError(
            _("Item %(code)s belongs to another organization."),
            code="item_organization_mismatch",
            params={"code": line.item.code},
        )
    if not line.item.is_active:
        raise ValidationError(
            _("Item %(code)s is archived."),
            code="item_inactive",
            params={"code": line.item.code},
        )
    if line.item.tracks_lots:
        if line.lot is None:
            raise ValidationError(
                _("Item %(code)s tracks lots, so the line needs one."),
                code="lot_required",
                params={"code": line.item.code},
            )
        if line.lot.item_id != line.item.pk:
            raise ValidationError(
                _("Lot %(lot)s belongs to another item."),
                code="lot_item_mismatch",
                params={"lot": line.lot.code},
            )
        if line.item.tracks_expiry and line.lot.expiry_date is None:
            raise ValidationError(
                _("Item %(code)s tracks expiry, so its lots need an expiry date."),
                code="lot_expiry_required",
                params={"code": line.item.code},
            )
    elif line.lot is not None:
        raise ValidationError(
            _("Item %(code)s does not track lots, so the line must not name one."),
            code="lot_not_allowed",
            params={"code": line.item.code},
        )


def _validate_reason_code(document: InventoryAdjustmentDocument, line: AdjustmentLineInput) -> None:
    reason_code = line.reason_code
    if reason_code.organization_id != document.organization_id:
        raise ValidationError(
            _("Reason code %(code)s belongs to another organization."),
            code="reason_code_organization_mismatch",
            params={"code": reason_code.code},
        )
    if reason_code.applies_to != ReasonCodeApplication.MANUAL_ADJUSTMENT:
        raise ValidationError(
            _("Reason code %(code)s is not a manual-adjustment reason."),
            code="reason_code_wrong_application",
            params={"code": reason_code.code},
        )
    if not reason_code.is_active:
        raise ValidationError(
            _("Reason code %(code)s is archived."),
            code="reason_code_archived",
            params={"code": reason_code.code},
        )
    if reason_code.requires_comment and not line.line_comment.strip():
        raise ValidationError(
            _("Reason code %(code)s requires a comment on the line."),
            code="reason_code_comment_required",
            params={"code": reason_code.code},
        )
    if reason_code.requires_evidence and not document.evidence_reference.strip():
        raise ValidationError(  # pragma: no cover - the constraint demands one anyway
            _("Reason code %(code)s requires an evidence reference."),
            code="reason_code_evidence_required",
            params={"code": reason_code.code},
        )


def _derive_base_quantity(
    document: InventoryAdjustmentDocument, line: AdjustmentLineInput
) -> Decimal:
    """Direct base entry, a FIXED package count, or a weighed VARIABLE one."""
    conversion = line.package_conversion
    if conversion is None:
        if line.entered_package_quantity is not None or line.measured_base_quantity is not None:
            raise ValidationError(
                _("A package quantity needs the package conversion it was counted in."),
                code="package_conversion_required",
            )
        if line.base_quantity is None:
            raise ValidationError(_("The line needs a quantity."), code="quantity_required")
        return quantize_quantity(line.base_quantity)

    if conversion.item_id != line.item.pk:
        raise ValidationError(
            _("Conversion %(id)s belongs to another item."),
            code="conversion_item_mismatch",
            params={"id": conversion.pk},
        )
    if not conversion.is_active or not conversion.covers(document.business_date):
        raise ValidationError(
            _("The conversion is not effective on %(date)s."),
            code="conversion_not_effective",
            params={"date": document.business_date.isoformat()},
        )
    if line.entered_package_quantity is None:
        raise ValidationError(
            _("A package conversion needs the number of packages."),
            code="package_quantity_required",
        )
    if conversion.conversion_type == ConversionType.VARIABLE:
        if line.measured_base_quantity is None:
            raise ValidationError(
                _("A variable-weight package must be weighed; its factor is nominal."),
                code="measured_quantity_required",
            )
        return quantize_quantity(line.measured_base_quantity)
    if line.measured_base_quantity is not None:
        raise ValidationError(
            _("A fixed conversion computes its own base quantity and takes no measurement."),
            code="measured_quantity_not_accepted",
        )
    return quantize_quantity(line.entered_package_quantity * conversion.factor_to_base)


@transaction.atomic
def delete_adjustment_line(*, line: InventoryAdjustmentLine, reason: str = "") -> None:
    """Remove a line from a draft."""
    document = InventoryAdjustmentDocument.objects.select_for_update().get(pk=line.document_id)
    _require_status(document, InventoryDocumentStatus.DRAFT, "not_a_draft")
    record_audit_event(
        action=AuditAction.UPDATED,
        target=document,
        branch=document.branch,
        previous_state=snapshot(line),
        reason=reason or "adjustment line removed",
        metadata={"line": line.sequence, "item": line.item.code},
    )
    line.delete()


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@transaction.atomic
def post_adjustment(*, document: InventoryAdjustmentDocument) -> InventoryAdjustmentDocument:
    """Post every line's correction and the one journal that explains them."""
    # 1. The document row.
    locked = InventoryAdjustmentDocument.objects.select_for_update().get(pk=document.pk)
    if locked.status == InventoryDocumentStatus.POSTED:
        raise ValidationError(_("This adjustment is already posted."), code="already_posted")
    _require_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    actor = _actor()

    lines = list(
        locked.lines.select_related(
            "item",
            "item__category",
            "item__category__parent",
            "item__category__parent__parent",
            "lot",
            "package_conversion",
            "reason_code",
        ).order_by("sequence")
    )
    if not lines:
        raise ValidationError(_("An empty adjustment cannot be posted."), code="no_lines")

    day = resolve_business_day(locked.branch, locked.effective_at)
    locked.business_date = day.business_date
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    period = _period_for(organization=locked.organization, on_date=day.business_date)

    # 3. The mappings, before a single account is resolved.
    lock_account_mappings_shared(locked.organization_id)
    variance = resolve_inventory_account(
        organization=locked.organization,
        role=INVENTORY_ADJUSTMENT,
        item=None,
        on_date=locked.business_date,
    )
    require_cost_center_where_the_account_demands_one(
        account=variance.account, cost_center=locked.cost_center
    )

    resolved = [_resolve_line(locked, line) for line in lines]
    # 4. Every key this event touches, canonically, before any of it moves.
    effects = [item.effect for item in resolved]
    acquire_stock_key_locks(effects)

    stock_entry = post_stock_entry(
        organization=locked.organization,
        effects=effects,
        idempotency_key=f"inventory-adjustment:{locked.public_id}",
        effective_at=locked.effective_at,
        business_date=locked.business_date,
        source_document_type=MANUAL_ADJUSTMENT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        reference=locked.evidence_reference,
        reason=locked.reason,
    )
    movements = {movement.effect_key: movement for movement in stock_entry.movements.all()}
    for item in resolved:
        movement = movements[f"adjustment-line:{item.line.line_uid}"]
        # The signed figure the kernel actually produced, not the requested
        # one: a loss is valued at the average and may take the entire
        # remaining value at zero, which only the kernel knows.
        item.line.total_value = movement.inventory_value
        item.line.movement = movement
        item.line.control_account = item.control_account

    # 6. The gapless number, now that no domain reason can fail.
    locked.document_number = next_document_number(
        organization=locked.organization,
        document_type=InventoryDocumentType.ADJUSTMENT,
        year=period.fiscal_year.year,
    )
    journal = post_entry(
        organization=locked.organization,
        accounting_date=locked.business_date,
        lines=_adjustment_journal_lines(locked, resolved, variance_account=variance.account),
        idempotency_key=f"inventory-adjustment-journal:{locked.public_id}",
        document_date=locked.business_date,
        narration=locked.reason,
        source_document_type=MANUAL_ADJUSTMENT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=POSTING_RULE_ADJUSTMENT,
    )
    link_journal_entry(entry=stock_entry, journal=journal)

    for item in resolved:
        item.line.save(update_fields=["total_value", "movement", "control_account", "updated_at"])

    locked.stock_entry = stock_entry
    locked.journal_entry = journal
    locked.status = InventoryDocumentStatus.POSTED
    locked.posted_by = actor
    locked.posted_at = timezone.now()
    locked.full_clean()
    locked.save(
        update_fields=[
            "business_date",
            "business_date_timezone",
            "business_day_start",
            "document_number",
            "stock_entry",
            "journal_entry",
            "status",
            "posted_by",
            "posted_at",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.POSTED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(locked),
        source_document_type=MANUAL_ADJUSTMENT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "document_number": locked.document_number,
            "journal_entry": journal.entry_number,
            "line_count": len(lines),
        },
    )
    return locked


def _resolve_line(
    document: InventoryAdjustmentDocument, line: InventoryAdjustmentLine
) -> _Resolved:
    """Turn one stored line into the kernel effect and account it needs."""
    standing = _standing_control_account(document, line)
    if line.kind == AdjustmentLineKind.QUANTITY_GAIN and standing is None:
        # A new positive position takes the mapping effective on the
        # document's business date; there is nothing standing to strand.
        standing = resolve_inventory_account(
            organization=document.organization,
            role=INVENTORY_CONTROL,
            item=line.item,
            on_date=document.business_date,
        ).account
    if standing is None:
        raise ValidationError(
            _("%(item)s holds no stock in %(warehouse)s to adjust."),
            code="no_position_to_adjust",
            params={"item": line.item.code, "warehouse": document.warehouse.code},
        )

    effect_key = f"adjustment-line:{line.line_uid}"
    if line.kind == AdjustmentLineKind.VALUE_ONLY:
        effect = MovementInput(
            warehouse=document.warehouse,
            item=line.item,
            movement_type=MovementType.MANUAL_ADJUSTMENT,
            quantity=ZERO,
            effect_key=effect_key,
            lot=line.lot,
            control_account=standing,
            direction=EffectDirection.VALUE_ONLY,
            value_adjustment=line.value_adjustment,
        )
    elif line.kind == AdjustmentLineKind.QUANTITY_GAIN:
        effect = MovementInput(
            warehouse=document.warehouse,
            item=line.item,
            movement_type=MovementType.MANUAL_ADJUSTMENT,
            quantity=line.base_quantity,
            effect_key=effect_key,
            lot=line.lot,
            unit_cost=line.unit_cost,
            source_conversion=line.package_conversion,
            control_account=standing,
            direction=EffectDirection.IN,
        )
    else:
        effect = MovementInput(
            warehouse=document.warehouse,
            item=line.item,
            movement_type=MovementType.MANUAL_ADJUSTMENT,
            quantity=line.base_quantity,
            effect_key=effect_key,
            lot=line.lot,
            source_conversion=line.package_conversion,
            control_account=standing,
            direction=EffectDirection.OUT,
        )
    return _Resolved(line=line, effect=effect, control_account=standing)


def _standing_control_account(
    document: InventoryAdjustmentDocument, line: InventoryAdjustmentLine
) -> Account | None:
    """The account this position's stock currently sits in, read never resolved."""
    balance = (
        StockBalance.objects.filter(
            warehouse_id=document.warehouse_id, item=line.item, lot=line.lot
        )
        .select_related("control_account")
        .first()
    )
    return balance.control_account if balance is not None else None


def _adjustment_journal_lines(
    document: InventoryAdjustmentDocument,
    resolved: list[_Resolved],
    *,
    variance_account: Account,
) -> list[PostingLine]:
    """
    Group the stored signed values into balanced journal lines.

    The direction is read from each **movement's own signed value**, not from
    the line kind. A `VALUE_ONLY` line can go either way, and a `QUANTITY_LOSS`
    that emptied a position took the entire remaining value rather than
    quantity times average — so the movement is the only thing that knows what
    actually moved.

    Grouped by account and direction and never netted, for the same reason a
    count is not: a document that wrote one item up and another down is two
    facts, and one net line would report neither.
    """
    control_debits: dict[int, Decimal] = {}
    control_credits: dict[int, Decimal] = {}
    accounts: dict[int, Account] = {}
    variance_debit = ZERO
    variance_credit = ZERO

    for item in resolved:
        value = item.line.total_value or ZERO
        account = item.control_account
        accounts[account.pk] = account
        if value > ZERO:
            control_debits[account.pk] = control_debits.get(account.pk, ZERO) + value
            variance_credit += value
        elif value < ZERO:
            amount = abs(value)
            control_credits[account.pk] = control_credits.get(account.pk, ZERO) + amount
            variance_debit += amount

    lines: list[PostingLine] = []
    for account_id, amount in sorted(
        control_debits.items(), key=lambda pair: accounts[pair[0]].code
    ):
        lines.append(
            PostingLine(
                account=accounts[account_id], branch=document.branch, debit=amount, credit=ZERO
            )
        )
    for account_id, amount in sorted(
        control_credits.items(), key=lambda pair: accounts[pair[0]].code
    ):
        lines.append(
            PostingLine(
                account=accounts[account_id], branch=document.branch, debit=ZERO, credit=amount
            )
        )
    if variance_debit > ZERO:
        lines.append(
            PostingLine(
                account=variance_account,
                branch=document.branch,
                cost_center=document.cost_center,
                debit=variance_debit,
                credit=ZERO,
            )
        )
    if variance_credit > ZERO:
        lines.append(
            PostingLine(
                account=variance_account,
                branch=document.branch,
                cost_center=document.cost_center,
                debit=ZERO,
                credit=variance_credit,
            )
        )
    if not lines:
        raise ValidationError(
            _("This adjustment moves no value; there is nothing to post."),
            code="adjustment_has_no_effect",
        )
    return lines


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@transaction.atomic
def reverse_adjustment(
    *, document: InventoryAdjustmentDocument, reason: str
) -> InventoryAdjustmentDocument:
    """
    Mirror a posted adjustment exactly, dated today.

    Availability applies to a reversal as to anything else: reversing a gain
    whose goods have since been consumed is refused by the kernel, because
    exempting it would make "reverse the adjustment" the standard way to drive
    a balance negative.
    """
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")

    locked = InventoryAdjustmentDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, InventoryDocumentStatus.POSTED, "not_posted")
    actor = _actor()
    assert locked.stock_entry is not None  # noqa: S101 - POSTED implies both
    assert locked.journal_entry is not None  # noqa: S101

    reversal_day = resolve_business_day(locked.branch, timezone.now())
    _period_for(organization=locked.organization, on_date=reversal_day.business_date)

    reversal_entry = reverse_stock_entry(
        entry=locked.stock_entry,
        idempotency_key=f"inventory-adjustment-reversal:{locked.public_id}",
        reason=reason.strip(),
        business_date=reversal_day.business_date,
    )
    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=f"inventory-adjustment-journal-reverse:{locked.public_id}",
        reason=reason.strip(),
        accounting_date=reversal_day.business_date,
    )
    link_journal_entry(entry=reversal_entry, journal=reversal_journal)

    locked.status = InventoryDocumentStatus.REVERSED
    locked.reversed_by = actor
    locked.reversed_at = timezone.now()
    locked.reversal_reason = reason.strip()
    locked.reversal_journal_entry = reversal_journal
    locked.full_clean()
    locked.save(
        update_fields=[
            "status",
            "reversed_by",
            "reversed_at",
            "reversal_reason",
            "reversal_journal_entry",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.REVERSED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(locked),
        reason=reason,
        source_document_type=MANUAL_ADJUSTMENT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={"reversal_journal": reversal_journal.entry_number},
    )
    return locked


__all__ = [
    "AdjustmentLineInput",
    "add_adjustment_line",
    "create_adjustment",
    "delete_adjustment",
    "delete_adjustment_line",
    "post_adjustment",
    "reverse_adjustment",
    "update_adjustment",
]
