"""
The opening-stock document: lifecycle, atomic posting, and reversal.

This module knows the domain and nothing about users. Who may prepare, submit,
post, or reverse is answered in `apps/inventory/commands.py`, exactly as the
ledger kernel divides from its command layer.

## The one combined posting

`post_opening_document` is the first code path that writes both ledgers, and
its transaction acquires resources in ONE documented order:

    1. the opening document row                  (select_for_update)
    2. the stock keys, sorted canonically        (advisory locks, taken here
                                                  so the prior-history check
                                                  is race-free; re-acquired
                                                  as a no-op by the kernel)
    3. the organization's posted-order counter   (inside post_stock_entry)
    4. the opening document-number sequence
    5. the journal-number sequence               (inside post_entry)

Steps 3 and 4 are swapped relative to the suggested order in the Task 1.3
brief, deliberately: the stock kernel owns "keys then counter" as one unit,
and splitting that unit to interleave a document number would put half the
kernel's locking discipline in a second module. The order above is globally
consistent — every combined service must use it, no official code path posts
the journal first, and the concurrency tests post two openings in parallel
against it.

## Maker-checker

`submitted_by != posted_by`, enforced on the acts and backed by a database
constraint. Holding both permissions changes nothing: the user who declared
"these are the figures" cannot also be the user who declares "and they are
approved".
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    INVENTORY_CONTROL,
    INVENTORY_OPENING_EQUITY,
    Account,
    JournalLine,
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
from apps.inventory.accounts import ResolvedAccount, resolve_inventory_account
from apps.inventory.ledger import (
    MovementInput,
    acquire_stock_key_locks,
    post_stock_entry,
    reverse_stock_entry,
)
from apps.inventory.models import (
    ConversionType,
    InventoryDocumentSequence,
    InventoryDocumentType,
    InventoryItem,
    InventoryLot,
    ItemPackageConversion,
    MovementType,
    OpeningStockDocument,
    OpeningStockLine,
    OpeningStockStatus,
    StockMovement,
    Warehouse,
)
from apps.organizations.business_dates import (
    business_date_for,
    business_date_from_snapshot,
    resolve_business_day,
)
from apps.organizations.models import Branch, Organization

#: The rule identifier stamped on every opening journal. Bump it when the
#: accounting treatment changes, so old entries say which rule produced them.
OPENING_POSTING_RULE = "inventory-opening-v1"

SOURCE_DOCUMENT_TYPE = "INVENTORY_OPENING"

ZERO = Decimal("0")


def _require_status(document: OpeningStockDocument, status: str, code: str) -> None:
    if document.status != status:
        raise ValidationError(
            _("Document %(doc)s is %(actual)s, not %(expected)s."),
            code=code,
            params={
                "doc": document.document_number or str(document.public_id),
                "actual": document.get_status_display(),
                "expected": status,
            },
        )


def _aware_cutoff(cutoff_at: datetime.datetime) -> datetime.datetime:
    if timezone.is_naive(cutoff_at):
        raise ValidationError(_("The cutoff must state its timezone."), code="cutoff_must_be_aware")
    return cutoff_at


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


@transaction.atomic
def create_opening_document(
    *,
    organization: Organization,
    branch: Branch,
    cutoff_at: datetime.datetime,
    evidence_reference: str,
    narration: str = "",
) -> OpeningStockDocument:
    """Start a draft opening for one branch, dated by one explicit cutoff."""
    if branch.organization_id != organization.pk:
        raise ValidationError(
            _("Branch %(code)s belongs to another organization."),
            code="branch_organization_mismatch",
            params={"code": branch.code},
        )
    if not branch.is_active:
        raise ValidationError(
            _("Branch %(code)s is closed."), code="branch_inactive", params={"code": branch.code}
        )
    if not evidence_reference.strip():
        raise ValidationError(
            _("An opening needs its evidence reference — the signed count sheet."),
            code="evidence_reference_required",
        )
    cutoff_at = _aware_cutoff(cutoff_at)

    document = OpeningStockDocument(
        organization=organization,
        branch=branch,
        cutoff_at=cutoff_at,
        business_date=business_date_for(branch, cutoff_at),
        evidence_reference=evidence_reference.strip(),
        narration=narration.strip(),
        created_by=get_actor(),
    )
    document.full_clean()
    document.save()
    record_audit_event(
        action=AuditAction.CREATED, target=document, branch=branch, new_state=snapshot(document)
    )
    return document


@transaction.atomic
def update_opening_document(
    *,
    document: OpeningStockDocument,
    cutoff_at: datetime.datetime | None = None,
    evidence_reference: str | None = None,
    narration: str | None = None,
) -> OpeningStockDocument:
    """Amend a draft's header. Anything past DRAFT is locked against editing."""
    locked = OpeningStockDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, OpeningStockStatus.DRAFT, "not_a_draft")
    before = snapshot(locked)

    if cutoff_at is not None:
        locked.cutoff_at = _aware_cutoff(cutoff_at)
        locked.business_date = business_date_for(locked.branch, locked.cutoff_at)
    if evidence_reference is not None:
        if not evidence_reference.strip():
            raise ValidationError(
                _("An opening needs its evidence reference — the signed count sheet."),
                code="evidence_reference_required",
            )
        locked.evidence_reference = evidence_reference.strip()
    if narration is not None:
        locked.narration = narration.strip()

    locked.full_clean()
    locked.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        previous_state=before,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def delete_opening_document(*, document: OpeningStockDocument, reason: str = "") -> None:
    """
    Delete a draft. Only a draft — anything later is history, and the
    database trigger refuses it even if this check were bypassed.
    """
    locked = OpeningStockDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, OpeningStockStatus.DRAFT, "not_a_draft")
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.branch,
        previous_state=snapshot(locked),
        reason=reason,
    )
    locked.lines.all().delete()
    locked.delete()


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpeningLineInput:
    """One requested line, before validation derives its base quantity."""

    warehouse: Warehouse
    item: InventoryItem
    unit_cost: Decimal
    lot: InventoryLot | None = None
    package_conversion: ItemPackageConversion | None = None
    entered_package_quantity: Decimal | None = None
    measured_base_quantity: Decimal | None = None
    base_quantity: Decimal | None = None


def _validate_line_target(document: OpeningStockDocument, line: OpeningLineInput) -> None:
    if line.warehouse.branch_id != document.branch_id:
        raise ValidationError(
            _("Warehouse %(code)s belongs to another branch."),
            code="warehouse_branch_mismatch",
            params={"code": line.warehouse.code},
        )
    if not line.warehouse.is_active:
        raise ValidationError(
            _("Warehouse %(code)s is archived."),
            code="warehouse_inactive",
            params={"code": line.warehouse.code},
        )
    if line.warehouse.is_system:
        # Nothing is in transit before the ledger starts; an opening balance
        # inside the system warehouse would be a claim with no dispatch.
        raise ValidationError(
            _("Warehouse %(code)s is system-controlled and takes no opening balance."),
            code="opening_into_system_warehouse",
            params={"code": line.warehouse.code},
        )
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
                _("Item %(code)s tracks lots, so the opening line needs one."),
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


def _derive_base_quantity(document: OpeningStockDocument, line: OpeningLineInput) -> Decimal:
    """
    The authoritative counted quantity, from whichever way it was entered.

    Direct base entry, a FIXED package count, or a VARIABLE package count with
    a measured weight — one of the three, never a mixture. The conversion used
    is snapshotted on the stored line, so a later factor version cannot
    restate what this count meant.
    """
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
            _("The conversion belongs to another item."),
            code="conversion_item_mismatch",
        )
    if not conversion.is_active:
        raise ValidationError(
            _("Conversion version %(version)s is no longer active."),
            code="conversion_inactive",
            params={"version": conversion.version},
        )
    covers = conversion.effective_from <= document.business_date and (
        conversion.effective_to is None or document.business_date <= conversion.effective_to
    )
    if not covers:
        raise ValidationError(
            _("The conversion is not effective on %(date)s."),
            code="conversion_not_effective",
            params={"date": document.business_date.isoformat()},
        )
    if line.base_quantity is not None:
        raise ValidationError(
            _("Enter either a package count or a base quantity, not both."),
            code="quantity_entered_twice",
        )
    if line.entered_package_quantity is None or line.entered_package_quantity <= ZERO:
        raise ValidationError(
            _("The package count must be greater than zero."),
            code="package_quantity_required",
        )
    if not conversion.allows_fractional and line.entered_package_quantity % 1 != 0:
        raise ValidationError(
            _("This package does not come in fractions."),
            code="fractional_packages_not_allowed",
        )

    if conversion.conversion_type == ConversionType.VARIABLE:
        # The stored factor is a planning estimate; the scale is the truth.
        if line.measured_base_quantity is None or line.measured_base_quantity <= ZERO:
            raise ValidationError(
                _("A variable package needs the measured base quantity."),
                code="measured_quantity_required",
            )
        return quantize_quantity(line.measured_base_quantity)

    if line.measured_base_quantity is not None:
        raise ValidationError(
            _("A fixed package converts arithmetically; a measured quantity means VARIABLE."),
            code="measured_only_for_variable",
        )
    return quantize_quantity(line.entered_package_quantity * conversion.factor_to_base)


@transaction.atomic
def ensure_opening_lot(
    *, item: InventoryItem, code: str, expiry_date: datetime.date | None = None
) -> InventoryLot:
    """
    The lot an opening line names — fetched, or created if it is new.

    Creation is legitimate here and almost nowhere else: opening stock records
    batches that predate the ledger, so their lots cannot already exist. An
    existing lot is never silently re-dated — a contradicting expiry is a data
    conflict to resolve, not to overwrite.
    """
    code = code.strip()
    if not code:
        raise ValidationError(_("A lot needs a code."), code="lot_code_required")
    lot, created = InventoryLot.objects.get_or_create(
        item=item,
        code=code,
        defaults={"organization_id": item.organization_id, "expiry_date": expiry_date},
    )
    if created:
        record_audit_event(action=AuditAction.CREATED, target=lot, new_state=snapshot(lot))
    elif expiry_date is not None and lot.expiry_date != expiry_date:
        raise ValidationError(
            _("Lot %(code)s already exists with expiry %(existing)s."),
            code="lot_expiry_conflict",
            params={"code": code, "existing": lot.expiry_date or _("بلا تاريخ")},
        )
    return lot


@transaction.atomic
def add_opening_line(*, document: OpeningStockDocument, line: OpeningLineInput) -> OpeningStockLine:
    """Add one counted position to a draft."""
    locked = OpeningStockDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, OpeningStockStatus.DRAFT, "not_a_draft")
    _validate_line_target(locked, line)

    base_quantity = _derive_base_quantity(locked, line)
    if base_quantity <= ZERO:
        raise ValidationError(
            _("The quantity must be greater than zero."), code="quantity_not_positive"
        )
    unit_cost = quantize_unit_price(line.unit_cost)
    if unit_cost <= ZERO:
        raise ValidationError(
            _("The unit cost must be greater than zero."), code="unit_cost_not_positive"
        )
    total_value = quantize_money(base_quantity * unit_cost)
    if total_value <= ZERO:
        # Positive quantity at zero value would put free stock on the books.
        raise ValidationError(
            _("The line value rounds to zero; a positive quantity needs a real value."),
            code="line_value_not_positive",
        )

    duplicate = locked.lines.filter(warehouse=line.warehouse, item=line.item, lot=line.lot).exists()
    if duplicate:
        raise ValidationError(
            _("This warehouse, item, and lot already have a line in this document."),
            code="duplicate_valuation_key",
        )

    last = locked.lines.order_by("-sequence").first()
    stored = OpeningStockLine(
        document=locked,
        sequence=(last.sequence + 1) if last is not None else 1,
        warehouse=line.warehouse,
        item=line.item,
        lot=line.lot,
        package_conversion=line.package_conversion,
        entered_package_quantity=line.entered_package_quantity,
        measured_base_quantity=line.measured_base_quantity,
        base_quantity=base_quantity,
        unit_cost=unit_cost,
        total_value=total_value,
    )
    stored.full_clean()
    stored.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(stored),
        metadata={"line": stored.sequence, "item": line.item.code},
        reason="line added",
    )
    return stored


@transaction.atomic
def delete_opening_line(*, line: OpeningStockLine, reason: str = "") -> None:
    """Remove a line from a draft."""
    document = OpeningStockDocument.objects.select_for_update().get(pk=line.document_id)
    _require_status(document, OpeningStockStatus.DRAFT, "not_a_draft")
    record_audit_event(
        action=AuditAction.UPDATED,
        target=document,
        branch=document.branch,
        previous_state=snapshot(line),
        reason=reason or "line removed",
        metadata={"line": line.sequence, "item": line.item.code},
    )
    line.delete()


@transaction.atomic
def replace_opening_lines(
    *, document: OpeningStockDocument, lines: Sequence[OpeningLineInput]
) -> OpeningStockDocument:
    """
    Replace a draft's lines wholesale — the API PATCH shape.

    Wholesale like `update_draft` replaces journal lines: partial patching
    needs a stable line identity across edits, and the effect key takes its
    identity from the stored line's uid, which a replace regenerates —
    harmless, because effect keys matter only from the moment of posting,
    when the line set is already frozen.
    """
    locked = OpeningStockDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, OpeningStockStatus.DRAFT, "not_a_draft")
    locked.lines.all().delete()
    for line in lines:
        add_opening_line(document=locked, line=line)
    return locked


# ---------------------------------------------------------------------------
# Submit and return
# ---------------------------------------------------------------------------


@transaction.atomic
def submit_opening_document(*, document: OpeningStockDocument) -> OpeningStockDocument:
    """
    Lock the draft for approval. The submitter is recorded and thereby
    excluded from posting it.
    """
    locked = OpeningStockDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, OpeningStockStatus.DRAFT, "not_a_draft")
    actor = get_actor()
    if actor is None:
        raise ValidationError(
            _("Submitting needs a signed-in actor to record."), code="actor_required"
        )
    if not locked.lines.exists():
        raise ValidationError(_("An empty opening cannot be submitted."), code="no_lines")

    before = snapshot(locked)
    # The business date becomes authoritative here, with the branch settings
    # that produced it. Posting replays this snapshot instead of re-deriving,
    # so changing the branch's cutoff afterwards cannot move an approved
    # document into a different accounting period (§B).
    day = resolve_business_day(locked.branch, locked.cutoff_at)
    locked.business_date = day.business_date
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    locked.status = OpeningStockStatus.SUBMITTED
    locked.submitted_by = actor
    locked.submitted_at = timezone.now()
    locked.save(
        update_fields=[
            "business_date",
            "business_date_timezone",
            "business_day_start",
            "status",
            "submitted_by",
            "submitted_at",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        branch=locked.branch,
        previous_state=before,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def return_opening_to_draft(*, document: OpeningStockDocument, reason: str) -> OpeningStockDocument:
    """
    Send a submitted document back for correction, with a stated reason.

    The one legitimate way to change a submitted document: it goes back to
    DRAFT visibly, is edited there, and is submitted again — a fresh
    submission by whoever now stands behind the figures.
    """
    locked = OpeningStockDocument.objects.select_for_update().get(pk=document.pk)
    _require_status(locked, OpeningStockStatus.SUBMITTED, "not_submitted")
    if not reason.strip():
        raise ValidationError(
            _("Returning a submitted document needs a reason."), code="reason_required"
        )
    before = snapshot(locked)
    locked.status = OpeningStockStatus.DRAFT
    locked.submitted_by = None
    locked.submitted_at = None
    # The snapshot is released with the submission that made it. A document
    # back in draft may have its cutoff corrected, and resubmission derives a
    # fresh business date from the branch as it stands then (§B).
    locked.business_date_timezone = ""
    locked.business_day_start = None
    locked.save(
        update_fields=[
            "status",
            "submitted_by",
            "submitted_at",
            "business_date_timezone",
            "business_day_start",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.REJECTED,
        target=locked,
        branch=locked.branch,
        previous_state=before,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def _effect_of(line: OpeningStockLine) -> MovementInput:
    return MovementInput(
        warehouse=line.warehouse,
        item=line.item,
        movement_type=MovementType.OPENING,
        quantity=line.base_quantity,
        effect_key=f"opening-line:{line.line_uid}",
        lot=line.lot,
        unit_cost=line.unit_cost,
        source_conversion=line.package_conversion,
    )


def _next_document_number(*, organization: Organization, year: int) -> str:
    """The next gapless opening number, under a row lock (step 4 of the order)."""
    sequence, _created = InventoryDocumentSequence.objects.get_or_create(
        organization=organization,
        document_type=InventoryDocumentType.OPENING,
        year=year,
    )
    locked = InventoryDocumentSequence.objects.select_for_update().get(pk=sequence.pk)
    locked.last_number += 1
    locked.save(update_fields=["last_number"])
    return f"OPN-{year}-{locked.last_number:06d}"


def _resolve_line_accounts(
    document: OpeningStockDocument, lines: Sequence[OpeningStockLine]
) -> dict[int, ResolvedAccount]:
    """
    Every line's inventory-control account, resolved before any effect exists.

    One missing mapping fails the whole posting here — before a movement, a
    balance, a number, or a journal line has been written — so there is
    nothing partial to clean up.
    """
    resolutions: dict[int, ResolvedAccount] = {}
    for line in lines:
        resolutions[line.pk] = resolve_inventory_account(
            organization=document.organization,
            role=INVENTORY_CONTROL,
            item=line.item,
            on_date=document.business_date,
        )
    return resolutions


def _refuse_cost_center_accounts(*accounts: Account) -> None:
    """
    §O: a mapped account demanding a cost centre fails the posting; nothing
    invents one. Opening stock has no managerial dimension to claim.
    """
    for account in accounts:
        if account.requires_cost_center:
            raise ValidationError(
                _("Account %(code)s requires a cost center, which an opening cannot supply."),
                code="mapping_requires_cost_center",
                params={"code": account.code},
            )


@transaction.atomic
def post_opening_document(*, document: OpeningStockDocument) -> OpeningStockDocument:
    """
    Post a submitted opening to both ledgers, atomically.

    One transaction produces the OPENING movements, the balances, the
    valuation layers, the gapless document number, the balanced journal, the
    line-level links between all of them, and the audit event — or none of it.
    See the module docstring for the lock order.
    """
    # 1. The document row.
    locked = OpeningStockDocument.objects.select_for_update().get(pk=document.pk)
    if locked.status == OpeningStockStatus.POSTED:
        raise ValidationError(_("This opening is already posted."), code="already_posted")
    _require_status(locked, OpeningStockStatus.SUBMITTED, "not_submitted")

    actor = get_actor()
    if actor is None:
        raise ValidationError(
            _("Posting needs a signed-in actor to record."), code="actor_required"
        )
    if locked.submitted_by_id == actor.pk:
        raise ValidationError(
            _("The user who submitted an opening cannot also post it."),
            code="submitter_cannot_post",
        )

    lines = list(
        locked.lines.select_related(
            "warehouse",
            "warehouse__branch",
            "item",
            "item__category",
            "item__category__parent",
            "item__category__parent__parent",
            "lot",
            "package_conversion",
        ).order_by("sequence")
    )
    if not lines:  # pragma: no cover - submit refuses an empty document
        raise ValidationError(_("An empty opening cannot be posted."), code="no_lines")
    for line in lines:
        _validate_line_target(
            locked,
            OpeningLineInput(
                warehouse=line.warehouse,
                item=line.item,
                unit_cost=line.unit_cost,
                lot=line.lot,
            ),
        )

    # The business date was fixed at submission, with the branch settings that
    # produced it. Replayed here, never re-derived: a cutoff changed between
    # submission and approval must not move an approved document into another
    # period behind the approver's back (§B).
    if not locked.business_date_timezone or locked.business_day_start is None:
        raise ValidationError(  # pragma: no cover - the DB constraint refuses this state
            _("This document has no business-date snapshot. Return it to draft and resubmit."),
            code="missing_business_date_snapshot",
        )
    locked.business_date = business_date_from_snapshot(
        locked.cutoff_at,
        timezone_name=locked.business_date_timezone,
        day_start=locked.business_day_start,
    )
    period = resolve_period(organization=locked.organization, accounting_date=locked.business_date)
    validate_period_accepts_postings(period)

    effects = [_effect_of(line) for line in lines]

    # 2. The organization's account mappings, in shared mode — above the stock
    # keys in the global order, so a mapping mutation can never interleave
    # with the resolution below (ADR-019 §5).
    lock_account_mappings_shared(locked.organization_id)

    # 3. The stock keys — before the history check, so no concurrent posting
    # can slip an OPENING or a receipt in between the check and the post.
    acquire_stock_key_locks(effects)
    for line in lines:
        if StockMovement.objects.filter(
            warehouse=line.warehouse, item=line.item, lot=line.lot
        ).exists():
            raise ValidationError(
                _(
                    "%(item)s in %(warehouse)s already has posted movements; an opening "
                    "balance must be the first movement for its position."
                ),
                code="opening_key_already_has_history",
                params={"item": line.item.code, "warehouse": line.warehouse.code},
            )

    # Every account, resolved before any effect exists.
    resolutions = _resolve_line_accounts(locked, lines)
    equity_mapping = resolve_inventory_account(
        organization=locked.organization,
        role=INVENTORY_OPENING_EQUITY,
        item=None,
        on_date=locked.business_date,
    )
    _refuse_cost_center_accounts(
        *(resolved.account for resolved in resolutions.values()), equity_mapping.account
    )

    # Each effect now names the account its value enters, so the movement
    # records it immutably and a later mapping change cannot reinterpret it.
    effects = [
        replace(effect, control_account=resolutions[line.pk].account)
        for line, effect in zip(lines, effects, strict=True)
    ]

    # 4. The stock entry: movements, balances, valuation layers, and the
    # posted-order counter — the kernel's own discipline, untouched.
    stock_entry = post_stock_entry(
        organization=locked.organization,
        effects=effects,
        idempotency_key=f"inventory-opening:{locked.public_id}",
        effective_at=locked.cutoff_at,
        business_date=locked.business_date,
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        reference=locked.evidence_reference,
        reason=locked.narration or "opening stock",
    )

    # 4. The gapless human number, only now that the posting cannot fail for
    # a domain reason — an abandoned attempt must not burn one.
    locked.document_number = _next_document_number(
        organization=locked.organization, year=period.fiscal_year.year
    )

    # 5. The journal: one debit per distinct control account, grouping the
    # exact stored line values; one balancing credit to opening equity. The
    # totals are sums of stored 3-decimal figures — nothing is re-rounded.
    debit_by_account: dict[int, Decimal] = {}
    account_by_id: dict[int, Account] = {}
    for line in lines:
        account = resolutions[line.pk].account
        account_by_id[account.pk] = account
        debit_by_account[account.pk] = debit_by_account.get(account.pk, ZERO) + line.total_value
    total = sum(debit_by_account.values(), ZERO)

    posting_lines = [
        PostingLine(
            account=account_by_id[account_id],
            branch=locked.branch,
            debit=amount,
        )
        for account_id, amount in sorted(
            debit_by_account.items(), key=lambda pair: account_by_id[pair[0]].code
        )
    ]
    posting_lines.append(
        PostingLine(account=equity_mapping.account, branch=locked.branch, credit=total)
    )

    journal = post_entry(
        organization=locked.organization,
        accounting_date=locked.business_date,
        lines=posting_lines,
        idempotency_key=f"inventory-opening-journal:{locked.public_id}",
        document_date=locked.business_date,
        narration=locked.narration or str(_("رصيد افتتاحي للمخزون")),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=OPENING_POSTING_RULE,
    )

    # Line-level traceability, written while the document is still SUBMITTED
    # (the trigger freezes the lines the moment it turns POSTED).
    movement_by_key = {movement.effect_key: movement for movement in stock_entry.movements.all()}
    journal_line_by_account: dict[int, JournalLine] = {
        journal_line.account_id: journal_line
        for journal_line in journal.lines.filter(debit__gt=ZERO)
    }
    for line in lines:
        resolved = resolutions[line.pk]
        line.resolved_mapping = resolved.inventory_mapping
        line.resolved_organization_mapping = resolved.organization_mapping
        line.inventory_account = resolved.account
        line.movement = movement_by_key[f"opening-line:{line.line_uid}"]
        line.journal_line = journal_line_by_account[resolved.account.pk]
        line.save(
            update_fields=[
                "resolved_mapping",
                "resolved_organization_mapping",
                "inventory_account",
                "movement",
                "journal_line",
                "updated_at",
            ]
        )

    locked.stock_entry = stock_entry
    locked.journal_entry = journal
    locked.status = OpeningStockStatus.POSTED
    locked.posted_by = actor
    locked.posted_at = timezone.now()
    locked.save(
        update_fields=[
            "business_date",
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
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "document_number": locked.document_number,
            "stock_entry": stock_entry.pk,
            "journal_entry": journal.entry_number,
            "line_count": len(lines),
        },
    )
    return locked


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@transaction.atomic
def reverse_opening_document(
    *, document: OpeningStockDocument, reason: str
) -> OpeningStockDocument:
    """
    Reverse the whole document — never a line.

    Both mirrors are exact: each stock movement takes back its original
    quantity and value, and the reversing journal mirrors the original lines,
    whatever the averages or mappings have since become. Availability still
    applies — if the opening quantity has been consumed, the goods are not
    there to take back and the reversal is refused before anything is written.

    Both reversing effects are dated **now** (the current business day), in a
    period that must accept postings. A replacement opening is a new document.
    """
    locked = OpeningStockDocument.objects.select_for_update().get(pk=document.pk)
    if locked.status == OpeningStockStatus.REVERSED:
        raise ValidationError(_("This opening is already reversed."), code="already_reversed")
    _require_status(locked, OpeningStockStatus.POSTED, "not_posted")
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")
    actor = get_actor()
    if actor is None:
        raise ValidationError(
            _("Reversing needs a signed-in actor to record."), code="actor_required"
        )

    now = timezone.now()
    # A reversal is a new event happening *now*, so it takes today's business
    # date from the branch as it stands — not the original's stored snapshot,
    # which described a different day.
    reversal_business_date = business_date_for(locked.branch, now)

    # Stock first, then journal — the same direction as posting. The kernel
    # enforces availability and refuses a second reversal on the unique
    # source identity (INVENTORY_OPENING / uuid / REVERSED).
    assert locked.stock_entry is not None  # noqa: S101 - a POSTED document always links one
    assert locked.journal_entry is not None  # noqa: S101
    reverse_stock_entry(
        entry=locked.stock_entry,
        idempotency_key=f"inventory-opening-reverse:{locked.public_id}",
        reason=reason.strip(),
        effective_at=now,
        business_date=reversal_business_date,
    )
    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=f"inventory-opening-journal-reverse:{locked.public_id}",
        reason=reason.strip(),
        accounting_date=reversal_business_date,
    )

    locked.status = OpeningStockStatus.REVERSED
    locked.reversed_by = actor
    locked.reversed_at = now
    locked.reversal_reason = reason.strip()
    locked.reversal_journal_entry = reversal_journal
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
        reason=reason.strip(),
        source_document_type=SOURCE_DOCUMENT_TYPE,
        source_document_id=str(locked.public_id),
        metadata={"reversal_journal": reversal_journal.entry_number},
    )
    return locked
