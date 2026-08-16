"""
Physical stock counts: freeze, blind entry, maker-checker approval, variance.

A count is the one inventory document where the *procedure* is the control. The
figures it produces are only as trustworthy as the conditions they were
gathered under, so four of those conditions are enforced here rather than
assumed:

**One cutoff.** The book position is photographed at a single moment, and
`business_date` is the operating day that moment belongs to. Everything the
count later posts is dated by that day — not by the day somebody got round to
approving it. A count of the 1st approved on the 3rd is a fact about the 1st.

**A real freeze.** From `start_count` to `approve_count` or `cancel_count` the
warehouse accepts no postings at all, and the freeze is owned through
`Warehouse.frozen_by_count` so that "is it frozen" and "who froze it" cannot
give different answers. The lock that makes this hold against concurrent
postings is `lock_warehouses_exclusive`; reading the column alone would let a
posting already in flight land inside the snapshot.

**Blind entry.** The conductor is never shown the book quantity — not in the
API, not in the HTML, not in a hidden field. A counter who can see the expected
figure tends to find it, and a count that confirms the books is worth nothing.
`blind_lines` is the only selector the counting screen may use.

**Maker-checker.** Whoever counted does not approve. Enforced in the service,
at the API, and by a check constraint, because a rule that lives only in a
hidden button is not a rule.

## Valuation

A **loss** leaves at the standing moving average, with the kernel's
full-depletion rule when it empties the position — the same arithmetic as any
other outbound, because that is what it is.

A **gain** is the interesting case, and it splits:

* into a position that still holds stock, at the **standing average**, so
  finding more of something does not restate what the rest of it cost;
* into an empty or never-valued position, at an **explicitly approved unit
  cost**. There is no average to borrow, and defaulting to zero would book
  free stock — an asset that arrived from nowhere and a variance account that
  never took the credit. The conductor is not asked for this figure; it is a
  cost decision and belongs with the approver.

`zero_cost_confirmed` separates "nobody said what it was worth" from "we
looked, and it is worth nothing". Both would otherwise be a null.

## Locking

    1. the count row                         select_for_update
    2. the warehouse freeze lock             advisory, exclusive
    3. the warehouse row                     select_for_update
    4. the count lines                       select_for_update, by primary key
    5. the organization's mapping lock       shared
    6. every stock key, canonically          advisory, inside the kernel
    7. the posted-order counter              inside the kernel
    8. the document-number sequence
    9. the journal-number sequence           inside post_entry

Steps 2 and 3 are new in Task 1.6 and sit **above** the mapping lock, which is
where nothing else was already standing — no posting path locks a warehouse, so
adding them at the top inverts no existing order (ADR-021 §7).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    INVENTORY_CONTROL,
    INVENTORY_COUNT_VARIANCE,
    Account,
    AccountingPeriod,
    CostCenter,
    PeriodState,
    SourceEvent,
)
from apps.accounting.services import post_entry, resolve_period, reverse_entry
from apps.accounting.validators import PostingLine, validate_period_accepts_postings
from apps.core.context import get_actor
from apps.core.locks import lock_account_mappings_shared, lock_warehouses_exclusive
from apps.core.models import AuditAction
from apps.core.money import quantize_unit_price
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.inventory.accounts import resolve_inventory_account
from apps.inventory.ledger import (
    MovementInput,
    acquire_stock_key_locks,
    link_journal_entry,
    post_stock_entry,
    reverse_stock_entry,
)
from apps.inventory.models import (
    ACTIVE_COUNT_STATUSES,
    STOCK_COUNT_SOURCE_TYPE,
    ConversionType,
    InventoryDocumentType,
    InventoryItem,
    InventoryLot,
    InventoryReasonCode,
    ItemPackageConversion,
    MovementType,
    ReasonCodeApplication,
    StockBalance,
    StockCount,
    StockCountLine,
    StockCountScope,
    StockCountStatus,
    Warehouse,
    WarehouseType,
)
from apps.inventory.operations import (
    next_document_number,
    require_cost_center_where_the_account_demands_one,
)
from apps.organizations.business_dates import resolve_business_day
from apps.organizations.models import Branch, Organization
from apps.users.models import User

ZERO = Decimal("0")

POSTING_RULE_COUNT = "inventory-count-variance-v1"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CountEntry:
    """One counted figure, as the conductor entered it."""

    line: StockCountLine
    base_quantity: Decimal | None = None
    package_conversion: ItemPackageConversion | None = None
    entered_package_quantity: Decimal | None = None
    measured_base_quantity: Decimal | None = None
    note: str = ""
    reason_code: InventoryReasonCode | None = None


@dataclass(frozen=True)
class ApprovedCost:
    """An approver's answer for a gain the books cannot value."""

    line: StockCountLine
    unit_cost: Decimal
    #: Required to accept a zero. Without it, zero is indistinguishable from an
    #: unanswered question.
    zero_confirmed: bool = False


@dataclass
class _Variance:
    """One line's resolved variance, ready to post."""

    line: StockCountLine
    quantity: Decimal
    unit_cost: Decimal | None
    control_account: Account


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _actor() -> User:
    actor = get_actor()
    if actor is None:
        raise ValidationError(
            _("A count action needs a signed-in actor to record."), code="actor_required"
        )
    return actor


def _require_status(count: StockCount, allowed: tuple[str, ...], code: str) -> None:
    if count.status not in allowed:
        raise ValidationError(
            _("Count %(count)s is %(actual)s."),
            code=code,
            params={
                "count": count.count_number or str(count.public_id),
                "actual": count.get_status_display(),
            },
        )


def _period_for(
    *, organization: Organization, on_date: datetime.date, lock: bool = False
) -> AccountingPeriod:
    """
    The period this date falls in, refusing a closed one.

    `lock` takes the period **row** before checking it, and only `start_count`
    asks for it. Without it, a close and a count start can both commit: neither
    can see the other's uncommitted work under READ COMMITTED, so the close's
    guard finds no active count and the count's check finds an open period —
    and the result is a frozen warehouse inside a shut period, which can
    neither post nor be cancelled out of without reopening the period.

    `_change_period_state` locks the same row before running its guards, so the
    two orders meet: whichever takes the row first, the other waits and then
    sees the committed truth.
    """
    period = resolve_period(organization=organization, accounting_date=on_date)
    if lock:
        period = AccountingPeriod.objects.select_for_update().get(pk=period.pk)
    validate_period_accepts_postings(period)
    return period


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


@transaction.atomic
def create_count(
    *,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    reference: str = "",
    reason: str = "",
    cost_center: CostCenter | None = None,
) -> StockCount:
    """
    Prepare a count. Nothing is frozen and nothing is snapshotted yet.

    A draft exists so the scope, the evidence reference and the cost centre can
    be settled without holding a warehouse shut while somebody thinks about it.
    """
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
    _require_warehouse_is_countable(warehouse)
    if cost_center is not None and cost_center.organization_id != organization.pk:
        raise ValidationError(
            _("Cost center %(code)s belongs to another organization."),
            code="cost_center_organization_mismatch",
            params={"code": cost_center.code},
        )

    count = StockCount(
        organization=organization,
        branch=branch,
        warehouse=warehouse,
        scope_type=StockCountScope.FULL_WAREHOUSE,
        reference=reference.strip(),
        reason=reason.strip(),
        cost_center=cost_center,
    )
    count.full_clean()
    count.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=count,
        branch=branch,
        new_state=snapshot(count),
        source_document_type=STOCK_COUNT_SOURCE_TYPE,
        source_document_id=str(count.public_id),
    )
    return count


def _require_warehouse_is_countable(warehouse: Warehouse) -> None:
    """
    A count needs a place a person can walk into.

    In-transit stock is on a lorry. Nobody can count it, and the ledger already
    reconciles it against the transfers that put it there (ADR-020), which is a
    stronger check than a physical one and needs no freeze.
    """
    if not warehouse.is_active:
        raise ValidationError(
            _("Warehouse %(code)s is archived."),
            code="warehouse_inactive",
            params={"code": warehouse.code},
        )
    if warehouse.warehouse_type == WarehouseType.IN_TRANSIT or warehouse.is_system:
        raise ValidationError(
            _("Warehouse %(code)s is system-controlled and cannot be physically counted."),
            code="warehouse_not_countable",
            params={"code": warehouse.code},
        )


@transaction.atomic
def update_count(
    *,
    count: StockCount,
    reference: str = "",
    reason: str = "",
    cost_center: CostCenter | None = None,
) -> StockCount:
    """Amend a draft. Everything here is fixed the moment the count starts."""
    locked = StockCount.objects.select_for_update().get(pk=count.pk)
    _require_status(locked, (StockCountStatus.DRAFT,), "not_a_draft")
    before = snapshot(locked)
    locked.reference = reference.strip()
    locked.reason = reason.strip()
    locked.cost_center = cost_center
    locked.full_clean()
    locked.save(update_fields=["reference", "reason", "cost_center", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        previous_state=before,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def delete_count(*, count: StockCount, reason: str = "") -> None:
    """
    Discard a draft.

    Only a draft. A count that has started froze a warehouse and photographed a
    position, and both of those are facts somebody may later need to explain —
    so it is **cancelled**, never deleted (§T).
    """
    locked = StockCount.objects.select_for_update().get(pk=count.pk)
    _require_status(locked, (StockCountStatus.DRAFT,), "not_a_draft")
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.branch,
        previous_state=snapshot(locked),
        reason=reason or "count draft discarded",
    )
    locked.delete()


# ---------------------------------------------------------------------------
# Start: freeze and snapshot
# ---------------------------------------------------------------------------


@transaction.atomic
def start_count(*, count: StockCount, effective_at: datetime.datetime | None = None) -> StockCount:
    """
    Freeze the warehouse, fix the cutoff, and photograph every position in it.

    All of it or none of it. A warehouse frozen with no snapshot, or a snapshot
    of a warehouse that stayed open, are both worse than a failed start —
    they look like a count in progress and are not one.
    """
    # 1. The count row.
    locked = StockCount.objects.select_for_update().get(pk=count.pk)
    _require_status(locked, (StockCountStatus.DRAFT,), "not_a_draft")
    actor = _actor()

    # 2/3. The warehouse: the advisory lock first, so no posting can be in
    # flight while the snapshot is taken, then the row itself.
    lock_warehouses_exclusive([locked.warehouse_id])
    warehouse = Warehouse.objects.select_for_update().get(pk=locked.warehouse_id)
    _require_warehouse_is_countable(warehouse)
    if warehouse.frozen_by_count_id is not None:
        raise ValidationError(
            _("Warehouse %(code)s is already frozen by another count."),
            code="warehouse_already_frozen",
            params={"code": warehouse.code},
        )
    other = (
        StockCount.objects.filter(warehouse=warehouse, status__in=sorted(ACTIVE_COUNT_STATUSES))
        .exclude(pk=locked.pk)
        .first()
    )
    if other is not None:  # pragma: no cover - the unique constraint says so too
        raise ValidationError(
            _("Count %(count)s is already active in this warehouse."),
            code="count_already_active",
            params={"count": other.count_number or str(other.public_id)},
        )

    moment = effective_at or timezone.now()
    if timezone.is_naive(moment):
        raise ValidationError(
            _("The cutoff moment must state its timezone."), code="cutoff_must_be_aware"
        )
    day = resolve_business_day(locked.branch, moment)
    # Locked, so a period close cannot commit alongside this start and leave a
    # frozen warehouse stranded in a shut month. See `_period_for`.
    period = _period_for(organization=locked.organization, on_date=day.business_date, lock=True)

    locked.status = StockCountStatus.IN_PROGRESS
    locked.cutoff_at = moment
    locked.business_date = day.business_date
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    locked.conducted_by = actor
    locked.started_at = timezone.now()
    locked.count_number = next_document_number(
        organization=locked.organization,
        document_type=InventoryDocumentType.STOCK_COUNT,
        year=period.fiscal_year.year,
    )
    locked.full_clean()
    locked.save(
        update_fields=[
            "status",
            "cutoff_at",
            "business_date",
            "business_date_timezone",
            "business_day_start",
            "conducted_by",
            "started_at",
            "count_number",
            "updated_at",
        ]
    )

    warehouse.frozen_by_count = locked
    warehouse.save(update_fields=["frozen_by_count", "updated_at"])

    line_count = _snapshot_positions(locked)

    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(locked),
        reason="count started; warehouse frozen",
        source_document_type=STOCK_COUNT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "count_number": locked.count_number,
            "warehouse": warehouse.code,
            "cutoff_at": moment.isoformat(),
            "business_date": day.business_date.isoformat(),
            "lines": line_count,
        },
    )
    return locked


def _snapshot_positions(count: StockCount) -> int:
    """
    One immutable line per position the warehouse currently holds.

    Only positions with stock. A key that has emptied is not something anybody
    can be asked to go and count, and listing every item the warehouse has ever
    held would bury the sheet. If stock turns out to be there anyway, it is an
    **unexpected** line, added during counting with a book of zero — which is
    the honest record of what the books said.
    """
    balances = (
        StockBalance.objects.filter(warehouse_id=count.warehouse_id, quantity__gt=ZERO)
        .select_related("item", "item__base_unit", "lot", "control_account", "last_movement")
        .order_by("item__code", "lot__code", "pk")
    )
    lines = [
        StockCountLine(
            count=count,
            sequence=sequence,
            item=balance.item,
            lot=balance.lot,
            base_unit=balance.item.base_unit,
            book_quantity=balance.quantity,
            book_value=balance.value,
            book_average=balance.average_cost,
            book_control_account=balance.control_account,
            book_last_movement=balance.last_movement,
            book_posted_sequence=balance.last_posted_sequence,
        )
        for sequence, balance in enumerate(balances, start=1)
    ]
    StockCountLine.objects.bulk_create(lines)
    return len(lines)


# ---------------------------------------------------------------------------
# Blind entry
# ---------------------------------------------------------------------------


def blind_lines(count: StockCount) -> list[dict[str, Any]]:
    """
    The counting sheet, with **nothing** the conductor must not see.

    Built as plain dictionaries rather than model instances on purpose. A
    queryset of `StockCountLine` carries `book_quantity` on every row, and one
    careless serializer field or one `{{ line.book_quantity }}` in a template
    would leak it — through JSON, through a data attribute, through a hidden
    input, or through the HTML a curious counter can simply read. There is no
    way to leak a value that was never fetched.
    """
    rows = (
        StockCountLine.objects.filter(count=count)
        .select_related("item", "lot", "base_unit", "package_conversion")
        .order_by("sequence")
        .values(
            "id",
            "sequence",
            "line_uid",
            "item__code",
            "item__name_ar",
            "item__tracks_lots",
            "lot__code",
            "lot__expiry_date",
            "base_unit__code",
            "base_unit__name_ar",
            "counted_quantity",
            "entered_package_quantity",
            "measured_base_quantity",
            "package_conversion_id",
            "line_note",
            "is_unexpected",
        )
    )
    return [dict(row) for row in rows]


@transaction.atomic
def record_counts(*, count: StockCount, entries: list[CountEntry]) -> list[StockCountLine]:
    """
    Write counted quantities onto a count in progress.

    Zero is a valid answer and means the shelf is empty. `None` is not, and
    means nobody has been there yet — the distinction submission depends on.
    """
    locked = StockCount.objects.select_for_update().get(pk=count.pk)
    _require_status(locked, (StockCountStatus.IN_PROGRESS,), "count_not_in_progress")

    stored: list[StockCountLine] = []
    for entry in entries:
        if entry.line.count_id != locked.pk:
            raise ValidationError(
                _("That line belongs to another count."), code="line_count_mismatch"
            )
        line = StockCountLine.objects.select_for_update(of=("self",)).get(pk=entry.line.pk)
        quantity = _derive_counted_quantity(count=locked, line=line, entry=entry)
        if quantity < ZERO:
            raise ValidationError(
                _("A counted quantity cannot be negative."), code="counted_quantity_negative"
            )
        line.counted_quantity = quantity
        line.package_conversion = entry.package_conversion
        line.entered_package_quantity = entry.entered_package_quantity
        line.measured_base_quantity = entry.measured_base_quantity
        line.line_note = entry.note.strip()
        if entry.reason_code is not None:
            _validate_count_reason_code(locked, entry.reason_code)
            line.reason_code = entry.reason_code
        line.full_clean()
        line.save(
            update_fields=[
                "counted_quantity",
                "package_conversion",
                "entered_package_quantity",
                "measured_base_quantity",
                "line_note",
                "reason_code",
                "updated_at",
            ]
        )
        stored.append(line)

    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        reason="counted quantities recorded",
        source_document_type=STOCK_COUNT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={"lines": len(stored)},
    )
    return stored


def _validate_count_reason_code(count: StockCount, reason_code: InventoryReasonCode) -> None:
    if reason_code.organization_id != count.organization_id:
        raise ValidationError(
            _("Reason code %(code)s belongs to another organization."),
            code="reason_code_organization_mismatch",
            params={"code": reason_code.code},
        )
    if reason_code.applies_to != ReasonCodeApplication.COUNT_VARIANCE:
        raise ValidationError(
            _("Reason code %(code)s is not a count-variance reason."),
            code="reason_code_wrong_application",
            params={"code": reason_code.code},
        )
    if not reason_code.is_active:
        raise ValidationError(
            _("Reason code %(code)s is archived."),
            code="reason_code_archived",
            params={"code": reason_code.code},
        )


def _derive_counted_quantity(
    *, count: StockCount, line: StockCountLine, entry: CountEntry
) -> Decimal:
    """
    The counted base quantity, from whichever way it was entered.

    Direct base entry, a FIXED package count, or a VARIABLE package count with
    a measured weight — one of the three. The conversion is validated against
    the **count's business date**, not today's, so re-versioning a factor after
    the cutoff cannot restate what was counted.
    """
    conversion = entry.package_conversion
    if conversion is None:
        if entry.entered_package_quantity is not None or entry.measured_base_quantity is not None:
            raise ValidationError(
                _("A package quantity needs the package conversion it was counted in."),
                code="package_conversion_required",
            )
        if entry.base_quantity is None:
            raise ValidationError(_("The line needs a counted quantity."), code="quantity_required")
        return quantize_quantity(entry.base_quantity)

    if conversion.item_id != line.item_id:
        raise ValidationError(
            _("Conversion %(id)s belongs to another item."),
            code="conversion_item_mismatch",
            params={"id": conversion.pk},
        )
    assert count.business_date is not None  # noqa: S101 - IN_PROGRESS implies a cutoff
    if not conversion.covers(count.business_date):
        raise ValidationError(
            _("That conversion is not effective on the count's business date."),
            code="conversion_not_effective",
        )
    if entry.entered_package_quantity is None:
        raise ValidationError(
            _("A package conversion needs the number of packages counted."),
            code="package_quantity_required",
        )

    if conversion.conversion_type == ConversionType.VARIABLE:
        if entry.measured_base_quantity is None:
            raise ValidationError(
                _("A variable-weight package must be weighed; its factor is nominal."),
                code="measured_quantity_required",
            )
        return quantize_quantity(entry.measured_base_quantity)

    if entry.measured_base_quantity is not None:
        raise ValidationError(
            _("A fixed conversion computes its own base quantity and takes no measurement."),
            code="measured_quantity_not_accepted",
        )
    return quantize_quantity(entry.entered_package_quantity * conversion.factor_to_base)


@transaction.atomic
def add_unexpected_line(
    *,
    count: StockCount,
    item: InventoryItem,
    lot: InventoryLot | None = None,
    base_quantity: Decimal | None = None,
    package_conversion: ItemPackageConversion | None = None,
    entered_package_quantity: Decimal | None = None,
    measured_base_quantity: Decimal | None = None,
    note: str = "",
) -> StockCountLine:
    """
    Record stock that is physically there and absent from the books.

    Its book columns are zero — which is the truth, not a placeholder. What it
    is *worth* is left open: the conductor says how much is there, and an
    approver who can see cost says what it cost (§O).
    """
    locked = StockCount.objects.select_for_update().get(pk=count.pk)
    _require_status(locked, (StockCountStatus.IN_PROGRESS,), "count_not_in_progress")

    if item.organization_id != locked.organization_id:
        raise ValidationError(
            _("Item %(code)s belongs to another organization."),
            code="item_organization_mismatch",
            params={"code": item.code},
        )
    if not item.is_active:
        raise ValidationError(
            _("Item %(code)s is archived."), code="item_inactive", params={"code": item.code}
        )
    if item.tracks_lots:
        if lot is None:
            raise ValidationError(
                _("Item %(code)s tracks lots, so the line needs one."),
                code="lot_required",
                params={"code": item.code},
            )
        if lot.item_id != item.pk:
            raise ValidationError(
                _("Lot %(lot)s belongs to another item."),
                code="lot_item_mismatch",
                params={"lot": lot.code},
            )
        if item.tracks_expiry and lot.expiry_date is None:
            raise ValidationError(
                _("Item %(code)s tracks expiry, so its lots need an expiry date."),
                code="lot_expiry_required",
                params={"code": item.code},
            )
    elif lot is not None:
        raise ValidationError(
            _("Item %(code)s does not track lots, so the line must not name one."),
            code="lot_not_allowed",
            params={"code": item.code},
        )

    if StockCountLine.objects.filter(count=locked, item=item, lot=lot).exists():
        raise ValidationError(
            _("That item and lot are already on this count sheet."),
            code="duplicate_count_key",
        )

    last = StockCountLine.objects.filter(count=locked).order_by("-sequence").first()
    line = StockCountLine(
        count=locked,
        sequence=(last.sequence + 1) if last is not None else 1,
        item=item,
        lot=lot,
        base_unit=item.base_unit,
        book_quantity=ZERO,
        book_value=ZERO,
        book_average=ZERO,
        is_unexpected=True,
        line_note=note.strip(),
    )
    entry = CountEntry(
        line=line,
        base_quantity=base_quantity,
        package_conversion=package_conversion,
        entered_package_quantity=entered_package_quantity,
        measured_base_quantity=measured_base_quantity,
    )
    quantity = _derive_counted_quantity(count=locked, line=line, entry=entry)
    if quantity <= ZERO:
        raise ValidationError(
            _("Unexpected stock must be a positive quantity; there is no line to add otherwise."),
            code="unexpected_quantity_not_positive",
        )
    line.counted_quantity = quantity
    line.package_conversion = package_conversion
    line.entered_package_quantity = entered_package_quantity
    line.measured_base_quantity = measured_base_quantity
    line.full_clean()
    line.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(line),
        reason="unexpected stock recorded",
        source_document_type=STOCK_COUNT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={"item": item.code, "quantity": str(quantity)},
    )
    return line


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


@transaction.atomic
def submit_count(*, count: StockCount) -> StockCount:
    """
    Freeze the counted figures and hand the count to an approver.

    The warehouse stays shut. Submission ends the conductor's part, not the
    count: nothing has been valued and nothing has been posted, and reopening
    the warehouse now would let stock move between the sheet and the ledger
    entry that is supposed to explain it.
    """
    locked = StockCount.objects.select_for_update().get(pk=count.pk)
    _require_status(locked, (StockCountStatus.IN_PROGRESS,), "count_not_in_progress")
    actor = _actor()

    lines = list(StockCountLine.objects.filter(count=locked).order_by("sequence"))
    if not lines:
        raise ValidationError(_("An empty count cannot be submitted."), code="count_has_no_lines")
    uncounted = [line for line in lines if line.counted_quantity is None]
    if uncounted:
        raise ValidationError(
            _("%(count)s line(s) have not been counted yet, starting at line %(first)s."),
            code="count_incomplete",
            params={"count": len(uncounted), "first": uncounted[0].sequence},
        )

    for line in lines:
        assert line.counted_quantity is not None  # noqa: S101 - checked above
        line.variance_quantity = quantize_quantity(line.counted_quantity - line.book_quantity)
        line.save(update_fields=["variance_quantity", "updated_at"])

    locked.status = StockCountStatus.SUBMITTED
    locked.submitted_by = actor
    locked.submitted_at = timezone.now()
    locked.full_clean()
    locked.save(update_fields=["status", "submitted_by", "submitted_at", "updated_at"])
    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(locked),
        source_document_type=STOCK_COUNT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "lines": len(lines),
            "varying": sum(1 for line in lines if line.variance_quantity != ZERO),
        },
    )
    return locked


# ---------------------------------------------------------------------------
# Approval and posting
# ---------------------------------------------------------------------------


@transaction.atomic
def approve_count(*, count: StockCount, costs: list[ApprovedCost] | None = None) -> StockCount:
    """
    Approve the variance, post it, and release the warehouse.

    One transaction: the movements, the balances, the journal, the count's own
    state and the unfreeze all commit together or none of them do. A count that
    posted its stock and not its journal, or released a warehouse it had not
    finished with, would be worse than one that failed outright.
    """
    # 1. The count row.
    locked = StockCount.objects.select_for_update().get(pk=count.pk)
    _require_status(locked, (StockCountStatus.SUBMITTED,), "count_not_submitted")
    actor = _actor()
    if locked.conducted_by_id == actor.pk:
        raise ValidationError(
            _("A count is approved by somebody other than whoever conducted it."),
            code="approver_is_the_conductor",
        )

    # 2/3. The warehouse freeze this count owns.
    lock_warehouses_exclusive([locked.warehouse_id])
    warehouse = Warehouse.objects.select_for_update().get(pk=locked.warehouse_id)
    if warehouse.frozen_by_count_id != locked.pk:
        raise ValidationError(
            _("Warehouse %(code)s is not frozen by this count."),
            code="count_does_not_own_the_freeze",
            params={"code": warehouse.code},
        )

    assert locked.business_date is not None  # noqa: S101 - SUBMITTED implies a cutoff
    period = _period_for(organization=locked.organization, on_date=locked.business_date)

    # 4. The lines.
    lines = list(
        StockCountLine.objects.select_for_update(of=("self",))
        .filter(count=locked)
        .select_related(
            "item",
            "item__category",
            "item__category__parent",
            "item__category__parent__parent",
            "lot",
        )
        .order_by("pk")
    )
    _apply_approved_costs(locked, lines, costs or [])
    _require_snapshot_still_matches(locked, lines)

    # 5. The mappings, before a single account is resolved.
    lock_account_mappings_shared(locked.organization_id)
    variance_account = resolve_inventory_account(
        organization=locked.organization,
        role=INVENTORY_COUNT_VARIANCE,
        item=None,
        on_date=locked.business_date,
    )
    require_cost_center_where_the_account_demands_one(
        account=variance_account.account, cost_center=locked.cost_center
    )
    variances = _resolve_variances(locked, lines)

    if variances:
        _post_count_variance(
            count=locked,
            variances=variances,
            warehouse=warehouse,
            period=period,
            variance_account=variance_account.account,
        )

    # 12. Only now, with every posting done, is the warehouse released — and
    # it is released *before* the count leaves its active state, because a
    # count that is no longer active may not still hold a freeze and the
    # database refuses both halves of that. Ordering statements is not the same
    # as unfreezing early: this whole function is one transaction, so a failure
    # anywhere after this point still leaves the warehouse frozen and the count
    # SUBMITTED, which is what §R actually asks for.
    warehouse.frozen_by_count = None
    warehouse.save(update_fields=["frozen_by_count", "updated_at"])

    locked.status = StockCountStatus.POSTED
    locked.approved_by = actor
    locked.approved_at = timezone.now()
    locked.full_clean()
    locked.save(
        update_fields=[
            "status",
            "approved_by",
            "approved_at",
            "stock_entry",
            "journal_entry",
            "updated_at",
        ]
    )

    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        branch=locked.branch,
        new_state=snapshot(locked),
        source_document_type=STOCK_COUNT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "count_number": locked.count_number,
            "varying_lines": len(variances),
            "stock_entry": locked.stock_entry_id,
            "journal_entry": locked.journal_entry.entry_number if locked.journal_entry else None,
        },
    )
    return locked


def _apply_approved_costs(
    count: StockCount, lines: list[StockCountLine], costs: list[ApprovedCost]
) -> None:
    """Write the approver's unit costs onto the lines that need them."""
    by_id = {line.pk: line for line in lines}
    for supplied in costs:
        line = by_id.get(supplied.line.pk)
        if line is None:
            raise ValidationError(
                _("That line belongs to another count."), code="line_count_mismatch"
            )
        unit_cost = quantize_unit_price(supplied.unit_cost)
        if unit_cost < ZERO:
            raise ValidationError(
                _("An approved unit cost cannot be negative."), code="unit_cost_negative"
            )
        if unit_cost == ZERO and not supplied.zero_confirmed:
            raise ValidationError(
                _(
                    "A zero unit cost must be confirmed explicitly. Stock found and booked at "
                    "nothing is a decision, not a default."
                ),
                code="zero_cost_not_confirmed",
            )
        line.approved_unit_cost = unit_cost
        line.zero_cost_confirmed = unit_cost == ZERO and supplied.zero_confirmed
        line.save(update_fields=["approved_unit_cost", "zero_cost_confirmed", "updated_at"])


def _require_snapshot_still_matches(count: StockCount, lines: list[StockCountLine]) -> None:
    """
    The book position must be exactly what was photographed at the cutoff.

    If it is not, the freeze did not hold or something wrote a balance directly,
    and either way the variance about to be posted was computed against a
    position that no longer exists. Posting it anyway would bury the evidence
    of the first fault inside a plausible-looking second one, so this refuses
    and names the line.
    """
    balances = {
        (balance.item_id, balance.lot_id): balance
        for balance in StockBalance.objects.filter(warehouse_id=count.warehouse_id)
    }
    for line in lines:
        balance = balances.get((line.item_id, line.lot_id))
        current_quantity = balance.quantity if balance is not None else ZERO
        current_value = balance.value if balance is not None else ZERO
        if current_quantity != line.book_quantity or current_value != line.book_value:
            raise ValidationError(
                _(
                    "The book position for %(item)s has changed since the cutoff: the count "
                    "recorded %(book)s and the ledger now holds %(current)s. The freeze was "
                    "bypassed; this count cannot be posted."
                ),
                code="count_snapshot_mismatch",
                params={
                    "item": line.item.code,
                    "book": str(line.book_quantity),
                    "current": str(current_quantity),
                },
            )


def _resolve_variances(count: StockCount, lines: list[StockCountLine]) -> list[_Variance]:
    """
    Which lines move stock, in which direction, and at what unit cost.

    A gain into standing stock takes the standing average, so finding more of
    something does not restate what the rest of it cost. A gain into an empty
    position has no average to take and needs the approver's figure. A loss
    takes nothing: the kernel values it at the average and applies the
    full-depletion rule, which is the whole point of asking it rather than
    computing a value here.
    """
    assert count.business_date is not None  # noqa: S101 - SUBMITTED implies a cutoff
    variances: list[_Variance] = []
    for line in lines:
        variance = line.variance_quantity or ZERO
        if variance == ZERO:
            continue

        if variance < ZERO:
            control = line.book_control_account
            if control is None:
                control = resolve_inventory_account(
                    organization=count.organization,
                    role=INVENTORY_CONTROL,
                    item=line.item,
                    on_date=count.business_date,
                ).account
            variances.append(
                _Variance(line=line, quantity=variance, unit_cost=None, control_account=control)
            )
            continue

        if line.book_quantity > ZERO and line.book_average > ZERO:
            unit_cost = line.book_average
            control = line.book_control_account
            if control is None:  # pragma: no cover - standing stock always names one
                control = resolve_inventory_account(
                    organization=count.organization,
                    role=INVENTORY_CONTROL,
                    item=line.item,
                    on_date=count.business_date,
                ).account
        else:
            if line.approved_unit_cost is None:
                raise ValidationError(
                    _(
                        "%(item)s was found with no book value to price it at, so its unit cost "
                        "must be approved explicitly before this count can post."
                    ),
                    code="approved_unit_cost_required",
                    params={"item": line.item.code},
                )
            unit_cost = line.approved_unit_cost
            # A newly valued position takes the mapping effective on the
            # count's own business date, not today's.
            control = resolve_inventory_account(
                organization=count.organization,
                role=INVENTORY_CONTROL,
                item=line.item,
                on_date=count.business_date,
            ).account
        variances.append(
            _Variance(line=line, quantity=variance, unit_cost=unit_cost, control_account=control)
        )
    return variances


def _post_count_variance(
    *,
    count: StockCount,
    variances: list[_Variance],
    warehouse: Warehouse,
    period: AccountingPeriod,
    variance_account: Account,
) -> None:
    """Post the movements, then the journal, and link both to the count."""
    assert count.business_date is not None  # noqa: S101 - SUBMITTED implies a cutoff
    effects = [
        MovementInput(
            warehouse=warehouse,
            item=variance.line.item,
            movement_type=(
                MovementType.COUNT_GAIN if variance.quantity > ZERO else MovementType.COUNT_LOSS
            ),
            quantity=abs(variance.quantity),
            effect_key=f"count-line:{variance.line.line_uid}",
            lot=variance.line.lot,
            unit_cost=variance.unit_cost,
            source_conversion=variance.line.package_conversion,
            control_account=variance.control_account,
        )
        for variance in variances
    ]
    # 6. Every key this event touches, canonically, before any of it moves.
    acquire_stock_key_locks(effects)

    stock_entry = post_stock_entry(
        organization=count.organization,
        effects=effects,
        idempotency_key=f"stock-count:{count.public_id}",
        effective_at=count.cutoff_at,
        business_date=count.business_date,
        source_document_type=STOCK_COUNT_SOURCE_TYPE,
        source_document_id=str(count.public_id),
        source_event=SourceEvent.POSTED,
        reference=count.reference,
        reason=count.reason or "physical count variance",
        # This count owns this warehouse's freeze, and is the one posting the
        # freeze exists to make possible.
        owned_freezes=[warehouse.pk],
    )
    movements = {movement.effect_key: movement for movement in stock_entry.movements.all()}
    for variance in variances:
        movement = movements[f"count-line:{variance.line.line_uid}"]
        variance.line.variance_value = movement.inventory_value
        variance.line.movement = movement
        variance.line.save(update_fields=["variance_value", "movement", "updated_at"])

    count.stock_entry = stock_entry

    journal_lines = _count_journal_lines(count, variances, variance_account=variance_account)
    if not journal_lines:
        # Quantity moved and money did not — the only way that happens is a
        # gain whose unit cost was explicitly confirmed as zero. There is
        # genuinely nothing for the general ledger to record, and posting an
        # empty entry to say so would be a journal that means nothing.
        return

    journal = post_entry(
        organization=count.organization,
        accounting_date=count.business_date,
        lines=journal_lines,
        idempotency_key=f"stock-count-journal:{count.public_id}",
        document_date=count.business_date,
        narration=count.reason or f"جرد {count.count_number}",
        source_document_type=STOCK_COUNT_SOURCE_TYPE,
        source_document_id=str(count.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=POSTING_RULE_COUNT,
    )
    link_journal_entry(entry=stock_entry, journal=journal)
    count.journal_entry = journal


def _count_journal_lines(
    count: StockCount, variances: list[_Variance], *, variance_account: Account
) -> list[PostingLine]:
    """
    Group the stored line values into balanced journal lines.

        gain   Dr Inventory Control      Cr Inventory Count Variance
        loss   Dr Inventory Count Variance   Cr Inventory Control

    Grouped by account and **direction**, never netted: a count that found
    300,000 of rice and lost 280,000 of chicken is not a 20,000 event, and a
    single net line would say it was. Every figure is a sum of stored
    3-decimal movement values.
    """
    control_debits: dict[int, Decimal] = {}
    control_credits: dict[int, Decimal] = {}
    accounts: dict[int, Account] = {}
    variance_debit = ZERO
    variance_credit = ZERO

    for variance in variances:
        value = variance.line.variance_value or ZERO
        account = variance.control_account
        accounts[account.pk] = account
        if variance.quantity > ZERO:
            control_debits[account.pk] = control_debits.get(account.pk, ZERO) + value
            variance_credit += value
        else:
            amount = abs(value)
            control_credits[account.pk] = control_credits.get(account.pk, ZERO) + amount
            variance_debit += amount

    lines: list[PostingLine] = []
    for account_id, amount in sorted(
        control_debits.items(), key=lambda pair: accounts[pair[0]].code
    ):
        if amount > ZERO:
            lines.append(
                PostingLine(
                    account=accounts[account_id],
                    branch=count.branch,
                    debit=amount,
                    credit=ZERO,
                )
            )
    for account_id, amount in sorted(
        control_credits.items(), key=lambda pair: accounts[pair[0]].code
    ):
        if amount > ZERO:
            lines.append(
                PostingLine(
                    account=accounts[account_id],
                    branch=count.branch,
                    debit=ZERO,
                    credit=amount,
                )
            )
    if variance_debit > ZERO:
        lines.append(
            PostingLine(
                account=variance_account,
                branch=count.branch,
                cost_center=count.cost_center,
                debit=variance_debit,
                credit=ZERO,
            )
        )
    if variance_credit > ZERO:
        lines.append(
            PostingLine(
                account=variance_account,
                branch=count.branch,
                cost_center=count.cost_center,
                debit=ZERO,
                credit=variance_credit,
            )
        )
    # Empty when every variance was valued at zero. The caller posts no journal
    # rather than an empty one.
    return lines


# ---------------------------------------------------------------------------
# Cancellation and reversal
# ---------------------------------------------------------------------------


@transaction.atomic
def cancel_count(*, count: StockCount, reason: str) -> StockCount:
    """
    Abandon a count without posting anything, and reopen the warehouse.

    The count is **kept**. Its cutoff, its snapshot and its counted figures are
    what a later question about that warehouse's history will be asked against,
    and deleting them would leave a frozen afternoon nobody can account for.
    """
    if not reason.strip():
        raise ValidationError(_("Cancelling a count requires a reason."), code="reason_required")

    locked = StockCount.objects.select_for_update().get(pk=count.pk)
    _require_status(
        locked,
        (StockCountStatus.IN_PROGRESS, StockCountStatus.SUBMITTED),
        "count_not_active",
    )
    actor = _actor()

    lock_warehouses_exclusive([locked.warehouse_id])
    warehouse = Warehouse.objects.select_for_update().get(pk=locked.warehouse_id)
    if warehouse.frozen_by_count_id != locked.pk:  # pragma: no cover - an active count owns it
        raise ValidationError(
            _("Warehouse %(code)s is not frozen by this count."),
            code="count_does_not_own_the_freeze",
            params={"code": warehouse.code},
        )

    # Exactly its own freeze, and only because it was verified above to be the
    # owner. A cancellation that cleared the column unconditionally would let
    # one count release another's warehouse. Released before the status moves,
    # for the reason given in `approve_count`.
    warehouse.frozen_by_count = None
    warehouse.save(update_fields=["frozen_by_count", "updated_at"])

    locked.status = StockCountStatus.CANCELLED
    locked.cancelled_by = actor
    locked.cancelled_at = timezone.now()
    locked.cancellation_reason = reason.strip()
    locked.full_clean()
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
        new_state=snapshot(locked),
        reason=reason,
        source_document_type=STOCK_COUNT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={"warehouse": warehouse.code},
    )
    return locked


@transaction.atomic
def reverse_count(*, count: StockCount, reason: str) -> StockCount:
    """
    Undo a posted count's variance with an exact mirrored event.

    Dated **today**, not at the original cutoff: a reversal is a new economic
    event in the current period, and backdating it into the month the count
    belongs to is what a reversal exists to avoid.

    The warehouse is **not** frozen again. Reversal says the posted variance
    was wrong; it says nothing about the shelves, and re-freezing a working
    warehouse on the strength of a correction would stop a branch trading. A
    corrected figure needs a new count, which is the only thing that can
    produce one honestly (§U).
    """
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")

    locked = StockCount.objects.select_for_update().get(pk=count.pk)
    _require_status(locked, (StockCountStatus.POSTED,), "count_not_posted")
    actor = _actor()

    if locked.stock_entry is None:
        raise ValidationError(
            _("This count posted no variance, so there is nothing to reverse."),
            code="count_has_no_variance",
        )

    reversal_day = resolve_business_day(locked.branch, timezone.now())
    _period_for(organization=locked.organization, on_date=reversal_day.business_date)

    reversal_entry = reverse_stock_entry(
        entry=locked.stock_entry,
        idempotency_key=f"stock-count-reversal:{locked.public_id}",
        reason=reason.strip(),
        business_date=reversal_day.business_date,
    )
    # Null when the count moved quantity at a confirmed zero cost: there was no
    # journal, so there is none to mirror.
    reversal_journal = None
    if locked.journal_entry is not None:
        reversal_journal = reverse_entry(
            entry=locked.journal_entry,
            idempotency_key=f"stock-count-journal-reverse:{locked.public_id}",
            reason=reason.strip(),
            accounting_date=reversal_day.business_date,
        )
        link_journal_entry(entry=reversal_entry, journal=reversal_journal)

    locked.status = StockCountStatus.REVERSED
    locked.reversed_by = actor
    locked.reversed_at = timezone.now()
    locked.reversal_reason = reason.strip()
    locked.reversal_stock_entry = reversal_entry
    locked.reversal_journal_entry = reversal_journal
    locked.full_clean()
    locked.save(
        update_fields=[
            "status",
            "reversed_by",
            "reversed_at",
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
        new_state=snapshot(locked),
        reason=reason,
        source_document_type=STOCK_COUNT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "reversal_journal": (
                reversal_journal.entry_number if reversal_journal is not None else None
            )
        },
    )
    return locked


# ---------------------------------------------------------------------------
# The period-close guard (§S)
# ---------------------------------------------------------------------------


def refuse_close_while_a_count_is_active(period: AccountingPeriod, new_state: str) -> None:
    """
    An active count blocks closing the period its cutoff falls in.

    Without this a count can freeze a warehouse on the 30th, the month closes
    on the 1st, and the count becomes impossible to post *and* impossible to
    cancel out of usefully — the warehouse stays shut until somebody reopens a
    closed period to let it finish. Refusing the close is the cheaper of the
    two, and it names the count and the warehouse so the answer is obvious.

    Registered with `apps.accounting.services` at app-ready, so accounting
    never learns what a stock count is (ADR-021 §9).
    """
    if new_state not in (PeriodState.SOFT_CLOSED, PeriodState.CLOSED):  # pragma: no cover
        return
    blocking = (
        StockCount.objects.filter(
            organization_id=period.fiscal_year.organization_id,
            status__in=sorted(ACTIVE_COUNT_STATUSES),
            business_date__gte=period.start_date,
            business_date__lte=period.end_date,
        )
        .select_related("warehouse")
        .order_by("count_number")
        .first()
    )
    if blocking is not None:
        raise ValidationError(
            _(
                "Count %(count)s is still active in warehouse %(warehouse)s on %(date)s. "
                "Post or cancel it before closing this period."
            ),
            code="active_inventory_count",
            params={
                "count": blocking.count_number,
                "warehouse": blocking.warehouse.code,
                "date": blocking.business_date.isoformat() if blocking.business_date else "",
            },
        )


__all__ = [
    "ApprovedCost",
    "CountEntry",
    "add_unexpected_line",
    "approve_count",
    "blind_lines",
    "cancel_count",
    "create_count",
    "delete_count",
    "record_counts",
    "refuse_close_while_a_count_is_active",
    "reverse_count",
    "start_count",
    "submit_count",
    "update_count",
]
