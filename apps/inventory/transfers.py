"""
Stock transfers: dispatch, in-transit custody, partial receipt, shortage.

A transfer is not one posting. It is an aggregate of separately posted events
that each move real value, and modelling it as a single document would force
either a status that lies about how much has arrived or a hidden second
document nobody can audit.

    dispatch    Dr Inventory In-Transit         Cr Source Inventory Control
    receipt     Dr Destination Inventory        Cr Inventory In-Transit
                (same branch — one journal)
    receipt     Dr Inter-Branch Clearing        Cr Inventory In-Transit
                Dr Destination Inventory        Cr Inter-Branch Clearing
                (two branches — two journals, one per branch, each balanced)
    shortage    Dr Inventory Shortage Loss      Cr Inventory In-Transit

Four things those lines protect.

**The goods stay the source branch's until they arrive.** They sit in that
branch's in-transit warehouse from dispatch until a receipt takes its share
out, which is both the accounting truth and the answer to "whose loss is it if
the lorry never turns up" (ADR-020 §1).

**A receipt is valued from its own dispatch, never from the in-transit
average.** One in-transit position pools every transfer of that item in
flight, so its average is a blend of them all; receiving against it would take
one transfer's quantity out at another transfer's cost, and the difference
would never come back. Each transfer line keeps its own remaining quantity and
value, and a receipt consumes a share of *that* (ADR-020 §4–5).

**The last event takes the exact remainder.** The final receipt, or the
shortage that closes what is left, is valued at the remaining value to the
dinar rather than at a recomputed share — so receipts plus shortage always
equal the dispatch, with no residual for anybody to chase later.

**A cross-branch receipt keeps each branch balanced on its own books.** One
two-line journal spanning both would leave each branch's standalone trial
balance out by the value of the goods. Two journals meeting at inter-branch
clearing leave each balanced and the clearing account netting to zero for the
complete event (ADR-020 §9).

## Locking

Every posting takes, in this order (ADR-020 §11):

    1. the parent transfer row              select_for_update
    2. the receipt or shortage row          select_for_update
    3. the organization's mapping lock      shared
    4. the transfer lines being resolved    select_for_update, by primary key
    5. every stock key the event touches    advisory, canonical order
    6. the inventory posted-sequence        inside the kernel
    7. the document-number sequence
    8. the journal-number sequence          inside post_entry

Step 5 is taken **up front, across both sides of the event**, and that is not
a detail. A receipt releases from in-transit and lands at the destination
through two separate kernel calls, and letting each sort its own single key
would order them by the order the calls are written rather than canonically.
A dispatch of `W_A -> W_B` locks `(W_A, item)` then `(IN_TRANSIT, item)`; a
receipt of `W_B -> W_A` in the same branch would lock `(IN_TRANSIT, item)`
then `(W_A, item)` — opposite order, same two keys, and the two deadlock. One
canonical acquisition covering the whole event removes the cycle.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    INTER_BRANCH_CLEARING,
    INVENTORY_CONTROL,
    INVENTORY_IN_TRANSIT,
    INVENTORY_SHORTAGE_LOSS,
    Account,
    AccountingPeriod,
    CostCenter,
    JournalEntry,
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
    MovementInput,
    acquire_movement_key_locks,
    acquire_stock_key_locks,
    link_journal_entry,
    post_stock_entry,
    reverse_stock_entry,
)
from apps.inventory.models import (
    TRANSFER_DISPATCH_SOURCE_TYPE,
    TRANSFER_RECEIPT_DESTINATION_TYPE,
    TRANSFER_RECEIPT_SOURCE_TYPE,
    TRANSFER_SHORTAGE_SOURCE_TYPE,
    ConversionType,
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryItem,
    InventoryLot,
    ItemPackageConversion,
    MovementType,
    StockBalance,
    StockLedgerEntry,
    StockTransfer,
    StockTransferLine,
    StockTransferReceipt,
    StockTransferReceiptLine,
    StockTransferShortage,
    StockTransferShortageLine,
    StockTransferStatus,
    Warehouse,
)
from apps.inventory.operations import (
    next_document_number,
    require_cost_center_where_the_account_demands_one,
)
from apps.inventory.services import ensure_in_transit_warehouse
from apps.organizations.business_dates import resolve_business_day
from apps.organizations.models import Branch, Organization
from apps.users.models import User

ZERO = Decimal("0")

#: Stamped on each journal, so an entry always says which rule produced it.
DISPATCH_POSTING_RULE = "inventory-transfer-dispatch-v1"
RECEIPT_POSTING_RULE = "inventory-transfer-receipt-v1"
SHORTAGE_POSTING_RULE = "inventory-transfer-shortage-v1"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransferLineInput:
    """One requested transfer line, before validation derives its quantity."""

    item: InventoryItem
    lot: InventoryLot | None = None
    package_conversion: ItemPackageConversion | None = None
    entered_package_quantity: Decimal | None = None
    measured_base_quantity: Decimal | None = None
    base_quantity: Decimal | None = None


@dataclass(frozen=True)
class ReceiptLineInput:
    """
    How much of one transfer line has arrived.

    The conversion is **not** an input. A receipt is measuring the same
    physical shipment the dispatch described, so it counts in the packaging the
    dispatch recorded; letting the receiver name a conversion would let a
    factor edited in between restate what was sent (§O).
    """

    transfer_line: StockTransferLine
    entered_package_quantity: Decimal | None = None
    measured_base_quantity: Decimal | None = None
    base_quantity: Decimal | None = None


@dataclass
class _Allocation:
    """One transfer line's share of an event, in quantity and exact value."""

    line: StockTransferLine
    quantity: Decimal
    value: Decimal
    unit_cost: Decimal


@dataclass(frozen=True)
class _Journals:
    """
    The journals one event produced, per branch side.

    Same-branch fills both with the one branch-local journal; cross-branch
    fills them with two. Comparing the two ids is therefore the honest test of
    whether the event was coordinated, and it needs no separate flag that could
    disagree with the rows.
    """

    source: JournalEntry
    destination: JournalEntry


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _aware(moment: datetime.datetime) -> datetime.datetime:
    if timezone.is_naive(moment):
        raise ValidationError(
            _("The effective moment must state its timezone."), code="effective_at_must_be_aware"
        )
    return moment


def _require_transfer_status(transfer: StockTransfer, allowed: Sequence[str], code: str) -> None:
    if transfer.status not in allowed:
        raise ValidationError(
            _("Transfer %(doc)s is %(actual)s."),
            code=code,
            params={
                "doc": transfer.transfer_number or str(transfer.public_id),
                "actual": transfer.get_status_display(),
            },
        )


def _require_event_status(
    event: StockTransferReceipt | StockTransferShortage, status: str, code: str
) -> None:
    if event.status != status:
        raise ValidationError(
            _("This transfer event is %(actual)s, not %(expected)s."),
            code=code,
            params={"actual": event.get_status_display(), "expected": status},
        )


def _actor() -> User:
    actor = get_actor()
    if actor is None:
        raise ValidationError(
            _("Posting needs a signed-in actor to record."), code="actor_required"
        )
    return actor


def _period_for(*, organization: Organization, on_date: datetime.date) -> AccountingPeriod:
    """
    Resolve one branch's period for its **own** business date, and demand it
    be open.

    Called once per branch involved, never once per event: a cross-branch
    receipt validates two periods because it dates two sets of books, and
    either being closed rolls the whole thing back (§H).
    """
    period = resolve_period(organization=organization, accounting_date=on_date)
    validate_period_accepts_postings(period)
    return period


def _standing_account(warehouse: Warehouse, line: StockTransferLine) -> Account | None:
    """
    The account the stock at this position currently sits in.

    Read from the balance, never resolved afresh: value leaves through the
    account it entered, whatever the mapping says today (ADR-019 §7).
    """
    balance = (
        StockBalance.objects.filter(warehouse=warehouse, item=line.item, lot=line.lot)
        .select_related("control_account")
        .first()
    )
    return balance.control_account if balance is not None else None


def allocate(
    *, remaining_quantity: Decimal, remaining_value: Decimal, taken_quantity: Decimal
) -> Decimal:
    """
    The share of a transfer line's remaining value that `taken_quantity` takes.

        taken == remaining  ->  the whole remaining value, exactly
        taken <  remaining  ->  remaining_value x taken / remaining

    The equality branch is what makes the arithmetic close. Computing the last
    event's share by the ratio would leave a dinar or two of value standing
    against no quantity, and nothing downstream could ever clear it — the same
    reasoning as the kernel's full-depletion rule, applied to an allocation
    basis instead of a balance (ADR-020 §5).

    The ratio is evaluated as one expression and quantized once at the end.
    Rounding the multiplication and the division separately would drift.
    """
    if taken_quantity == remaining_quantity:
        return remaining_value
    return quantize_money(remaining_value * taken_quantity / remaining_quantity)


def _derive_quantity(
    *,
    conversion: ItemPackageConversion | None,
    entered_package_quantity: Decimal | None,
    measured_base_quantity: Decimal | None,
    base_quantity: Decimal | None,
    item: InventoryItem,
) -> Decimal:
    """
    The authoritative base quantity, from whichever way it was entered.

    Direct base entry, a FIXED package count, or a VARIABLE package count with
    a measured weight — one of the three, never a mixture, and the conversion
    is validated against the **business date** rather than the wall clock.
    """
    if conversion is None:
        if entered_package_quantity is not None or measured_base_quantity is not None:
            raise ValidationError(
                _("A package quantity needs the package conversion it was counted in."),
                code="package_conversion_required",
            )
        if base_quantity is None:
            raise ValidationError(_("The line needs a quantity."), code="quantity_required")
        return quantize_quantity(base_quantity)

    if conversion.item_id != item.pk:
        raise ValidationError(
            _("The conversion belongs to another item."), code="conversion_item_mismatch"
        )
    if base_quantity is not None:
        raise ValidationError(
            _("Enter either a package count or a base quantity, not both."),
            code="quantity_entered_twice",
        )
    if entered_package_quantity is None or entered_package_quantity <= ZERO:
        raise ValidationError(
            _("The package count must be greater than zero."), code="package_quantity_required"
        )
    if not conversion.allows_fractional and entered_package_quantity % 1 != 0:
        raise ValidationError(
            _("This package does not come in fractions."), code="fractional_packages_not_allowed"
        )

    if conversion.conversion_type == ConversionType.VARIABLE:
        # The stored factor is a planning estimate; the scale is the truth,
        # and it is the truth again at the receiving end.
        if measured_base_quantity is None or measured_base_quantity <= ZERO:
            raise ValidationError(
                _("A variable package needs the measured base quantity."),
                code="measured_quantity_required",
            )
        return quantize_quantity(measured_base_quantity)

    if measured_base_quantity is not None:
        raise ValidationError(
            _("A fixed package converts arithmetically; a measured quantity means VARIABLE."),
            code="measured_only_for_variable",
        )
    return quantize_quantity(entered_package_quantity * conversion.factor_to_base)


def _validate_conversion_is_effective(
    conversion: ItemPackageConversion, on_date: datetime.date
) -> None:
    if not conversion.is_active:
        raise ValidationError(
            _("Conversion version %(version)s is no longer active."),
            code="conversion_inactive",
            params={"version": conversion.version},
        )
    covers = conversion.effective_from <= on_date and (
        conversion.effective_to is None or on_date <= conversion.effective_to
    )
    if not covers:
        raise ValidationError(
            _("The conversion is not effective on %(date)s."),
            code="conversion_not_effective",
            params={"date": on_date.isoformat()},
        )


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


@transaction.atomic
def create_transfer(
    *,
    organization: Organization,
    source_warehouse: Warehouse,
    destination_warehouse: Warehouse,
    effective_at: datetime.datetime,
    evidence_reference: str,
    narration: str = "",
) -> StockTransfer:
    """Start a draft transfer between two warehouses of one organization."""
    _validate_transfer_endpoints(
        organization=organization,
        source_warehouse=source_warehouse,
        destination_warehouse=destination_warehouse,
    )
    if not evidence_reference.strip():
        raise ValidationError(
            _("A transfer needs its evidence reference."), code="evidence_reference_required"
        )

    moment = _aware(effective_at)
    transfer = StockTransfer(
        organization=organization,
        source_warehouse=source_warehouse,
        destination_warehouse=destination_warehouse,
        effective_at=moment,
        # A preview until dispatch fixes it, exactly as a draft receipt's is.
        business_date=resolve_business_day(source_warehouse.branch, moment).business_date,
        evidence_reference=evidence_reference.strip(),
        narration=narration.strip(),
        created_by=get_actor(),
    )
    transfer.full_clean()
    transfer.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=transfer,
        branch=source_warehouse.branch,
        new_state=snapshot(transfer),
    )
    return transfer


def _validate_transfer_endpoints(
    *,
    organization: Organization,
    source_warehouse: Warehouse,
    destination_warehouse: Warehouse,
) -> None:
    """
    Both ends real, distinct, active, inside this organization, and neither a
    system warehouse.

    Cross-organization movement is refused outright rather than modelled: two
    organizations are two sets of books, and goods crossing between them is a
    sale and a purchase with an invoice and a price, not an internal transfer
    at cost (§F).
    """
    for warehouse, label in (
        (source_warehouse, "source"),
        (destination_warehouse, "destination"),
    ):
        if warehouse.branch.organization_id != organization.pk:
            raise ValidationError(
                _("Warehouse %(code)s belongs to another organization."),
                code="warehouse_organization_mismatch",
                params={"code": warehouse.code},
            )
        if not warehouse.is_active:
            raise ValidationError(
                _("Warehouse %(code)s is archived."),
                code="warehouse_inactive",
                params={"code": warehouse.code},
            )
        if not warehouse.branch.is_active:
            raise ValidationError(
                _("Branch %(code)s is closed."),
                code="branch_inactive",
                params={"code": warehouse.branch.code},
            )
        if warehouse.is_system:
            # In-transit is picked by the posting service from the source
            # branch. Offering it as an endpoint would let somebody dispatch
            # out of the place goods already in flight are sitting, or land a
            # transfer in a warehouse that has no physical existence.
            raise ValidationError(
                _("%(code)s is the system in-transit warehouse and cannot be chosen."),
                code="system_warehouse_not_selectable",
                params={"code": warehouse.code, "side": label},
            )
    if source_warehouse.pk == destination_warehouse.pk:
        raise ValidationError(
            _("A transfer needs two different warehouses."), code="transfer_endpoints_identical"
        )


@transaction.atomic
def update_transfer(
    *,
    transfer: StockTransfer,
    effective_at: datetime.datetime | None = None,
    evidence_reference: str | None = None,
    narration: str | None = None,
) -> StockTransfer:
    """Amend a draft's header. Anything past DRAFT is frozen by trigger."""
    locked = StockTransfer.objects.select_for_update().get(pk=transfer.pk)
    _require_transfer_status(locked, [StockTransferStatus.DRAFT], "not_a_draft")
    before = snapshot(locked)

    if effective_at is not None:
        locked.effective_at = _aware(effective_at)
        locked.business_date = resolve_business_day(
            locked.source_warehouse.branch, locked.effective_at
        ).business_date
    if evidence_reference is not None:
        if not evidence_reference.strip():
            raise ValidationError(
                _("A transfer needs its evidence reference."), code="evidence_reference_required"
            )
        locked.evidence_reference = evidence_reference.strip()
    if narration is not None:
        locked.narration = narration.strip()

    locked.full_clean()
    locked.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.source_warehouse.branch,
        previous_state=before,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def delete_transfer(*, transfer: StockTransfer, reason: str = "") -> None:
    """Discard a draft. Only a draft — the trigger refuses anything later."""
    locked = StockTransfer.objects.select_for_update().get(pk=transfer.pk)
    _require_transfer_status(locked, [StockTransferStatus.DRAFT], "not_a_draft")
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.source_warehouse.branch,
        previous_state=snapshot(locked),
        reason=reason,
    )
    locked.lines.all().delete()
    locked.delete()


@transaction.atomic
def add_transfer_line(*, transfer: StockTransfer, line: TransferLineInput) -> StockTransferLine:
    """Add one item to a draft transfer."""
    locked = StockTransfer.objects.select_for_update().get(pk=transfer.pk)
    _require_transfer_status(locked, [StockTransferStatus.DRAFT], "not_a_draft")

    if line.item.organization_id != locked.organization_id:
        raise ValidationError(
            _("Item %(code)s belongs to another organization."),
            code="item_organization_mismatch",
            params={"code": line.item.code},
        )
    if not line.item.is_active:
        raise ValidationError(
            _("Item %(code)s is archived."), code="item_inactive", params={"code": line.item.code}
        )
    _validate_lot(line.item, line.lot)

    if line.package_conversion is not None:
        _validate_conversion_is_effective(line.package_conversion, locked.business_date)
    base_quantity = _derive_quantity(
        conversion=line.package_conversion,
        entered_package_quantity=line.entered_package_quantity,
        measured_base_quantity=line.measured_base_quantity,
        base_quantity=line.base_quantity,
        item=line.item,
    )
    if base_quantity <= ZERO:
        raise ValidationError(
            _("The quantity must be greater than zero."), code="quantity_not_positive"
        )
    if locked.lines.filter(item=line.item, lot=line.lot).exists():
        # One physical position, one line. Two would each carry their own
        # remaining balance for stock that has only one.
        raise ValidationError(
            _("This item and lot already have a line on this transfer."),
            code="duplicate_valuation_key",
        )

    last = locked.lines.order_by("-sequence").first()
    stored = StockTransferLine(
        transfer=locked,
        sequence=(last.sequence + 1) if last is not None else 1,
        item=line.item,
        lot=line.lot,
        package_conversion=line.package_conversion,
        entered_package_quantity=line.entered_package_quantity,
        measured_base_quantity=line.measured_base_quantity,
        base_quantity=base_quantity,
    )
    stored.full_clean()
    stored.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.source_warehouse.branch,
        new_state=snapshot(stored),
        metadata={"line": stored.sequence, "item": line.item.code},
        reason="transfer line added",
    )
    return stored


def _validate_lot(item: InventoryItem, lot: InventoryLot | None) -> None:
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
    elif lot is not None:
        raise ValidationError(
            _("Item %(code)s does not track lots, so the line must not name one."),
            code="lot_not_allowed",
            params={"code": item.code},
        )


@transaction.atomic
def delete_transfer_line(*, line: StockTransferLine, reason: str = "") -> None:
    """Remove a line from a draft transfer."""
    transfer = StockTransfer.objects.select_for_update().get(pk=line.transfer_id)
    _require_transfer_status(transfer, [StockTransferStatus.DRAFT], "not_a_draft")
    record_audit_event(
        action=AuditAction.UPDATED,
        target=transfer,
        branch=transfer.source_warehouse.branch,
        previous_state=snapshot(line),
        reason=reason or "transfer line removed",
        metadata={"line": line.sequence, "item": line.item.code},
    )
    line.delete()


@transaction.atomic
def replace_transfer_lines(
    *, transfer: StockTransfer, lines: Sequence[TransferLineInput]
) -> StockTransfer:
    """Replace a draft's lines wholesale — the API PATCH shape."""
    locked = StockTransfer.objects.select_for_update().get(pk=transfer.pk)
    _require_transfer_status(locked, [StockTransferStatus.DRAFT], "not_a_draft")
    locked.lines.all().delete()
    for line in lines:
        add_transfer_line(transfer=locked, line=line)
    return locked


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@transaction.atomic
def dispatch_transfer(*, transfer: StockTransfer) -> StockTransfer:
    """
    Send the goods: out of the source warehouse and into the source branch's
    in-transit stock, at exactly the value they left at.

    No gain or loss. The in-transit inbound is given the **exact** outbound
    value rather than a recomputed `quantity x cost`, so the two halves cancel
    to the dinar even when the source position was fully depleted and took its
    entire remaining book value with it (ADR-020 §3).
    """
    locked = StockTransfer.objects.select_for_update().get(pk=transfer.pk)
    _require_transfer_status(locked, [StockTransferStatus.DRAFT], "not_a_draft")
    actor = _actor()

    lines = list(
        locked.lines.select_related(
            "item",
            "item__category",
            "item__category__parent",
            "item__category__parent__parent",
            "lot",
            "package_conversion",
        ).order_by("sequence")
    )
    if not lines:
        raise ValidationError(_("An empty transfer cannot be dispatched."), code="no_lines")

    source_warehouse = locked.source_warehouse
    _validate_transfer_endpoints(
        organization=locked.organization,
        source_warehouse=source_warehouse,
        destination_warehouse=locked.destination_warehouse,
    )
    transit = ensure_in_transit_warehouse(branch=source_warehouse.branch)

    day = resolve_business_day(source_warehouse.branch, locked.effective_at)
    locked.business_date = day.business_date
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    period = _period_for(organization=locked.organization, on_date=day.business_date)

    # 3. The mapping lock, before a single account is resolved.
    lock_account_mappings_shared(locked.organization_id)

    in_transit = resolve_inventory_account(
        organization=locked.organization,
        role=INVENTORY_IN_TRANSIT,
        item=None,
        on_date=day.business_date,
    )
    require_cost_center_where_the_account_demands_one(account=in_transit.account, cost_center=None)

    # The credit side per line: the account the goods are standing in, read
    # from the balance rather than resolved, so a mapping changed since they
    # arrived cannot credit them out of an account they were never in.
    source_accounts: dict[int, Account] = {}
    for line in lines:
        standing = _standing_account(source_warehouse, line)
        if standing is None:
            standing = resolve_inventory_account(
                organization=locked.organization,
                role=INVENTORY_CONTROL,
                item=line.item,
                on_date=day.business_date,
            ).account
        source_accounts[line.pk] = standing

    # 5. Every key this event touches, canonically, before any of it moves.
    out_effects = [
        MovementInput(
            warehouse=source_warehouse,
            item=line.item,
            movement_type=MovementType.TRANSFER_OUT,
            quantity=line.base_quantity,
            effect_key=f"transfer-out:{line.line_uid}",
            lot=line.lot,
            source_conversion=line.package_conversion,
            control_account=source_accounts[line.pk],
        )
        for line in lines
    ]
    acquire_stock_key_locks(
        [
            *out_effects,
            *[
                MovementInput(
                    warehouse=transit,
                    item=line.item,
                    movement_type=MovementType.TRANSFER_IN,
                    quantity=line.base_quantity,
                    effect_key=f"transfer-in:{line.line_uid}",
                    lot=line.lot,
                    unit_cost=ZERO,
                )
                for line in lines
            ],
        ]
    )

    # The outbound values are only known once the kernel has computed them, so
    # the two halves of a dispatch are two postings against one entry: first
    # take the goods out, then read what they were worth and put exactly that
    # into transit.
    dispatched, transit_entry = _post_dispatch_effects(
        transfer=locked,
        lines=lines,
        transit=transit,
        out_effects=out_effects,
        in_transit_account=in_transit.account,
        business_date=day.business_date,
    )

    locked.transfer_number = next_document_number(
        organization=locked.organization,
        document_type=InventoryDocumentType.TRANSFER,
        year=period.fiscal_year.year,
    )
    journal = post_entry(
        organization=locked.organization,
        accounting_date=day.business_date,
        lines=_dispatch_journal_lines(
            locked, lines, in_transit=in_transit.account, source_accounts=source_accounts
        ),
        idempotency_key=f"transfer-dispatch-journal:{locked.public_id}",
        document_date=day.business_date,
        narration=locked.narration or str(_("تحويل مخزني")),
        source_document_type=TRANSFER_DISPATCH_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=DISPATCH_POSTING_RULE,
    )
    # Both halves name the journal: one economic event, one journal, and the
    # conditional control-account invariant must bite on both sides of it.
    link_journal_entry(entry=dispatched, journal=journal)
    link_journal_entry(entry=transit_entry, journal=journal)

    locked.stock_entry = dispatched
    locked.journal_entry = journal
    locked.status = StockTransferStatus.DISPATCHED
    locked.dispatched_by = actor
    locked.dispatched_at = timezone.now()
    locked.save(
        update_fields=[
            "business_date",
            "business_date_timezone",
            "business_day_start",
            "transfer_number",
            "stock_entry",
            "journal_entry",
            "status",
            "dispatched_by",
            "dispatched_at",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.POSTED,
        target=locked,
        branch=source_warehouse.branch,
        new_state=snapshot(locked),
        source_document_type=TRANSFER_DISPATCH_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "transfer_number": locked.transfer_number,
            "stock_entry": dispatched.pk,
            "journal_entry": journal.entry_number,
            "line_count": len(lines),
        },
    )
    return locked


def _post_dispatch_effects(
    *,
    transfer: StockTransfer,
    lines: list[StockTransferLine],
    transit: Warehouse,
    out_effects: list[MovementInput],
    in_transit_account: Account,
    business_date: datetime.date,
) -> tuple[StockLedgerEntry, StockLedgerEntry]:
    """
    Both halves of a dispatch, as two ledger entries of one economic event.

    The outbound goes first because its value is what the inbound must carry:
    a position emptied to zero surrenders its **entire** remaining book value,
    which is not `quantity x average` and cannot be predicted before the fact.
    Feeding that exact figure back as the in-transit value is what makes the
    pair value-neutral in every case rather than in the common one.
    """
    entry = post_stock_entry(
        organization=transfer.organization,
        effects=out_effects,
        idempotency_key=f"transfer-dispatch:{transfer.public_id}",
        effective_at=transfer.effective_at,
        business_date=business_date,
        source_document_type=TRANSFER_DISPATCH_SOURCE_TYPE,
        source_document_id=str(transfer.public_id),
        source_event=SourceEvent.POSTED,
        reference=transfer.evidence_reference,
        reason=transfer.narration or str(_("تحويل مخزني")),
    )
    outbound = {movement.effect_key: movement for movement in entry.movements.all()}

    in_effects: list[MovementInput] = []
    for line in lines:
        movement = outbound[f"transfer-out:{line.line_uid}"]
        value = abs(movement.inventory_value)
        if value <= ZERO:
            raise ValidationError(
                _("%(item)s left %(warehouse)s at no value; a transfer must carry its cost."),
                code="dispatch_value_not_positive",
                params={"item": line.item.code, "warehouse": transfer.source_warehouse.code},
            )
        unit_cost = quantize_unit_price(value / line.base_quantity)
        in_effects.append(
            MovementInput(
                warehouse=transit,
                item=line.item,
                movement_type=MovementType.TRANSFER_IN,
                quantity=line.base_quantity,
                effect_key=f"transfer-in:{line.line_uid}",
                lot=line.lot,
                unit_cost=unit_cost,
                source_conversion=line.package_conversion,
                control_account=in_transit_account,
                # The exact figure, never `quantity x unit_cost` re-derived.
                inbound_value=value,
            )
        )

    transit_entry = post_stock_entry(
        organization=transfer.organization,
        effects=in_effects,
        idempotency_key=f"transfer-dispatch-transit:{transfer.public_id}",
        effective_at=transfer.effective_at,
        business_date=business_date,
        reference=transfer.evidence_reference,
        reason=str(_("إدخال بضاعة بالطريق")),
    )
    inbound = {movement.effect_key: movement for movement in transit_entry.movements.all()}

    for line in lines:
        out_movement = outbound[f"transfer-out:{line.line_uid}"]
        in_movement = inbound[f"transfer-in:{line.line_uid}"]
        value = abs(out_movement.inventory_value)
        line.unit_cost = quantize_unit_price(value / line.base_quantity)
        line.total_value = value
        line.remaining_quantity = line.base_quantity
        line.remaining_value = value
        line.source_movement = out_movement
        line.transit_movement = in_movement
        line.source_control_account = out_movement.control_account
        line.transit_control_account = in_movement.control_account
        line.save(
            update_fields=[
                "unit_cost",
                "total_value",
                "remaining_quantity",
                "remaining_value",
                "source_movement",
                "transit_movement",
                "source_control_account",
                "transit_control_account",
                "updated_at",
            ]
        )
    return entry, transit_entry


def _dispatch_journal_lines(
    transfer: StockTransfer,
    lines: list[StockTransferLine],
    *,
    in_transit: Account,
    source_accounts: dict[int, Account],
) -> list[PostingLine]:
    """Dr Inventory In-Transit, Cr each source control account, source branch."""
    branch = transfer.source_warehouse.branch
    credits: dict[int, Decimal] = {}
    accounts: dict[int, Account] = {}
    total = ZERO
    for line in lines:
        account = source_accounts[line.pk]
        accounts[account.pk] = account
        value = line.total_value or ZERO
        credits[account.pk] = credits.get(account.pk, ZERO) + value
        total += value

    posting_lines = [PostingLine(account=in_transit, branch=branch, debit=total)]
    for account_id, amount in sorted(credits.items(), key=lambda pair: accounts[pair[0]].code):
        posting_lines.append(
            PostingLine(account=accounts[account_id], branch=branch, credit=amount)
        )
    return posting_lines


@transaction.atomic
def reverse_dispatch(*, transfer: StockTransfer, reason: str) -> StockTransfer:
    """
    Undo a dispatch, when and only when nothing downstream has happened.

    Refused while any receipt or shortage is still active against it: those
    events took their value from the dispatch, so undoing it would leave them
    referencing something that no longer happened. Reverse them first, in the
    order they were posted (§Q3).
    """
    locked = StockTransfer.objects.select_for_update().get(pk=transfer.pk)
    if locked.status == StockTransferStatus.REVERSED:
        raise ValidationError(_("This transfer is already reversed."), code="already_reversed")
    _require_transfer_status(locked, [StockTransferStatus.DISPATCHED], "dispatch_not_reversible")
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")
    actor = _actor()

    if locked.receipts.filter(status=InventoryDocumentStatus.POSTED).exists():
        raise ValidationError(
            _("Goods have been received against this transfer. Reverse the receipts first."),
            code="transfer_has_active_receipts",
        )
    if locked.shortages.filter(status=InventoryDocumentStatus.POSTED).exists():
        raise ValidationError(
            _("This transfer has been closed with a shortage. Reverse the closure first."),
            code="transfer_has_active_shortage",
        )

    lines = list(locked.lines.select_related("item", "lot").order_by("sequence"))
    for line in lines:
        if line.remaining_quantity != line.base_quantity or line.remaining_value != (
            line.total_value or ZERO
        ):
            raise ValidationError(  # pragma: no cover - unreachable while the two checks above hold
                _("Part of %(item)s has already been resolved; the dispatch cannot be undone."),
                code="transfer_partially_resolved",
                params={"item": line.item.code},
            )

    now = timezone.now()
    reversal_business_date = resolve_business_day(locked.source_warehouse.branch, now).business_date
    assert locked.stock_entry is not None  # noqa: S101 - a dispatched transfer links one
    assert locked.journal_entry is not None  # noqa: S101

    transit_entry = _transit_entry_of(locked)
    acquire_movement_key_locks(
        [
            *locked.stock_entry.movements.select_related(
                "warehouse", "warehouse__branch", "item", "lot"
            ),
            *transit_entry.movements.select_related(
                "warehouse", "warehouse__branch", "item", "lot"
            ),
        ]
    )
    # Out of transit first, then back onto the source shelf: the availability
    # check has to bite on the in-transit side, which is the side that could
    # have been emptied by a receipt posted and reversed in between.
    reversing_transit = reverse_stock_entry(
        entry=transit_entry,
        idempotency_key=f"transfer-dispatch-transit-reverse:{locked.public_id}",
        reason=reason.strip(),
        effective_at=now,
        business_date=reversal_business_date,
    )
    reversing = reverse_stock_entry(
        entry=locked.stock_entry,
        idempotency_key=f"transfer-dispatch-reverse:{locked.public_id}",
        reason=reason.strip(),
        effective_at=now,
        business_date=reversal_business_date,
    )
    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=f"transfer-dispatch-journal-reverse:{locked.public_id}",
        reason=reason.strip(),
        accounting_date=reversal_business_date,
    )
    link_journal_entry(entry=reversing, journal=reversal_journal)
    link_journal_entry(entry=reversing_transit, journal=reversal_journal)

    for line in lines:
        line.remaining_quantity = ZERO
        line.remaining_value = ZERO
        line.save(update_fields=["remaining_quantity", "remaining_value", "updated_at"])

    locked.status = StockTransferStatus.REVERSED
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
        branch=locked.source_warehouse.branch,
        new_state=snapshot(locked),
        reason=reason.strip(),
        source_document_type=TRANSFER_DISPATCH_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={"reversal_journal": reversal_journal.entry_number},
    )
    return locked


def _transit_entry_of(transfer: StockTransfer) -> StockLedgerEntry:
    """
    The in-transit half of a dispatch.

    Found by its own idempotency key rather than by a column, because it is
    not a separate economic event — it is the second half of one, split only
    because the inbound value is not knowable until the outbound is computed.
    """
    entry = StockLedgerEntry.objects.filter(
        organization=transfer.organization,
        idempotency_key=f"transfer-dispatch-transit:{transfer.public_id}",
    ).first()
    if entry is None:  # pragma: no cover - a dispatched transfer always has one
        raise ValidationError(
            _("The in-transit half of this dispatch is missing."),
            code="transfer_transit_entry_missing",
        )
    return entry


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


@transaction.atomic
def create_receipt(
    *,
    transfer: StockTransfer,
    effective_at: datetime.datetime,
    evidence_reference: str,
    narration: str = "",
) -> StockTransferReceipt:
    """Start a draft receipt against an open transfer."""
    locked = StockTransfer.objects.select_for_update().get(pk=transfer.pk)
    _require_transfer_status(
        locked,
        [StockTransferStatus.DISPATCHED, StockTransferStatus.PARTIALLY_RECEIVED],
        "transfer_not_open",
    )
    if not evidence_reference.strip():
        raise ValidationError(
            _("A transfer receipt needs its evidence reference."),
            code="evidence_reference_required",
        )

    moment = _aware(effective_at)
    receipt = StockTransferReceipt(
        transfer=locked,
        effective_at=moment,
        business_date=resolve_business_day(
            locked.destination_warehouse.branch, moment
        ).business_date,
        evidence_reference=evidence_reference.strip(),
        narration=narration.strip(),
        created_by=get_actor(),
    )
    receipt.full_clean()
    receipt.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=receipt,
        branch=locked.destination_warehouse.branch,
        new_state=snapshot(receipt),
    )
    return receipt


@transaction.atomic
def update_receipt(
    *,
    receipt: StockTransferReceipt,
    effective_at: datetime.datetime | None = None,
    evidence_reference: str | None = None,
    narration: str | None = None,
) -> StockTransferReceipt:
    """
    Amend a draft receipt's header.

    Returning to draft releases the snapshots, exactly as Task 1.4 allows:
    a draft has committed to nothing, so recalculating its dates is a
    deliberate act rather than a silent restatement.
    """
    locked = StockTransferReceipt.objects.select_for_update().get(pk=receipt.pk)
    _require_event_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    before = snapshot(locked)

    if effective_at is not None:
        locked.effective_at = _aware(effective_at)
        locked.business_date = resolve_business_day(
            locked.transfer.destination_warehouse.branch, locked.effective_at
        ).business_date
        locked.business_date_timezone = ""
        locked.business_day_start = None
        locked.source_business_date = None
        locked.source_business_date_timezone = ""
        locked.source_business_day_start = None
    if evidence_reference is not None:
        if not evidence_reference.strip():
            raise ValidationError(
                _("A transfer receipt needs its evidence reference."),
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
        branch=locked.transfer.destination_warehouse.branch,
        previous_state=before,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def delete_receipt(*, receipt: StockTransferReceipt, reason: str = "") -> None:
    """Discard a draft receipt."""
    locked = StockTransferReceipt.objects.select_for_update().get(pk=receipt.pk)
    _require_event_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.transfer.destination_warehouse.branch,
        previous_state=snapshot(locked),
        reason=reason,
    )
    locked.lines.all().delete()
    locked.delete()


@transaction.atomic
def add_receipt_line(
    *, receipt: StockTransferReceipt, line: ReceiptLineInput
) -> StockTransferReceiptLine:
    """Record how much of one transfer line has arrived."""
    locked = StockTransferReceipt.objects.select_for_update().get(pk=receipt.pk)
    _require_event_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")

    target = line.transfer_line
    if target.transfer_id != locked.transfer_id:
        # The line names a transfer this receipt is not against. Refused as
        # out of scope rather than 404'd here: scope resolution happens a layer
        # up, and this is the invariant, not the authorization.
        raise ValidationError(
            _("That line belongs to another transfer."), code="transfer_line_mismatch"
        )
    if target.remaining_quantity <= ZERO:
        raise ValidationError(
            _("%(item)s has nothing left in transit."),
            code="nothing_left_in_transit",
            params={"item": target.item.code},
        )

    conversion = target.package_conversion
    if line.entered_package_quantity is not None and conversion is None:
        raise ValidationError(
            _("This line was not dispatched in packages, so it is received in base units."),
            code="package_conversion_required",
        )
    base_quantity = _derive_quantity(
        conversion=conversion if line.entered_package_quantity is not None else None,
        entered_package_quantity=line.entered_package_quantity,
        measured_base_quantity=line.measured_base_quantity,
        base_quantity=line.base_quantity,
        item=target.item,
    )
    if base_quantity <= ZERO:
        raise ValidationError(
            _("The quantity must be greater than zero."), code="quantity_not_positive"
        )
    if base_quantity > target.remaining_quantity:
        raise ValidationError(
            _("Only %(remaining)s of %(item)s is still in transit."),
            code="receipt_exceeds_remaining",
            params={"remaining": str(target.remaining_quantity), "item": target.item.code},
        )

    last = locked.lines.order_by("-sequence").first()
    stored = StockTransferReceiptLine(
        receipt=locked,
        sequence=(last.sequence + 1) if last is not None else 1,
        transfer_line=target,
        package_conversion=conversion if line.entered_package_quantity is not None else None,
        entered_package_quantity=line.entered_package_quantity,
        measured_base_quantity=line.measured_base_quantity,
        base_quantity=base_quantity,
    )
    stored.full_clean()
    stored.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        branch=locked.transfer.destination_warehouse.branch,
        new_state=snapshot(stored),
        metadata={"line": stored.sequence, "item": target.item.code},
        reason="receipt line added",
    )
    return stored


@transaction.atomic
def replace_receipt_lines(
    *, receipt: StockTransferReceipt, lines: Sequence[ReceiptLineInput]
) -> StockTransferReceipt:
    """Replace a draft receipt's lines wholesale."""
    locked = StockTransferReceipt.objects.select_for_update().get(pk=receipt.pk)
    _require_event_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    locked.lines.all().delete()
    for line in lines:
        add_receipt_line(receipt=locked, line=line)
    return locked


@transaction.atomic
def post_receipt(*, receipt: StockTransferReceipt) -> StockTransferReceipt:
    """
    Take delivery: out of the source branch's in-transit stock and onto the
    destination warehouse's shelf, at the value this transfer allocated.

    Both branches' accounting periods are validated, each against **its own**
    business date, and if either is closed the whole receipt rolls back —
    stock effects, journals, numbering and all (§H).
    """
    # 1. The parent, before the child: every other path takes them in this
    # order, and a receipt reads the transfer's remaining balances.
    transfer = StockTransfer.objects.select_for_update().get(pk=receipt.transfer_id)
    # 2. The receipt row.
    locked = StockTransferReceipt.objects.select_for_update().get(pk=receipt.pk)
    if locked.status == InventoryDocumentStatus.POSTED:
        raise ValidationError(_("This receipt is already posted."), code="already_posted")
    _require_event_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    _require_transfer_status(
        transfer,
        [StockTransferStatus.DISPATCHED, StockTransferStatus.PARTIALLY_RECEIVED],
        "transfer_not_open",
    )
    actor = _actor()

    lines = list(
        locked.lines.select_related(
            "transfer_line",
            "transfer_line__item",
            "transfer_line__item__category",
            "transfer_line__item__category__parent",
            "transfer_line__item__category__parent__parent",
            "transfer_line__lot",
            "package_conversion",
        ).order_by("sequence")
    )
    if not lines:
        raise ValidationError(_("An empty receipt cannot be posted."), code="no_lines")

    source_branch = transfer.source_warehouse.branch
    destination = transfer.destination_warehouse
    transit = ensure_in_transit_warehouse(branch=source_branch)

    destination_day = resolve_business_day(destination.branch, locked.effective_at)
    source_day = resolve_business_day(source_branch, locked.effective_at)
    destination_period = _period_for(
        organization=transfer.organization, on_date=destination_day.business_date
    )
    _period_for(organization=transfer.organization, on_date=source_day.business_date)

    # 3. The mapping lock.
    lock_account_mappings_shared(transfer.organization_id)

    # 4. The transfer lines, by primary key, before their remaining balance is
    # read. Two concurrent receipts against one transfer contend here, so
    # neither can allocate against a basis the other has already spent.
    target_ids = sorted({line.transfer_line_id for line in lines})
    targets = {
        target.pk: target
        for target in StockTransferLine.objects.select_for_update(of=("self",))
        .filter(pk__in=target_ids)
        .order_by("pk")
        .select_related("item", "lot")
    }

    allocations = _allocate_receipt(lines, targets)
    accounts = _resolve_receipt_accounts(
        transfer=transfer,
        allocations=allocations,
        transit=transit,
        destination_date=destination_day.business_date,
        source_date=source_day.business_date,
    )

    # 5. Every key, canonically, across both sides — see the module docstring.
    release_effects = [
        MovementInput(
            warehouse=transit,
            item=allocation.line.item,
            movement_type=MovementType.TRANSFER_OUT,
            quantity=allocation.quantity,
            effect_key=f"transfer-receipt-release:{line.line_uid}",
            lot=allocation.line.lot,
            control_account=accounts.transit[allocation.line.pk],
            # The share this transfer allocated, never the pooled average of
            # every transfer of this item that happens to be on the road.
            outbound_value=allocation.value,
        )
        for line, allocation in zip(lines, allocations, strict=True)
    ]
    arrival_effects = [
        MovementInput(
            warehouse=destination,
            item=allocation.line.item,
            movement_type=MovementType.TRANSFER_IN,
            quantity=allocation.quantity,
            effect_key=f"transfer-receipt-arrival:{line.line_uid}",
            lot=allocation.line.lot,
            unit_cost=allocation.unit_cost,
            source_conversion=allocation.line.package_conversion,
            control_account=accounts.destination[allocation.line.pk],
            inbound_value=allocation.value,
        )
        for line, allocation in zip(lines, allocations, strict=True)
    ]
    acquire_stock_key_locks([*release_effects, *arrival_effects])

    release_entry = post_stock_entry(
        organization=transfer.organization,
        effects=release_effects,
        idempotency_key=f"transfer-receipt-release:{locked.public_id}",
        effective_at=locked.effective_at,
        business_date=source_day.business_date,
        source_document_type=TRANSFER_RECEIPT_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        reference=locked.evidence_reference,
        reason=str(_("إخراج من بضاعة بالطريق")),
    )
    arrival_entry = post_stock_entry(
        organization=transfer.organization,
        effects=arrival_effects,
        idempotency_key=f"transfer-receipt-arrival:{locked.public_id}",
        effective_at=locked.effective_at,
        business_date=destination_day.business_date,
        source_document_type=TRANSFER_RECEIPT_DESTINATION_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        reference=locked.evidence_reference,
        reason=locked.narration or str(_("استلام تحويل")),
    )

    locked.receipt_number = next_document_number(
        organization=transfer.organization,
        document_type=InventoryDocumentType.TRANSFER_RECEIPT,
        year=destination_period.fiscal_year.year,
    )
    journals = _post_receipt_journals(
        transfer=transfer,
        receipt=locked,
        allocations=allocations,
        accounts=accounts,
        source_date=source_day.business_date,
        destination_date=destination_day.business_date,
    )
    link_journal_entry(entry=release_entry, journal=journals.source)
    link_journal_entry(entry=arrival_entry, journal=journals.destination)

    _link_receipt_lines(
        lines,
        allocations=allocations,
        accounts=accounts,
        release=release_entry,
        arrival=arrival_entry,
    )
    for allocation in allocations:
        target = allocation.line
        target.remaining_quantity = quantize_quantity(
            target.remaining_quantity - allocation.quantity
        )
        target.remaining_value = quantize_money(target.remaining_value - allocation.value)
        target.save(update_fields=["remaining_quantity", "remaining_value", "updated_at"])

    locked.business_date = destination_day.business_date
    locked.business_date_timezone = destination_day.timezone_name
    locked.business_day_start = destination_day.day_start
    locked.source_business_date = source_day.business_date
    locked.source_business_date_timezone = source_day.timezone_name
    locked.source_business_day_start = source_day.day_start
    locked.source_stock_entry = release_entry
    locked.destination_stock_entry = arrival_entry
    locked.source_journal_entry = journals.source
    locked.destination_journal_entry = journals.destination
    locked.status = InventoryDocumentStatus.POSTED
    locked.received_by = actor
    locked.posted_at = timezone.now()
    locked.save(
        update_fields=[
            "business_date",
            "business_date_timezone",
            "business_day_start",
            "source_business_date",
            "source_business_date_timezone",
            "source_business_day_start",
            "receipt_number",
            "source_stock_entry",
            "destination_stock_entry",
            "source_journal_entry",
            "destination_journal_entry",
            "status",
            "received_by",
            "posted_at",
            "updated_at",
        ]
    )
    recompute_transfer_status(transfer)
    record_audit_event(
        action=AuditAction.POSTED,
        target=locked,
        branch=destination.branch,
        new_state=snapshot(locked),
        source_document_type=TRANSFER_RECEIPT_DESTINATION_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "receipt_number": locked.receipt_number,
            "cross_branch": transfer.is_cross_branch,
            "source_business_date": source_day.business_date.isoformat(),
            "destination_business_date": destination_day.business_date.isoformat(),
            "line_count": len(lines),
        },
    )
    return locked


def _allocate_receipt(
    lines: list[StockTransferReceiptLine], targets: dict[int, StockTransferLine]
) -> list[_Allocation]:
    """
    Each received quantity's exact share of its transfer line's remaining value.

    Re-checked against the **locked** rows, not against whatever the draft saw:
    a concurrent receipt may have consumed part of the same basis since this
    one was prepared, and the draft's arithmetic would then over-allocate.
    """
    allocations: list[_Allocation] = []
    for line in lines:
        target = targets[line.transfer_line_id]
        if line.base_quantity > target.remaining_quantity:
            raise ValidationError(
                _("Only %(remaining)s of %(item)s is still in transit."),
                code="receipt_exceeds_remaining",
                params={"remaining": str(target.remaining_quantity), "item": target.item.code},
            )
        value = allocate(
            remaining_quantity=target.remaining_quantity,
            remaining_value=target.remaining_value,
            taken_quantity=line.base_quantity,
        )
        if value <= ZERO:
            raise ValidationError(
                _("The allocated value of %(item)s rounds to zero."),
                code="allocated_value_not_positive",
                params={"item": target.item.code},
            )
        allocations.append(
            _Allocation(
                line=target,
                quantity=line.base_quantity,
                value=value,
                unit_cost=quantize_unit_price(value / line.base_quantity),
            )
        )
    return allocations


@dataclass
class _ReceiptAccounts:
    """Everything a receipt's journals need, resolved before any stock moves."""

    #: transfer line pk -> the in-transit account that line's value is standing
    #: in, and the destination account it is going into.
    transit: dict[int, Account]
    destination: dict[int, Account]
    clearing: Account | None


def _transit_account_for(
    *,
    organization: Organization,
    transit: Warehouse,
    line: StockTransferLine,
    on_date: datetime.date,
) -> Account:
    """
    The account this line's in-transit value is actually standing in.

    Per line rather than once for the whole event. `INVENTORY_IN_TRANSIT` takes
    an organization default only, so in practice every item shares one account
    — but a mapping closed and re-opened between two dispatches leaves goods
    dispatched under the old one standing in it, and crediting them out of
    today's account would move value the source journal never put there.
    """
    return (
        _standing_account(transit, line)
        or resolve_inventory_account(
            organization=organization,
            role=INVENTORY_IN_TRANSIT,
            item=None,
            on_date=on_date,
        ).account
    )


def _resolve_receipt_accounts(
    *,
    transfer: StockTransfer,
    allocations: list[_Allocation],
    transit: Warehouse,
    destination_date: datetime.date,
    source_date: datetime.date,
) -> _ReceiptAccounts:
    """
    Resolve every account first, so a missing mapping costs nothing.

    §M is explicit that an unmapped role must fail with `account_role_unmapped`
    and roll back every stock and document effect. Resolving before anything
    moves makes that true by construction rather than by the transaction
    happening to unwind correctly.
    """
    transit_accounts: dict[int, Account] = {}
    destination_accounts: dict[int, Account] = {}
    for allocation in allocations:
        transit_accounts[allocation.line.pk] = _transit_account_for(
            organization=transfer.organization,
            transit=transit,
            line=allocation.line,
            on_date=source_date,
        )
        standing = _standing_account(transfer.destination_warehouse, allocation.line)
        if standing is None:
            standing = resolve_inventory_account(
                organization=transfer.organization,
                role=INVENTORY_CONTROL,
                item=allocation.line.item,
                on_date=destination_date,
            ).account
        require_cost_center_where_the_account_demands_one(account=standing, cost_center=None)
        destination_accounts[allocation.line.pk] = standing

    clearing: Account | None = None
    if transfer.is_cross_branch:
        clearing = resolve_inventory_account(
            organization=transfer.organization,
            role=INTER_BRANCH_CLEARING,
            item=None,
            on_date=source_date,
        ).account
        require_cost_center_where_the_account_demands_one(account=clearing, cost_center=None)
    return _ReceiptAccounts(
        transit=transit_accounts, destination=destination_accounts, clearing=clearing
    )


def _post_receipt_journals(
    *,
    transfer: StockTransfer,
    receipt: StockTransferReceipt,
    allocations: list[_Allocation],
    accounts: _ReceiptAccounts,
    source_date: datetime.date,
    destination_date: datetime.date,
) -> _Journals:
    """
    One journal when both ends are in one branch; two when they are not.

    The cross-branch pair uses the **same** value on both sides and is written
    in one transaction, so the organization's clearing account nets to zero for
    the complete event while each branch's own trial balance stays balanced.
    """
    total = sum((allocation.value for allocation in allocations), ZERO)
    destination_branch = transfer.destination_warehouse.branch
    source_branch = transfer.source_warehouse.branch

    by_id: dict[int, Account] = {}
    debits: dict[int, Decimal] = {}
    transit_credits: dict[int, Decimal] = {}
    for allocation in allocations:
        account = accounts.destination[allocation.line.pk]
        by_id[account.pk] = account
        debits[account.pk] = debits.get(account.pk, ZERO) + allocation.value
        transit_account = accounts.transit[allocation.line.pk]
        by_id[transit_account.pk] = transit_account
        transit_credits[transit_account.pk] = (
            transit_credits.get(transit_account.pk, ZERO) + allocation.value
        )
    destination_debits = [
        PostingLine(account=by_id[account_id], branch=destination_branch, debit=amount)
        for account_id, amount in sorted(debits.items(), key=lambda pair: by_id[pair[0]].code)
    ]

    def transit_credit_lines(branch: Branch) -> list[PostingLine]:
        return [
            PostingLine(account=by_id[account_id], branch=branch, credit=amount)
            for account_id, amount in sorted(
                transit_credits.items(), key=lambda pair: by_id[pair[0]].code
            )
        ]

    if not transfer.is_cross_branch:
        journal = post_entry(
            organization=transfer.organization,
            accounting_date=destination_date,
            lines=[
                *destination_debits,
                *transit_credit_lines(destination_branch),
            ],
            idempotency_key=f"transfer-receipt-journal:{receipt.public_id}",
            document_date=destination_date,
            narration=receipt.narration or str(_("استلام تحويل")),
            source_document_type=TRANSFER_RECEIPT_DESTINATION_TYPE,
            source_document_id=str(receipt.public_id),
            source_event=SourceEvent.POSTED,
            posting_rule_version=RECEIPT_POSTING_RULE,
        )
        return _Journals(source=journal, destination=journal)

    assert accounts.clearing is not None  # noqa: S101 - resolved for a cross-branch transfer
    source_journal = post_entry(
        organization=transfer.organization,
        accounting_date=source_date,
        lines=[
            PostingLine(account=accounts.clearing, branch=source_branch, debit=total),
            *transit_credit_lines(source_branch),
        ],
        idempotency_key=f"transfer-receipt-source-journal:{receipt.public_id}",
        document_date=source_date,
        narration=str(_("تسليم تحويل بين الفروع")),
        source_document_type=TRANSFER_RECEIPT_SOURCE_TYPE,
        source_document_id=str(receipt.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=RECEIPT_POSTING_RULE,
    )
    destination_journal = post_entry(
        organization=transfer.organization,
        accounting_date=destination_date,
        lines=[
            *destination_debits,
            PostingLine(account=accounts.clearing, branch=destination_branch, credit=total),
        ],
        idempotency_key=f"transfer-receipt-destination-journal:{receipt.public_id}",
        document_date=destination_date,
        narration=receipt.narration or str(_("استلام تحويل بين الفروع")),
        source_document_type=TRANSFER_RECEIPT_DESTINATION_TYPE,
        source_document_id=str(receipt.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=RECEIPT_POSTING_RULE,
    )
    return _Journals(source=source_journal, destination=destination_journal)


def _link_receipt_lines(
    lines: list[StockTransferReceiptLine],
    *,
    allocations: list[_Allocation],
    accounts: _ReceiptAccounts,
    release: StockLedgerEntry,
    arrival: StockLedgerEntry,
) -> None:
    """Write the immutable traceability, while the lines are still unfrozen."""
    released = {movement.effect_key: movement for movement in release.movements.all()}
    arrived = {movement.effect_key: movement for movement in arrival.movements.all()}
    for line, allocation in zip(lines, allocations, strict=True):
        line.allocated_value = allocation.value
        line.unit_cost = allocation.unit_cost
        line.transit_movement = released[f"transfer-receipt-release:{line.line_uid}"]
        line.destination_movement = arrived[f"transfer-receipt-arrival:{line.line_uid}"]
        line.destination_control_account = accounts.destination[allocation.line.pk]
        line.save(
            update_fields=[
                "allocated_value",
                "unit_cost",
                "transit_movement",
                "destination_movement",
                "destination_control_account",
                "updated_at",
            ]
        )


@transaction.atomic
def reverse_receipt(*, receipt: StockTransferReceipt, reason: str) -> StockTransferReceipt:
    """
    Undo one arrival: the goods go back into transit at exactly the value they
    came out at, and the transfer reopens for that quantity.

    Refused when the received goods have already been consumed at the
    destination — the availability check applies to a reversal exactly as it
    does to an issue, or "reverse the receipt" would become the standard way to
    drive a balance negative (§Q1).
    """
    transfer = StockTransfer.objects.select_for_update().get(pk=receipt.transfer_id)
    locked = StockTransferReceipt.objects.select_for_update().get(pk=receipt.pk)
    if locked.status == InventoryDocumentStatus.REVERSED:
        raise ValidationError(_("This receipt is already reversed."), code="already_reversed")
    _require_event_status(locked, InventoryDocumentStatus.POSTED, "not_posted")
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")
    actor = _actor()

    lines = list(locked.lines.select_related("transfer_line", "transfer_line__item"))
    target_ids = sorted({line.transfer_line_id for line in lines})
    targets = {
        target.pk: target
        for target in StockTransferLine.objects.select_for_update()
        .filter(pk__in=target_ids)
        .order_by("pk")
    }

    now = timezone.now()
    source_branch = transfer.source_warehouse.branch
    source_date = resolve_business_day(source_branch, now).business_date
    destination_date = resolve_business_day(
        transfer.destination_warehouse.branch, now
    ).business_date

    assert locked.source_stock_entry is not None  # noqa: S101 - POSTED links both
    assert locked.destination_stock_entry is not None  # noqa: S101
    acquire_movement_key_locks(
        [
            *locked.source_stock_entry.movements.select_related(
                "warehouse", "warehouse__branch", "item", "lot"
            ),
            *locked.destination_stock_entry.movements.select_related(
                "warehouse", "warehouse__branch", "item", "lot"
            ),
        ]
    )
    # The destination first: that is the side that can legitimately refuse,
    # and failing before anything has been put back into transit keeps the
    # error about the real problem.
    reversing_arrival = reverse_stock_entry(
        entry=locked.destination_stock_entry,
        idempotency_key=f"transfer-receipt-arrival-reverse:{locked.public_id}",
        reason=reason.strip(),
        effective_at=now,
        business_date=destination_date,
    )
    reversing_release = reverse_stock_entry(
        entry=locked.source_stock_entry,
        idempotency_key=f"transfer-receipt-release-reverse:{locked.public_id}",
        reason=reason.strip(),
        effective_at=now,
        business_date=source_date,
    )

    assert locked.destination_journal_entry is not None  # noqa: S101 - POSTED links both
    assert locked.source_journal_entry is not None  # noqa: S101
    destination_reversal = reverse_entry(
        entry=locked.destination_journal_entry,
        idempotency_key=f"transfer-receipt-destination-journal-reverse:{locked.public_id}",
        reason=reason.strip(),
        accounting_date=destination_date,
    )
    link_journal_entry(entry=reversing_arrival, journal=destination_reversal)
    if locked.source_journal_entry_id == locked.destination_journal_entry_id:
        source_reversal = destination_reversal
    else:
        source_reversal = reverse_entry(
            entry=locked.source_journal_entry,
            idempotency_key=f"transfer-receipt-source-journal-reverse:{locked.public_id}",
            reason=reason.strip(),
            accounting_date=source_date,
        )
    link_journal_entry(entry=reversing_release, journal=source_reversal)

    for line in lines:
        target = targets[line.transfer_line_id]
        target.remaining_quantity = quantize_quantity(
            target.remaining_quantity + line.base_quantity
        )
        target.remaining_value = quantize_money(
            target.remaining_value + (line.allocated_value or ZERO)
        )
        target.save(update_fields=["remaining_quantity", "remaining_value", "updated_at"])

    locked.status = InventoryDocumentStatus.REVERSED
    locked.reversed_by = actor
    locked.reversed_at = now
    locked.reversal_reason = reason.strip()
    locked.source_reversal_journal_entry = source_reversal
    locked.destination_reversal_journal_entry = destination_reversal
    locked.save(
        update_fields=[
            "status",
            "reversed_by",
            "reversed_at",
            "reversal_reason",
            "source_reversal_journal_entry",
            "destination_reversal_journal_entry",
            "updated_at",
        ]
    )
    recompute_transfer_status(transfer)
    record_audit_event(
        action=AuditAction.REVERSED,
        target=locked,
        branch=transfer.destination_warehouse.branch,
        new_state=snapshot(locked),
        reason=reason.strip(),
        source_document_type=TRANSFER_RECEIPT_DESTINATION_TYPE,
        source_document_id=str(locked.public_id),
        metadata={"destination_reversal_journal": destination_reversal.entry_number},
    )
    return locked


# ---------------------------------------------------------------------------
# Shortage
# ---------------------------------------------------------------------------


@transaction.atomic
def create_shortage(
    *,
    transfer: StockTransfer,
    effective_at: datetime.datetime,
    reason: str,
    evidence_reference: str,
    cost_center: CostCenter,
) -> StockTransferShortage:
    """Start a draft closure for everything this transfer will not deliver."""
    locked = StockTransfer.objects.select_for_update().get(pk=transfer.pk)
    _require_transfer_status(
        locked,
        [StockTransferStatus.DISPATCHED, StockTransferStatus.PARTIALLY_RECEIVED],
        "transfer_not_open",
    )
    if not reason.strip():
        raise ValidationError(
            _("A shortage closure needs a reason."), code="shortage_reason_required"
        )
    if not evidence_reference.strip():
        raise ValidationError(
            _("A shortage closure needs its evidence reference."),
            code="evidence_reference_required",
        )
    if cost_center.organization_id != locked.organization_id:
        raise ValidationError(
            _("Cost center %(code)s belongs to another organization."),
            code="cost_center_organization_mismatch",
            params={"code": cost_center.code},
        )
    if not cost_center.is_active:
        raise ValidationError(
            _("Cost center %(code)s is archived."),
            code="cost_center_inactive",
            params={"code": cost_center.code},
        )

    moment = _aware(effective_at)
    shortage = StockTransferShortage(
        transfer=locked,
        effective_at=moment,
        business_date=resolve_business_day(locked.source_warehouse.branch, moment).business_date,
        reason=reason.strip(),
        evidence_reference=evidence_reference.strip(),
        cost_center=cost_center,
        created_by=get_actor(),
    )
    shortage.full_clean()
    shortage.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=shortage,
        branch=locked.source_warehouse.branch,
        new_state=snapshot(shortage),
    )
    return shortage


@transaction.atomic
def delete_shortage(*, shortage: StockTransferShortage, reason: str = "") -> None:
    """Discard a draft closure."""
    locked = StockTransferShortage.objects.select_for_update().get(pk=shortage.pk)
    _require_event_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    record_audit_event(
        action=AuditAction.DELETED,
        target=locked,
        branch=locked.transfer.source_warehouse.branch,
        previous_state=snapshot(locked),
        reason=reason,
    )
    locked.lines.all().delete()
    locked.delete()


@transaction.atomic
def post_shortage(*, shortage: StockTransferShortage) -> StockTransferShortage:
    """
    Write off everything still standing in transit against this transfer.

    A closure takes the **entire** remainder, and its lines are generated here
    rather than entered: a partial write-off leaving an unexplained open
    residual is exactly the state this document exists to end, and offering it
    as an option would make it reachable by accident.
    """
    transfer = StockTransfer.objects.select_for_update().get(pk=shortage.transfer_id)
    locked = StockTransferShortage.objects.select_for_update().get(pk=shortage.pk)
    if locked.status == InventoryDocumentStatus.POSTED:
        raise ValidationError(_("This closure is already posted."), code="already_posted")
    _require_event_status(locked, InventoryDocumentStatus.DRAFT, "not_a_draft")
    _require_transfer_status(
        transfer,
        [StockTransferStatus.DISPATCHED, StockTransferStatus.PARTIALLY_RECEIVED],
        "transfer_not_open",
    )
    actor = _actor()

    source_branch = transfer.source_warehouse.branch
    transit = ensure_in_transit_warehouse(branch=source_branch)
    day = resolve_business_day(source_branch, locked.effective_at)
    period = _period_for(organization=transfer.organization, on_date=day.business_date)

    lock_account_mappings_shared(transfer.organization_id)

    open_lines = list(
        StockTransferLine.objects.select_for_update(of=("self",))
        .filter(transfer=transfer, remaining_quantity__gt=ZERO)
        .order_by("pk")
        .select_related("item", "lot")
    )
    if not open_lines:
        raise ValidationError(
            _("Nothing is left in transit against this transfer."), code="nothing_to_close"
        )

    loss = resolve_inventory_account(
        organization=transfer.organization,
        role=INVENTORY_SHORTAGE_LOSS,
        item=None,
        on_date=day.business_date,
    )
    require_cost_center_where_the_account_demands_one(
        account=loss.account, cost_center=locked.cost_center
    )
    transit_accounts = {
        line.pk: _transit_account_for(
            organization=transfer.organization,
            transit=transit,
            line=line,
            on_date=day.business_date,
        )
        for line in open_lines
    }

    stored_lines: list[StockTransferShortageLine] = []
    for sequence, line in enumerate(open_lines, start=1):
        stored = StockTransferShortageLine(
            shortage=locked,
            sequence=sequence,
            transfer_line=line,
            base_quantity=line.remaining_quantity,
        )
        stored.full_clean()
        stored.save()
        stored_lines.append(stored)

    effects = [
        MovementInput(
            warehouse=transit,
            item=stored.transfer_line.item,
            movement_type=MovementType.TRANSFER_SHORTAGE,
            quantity=stored.base_quantity,
            effect_key=f"transfer-shortage:{stored.line_uid}",
            lot=stored.transfer_line.lot,
            control_account=transit_accounts[stored.transfer_line_id],
            # The whole remaining value of the line, exactly — this is the
            # last event against it, so anything less would strand a residual.
            outbound_value=stored.transfer_line.remaining_value,
        )
        for stored in stored_lines
    ]
    acquire_stock_key_locks(effects)
    entry = post_stock_entry(
        organization=transfer.organization,
        effects=effects,
        idempotency_key=f"transfer-shortage:{locked.public_id}",
        effective_at=locked.effective_at,
        business_date=day.business_date,
        source_document_type=TRANSFER_SHORTAGE_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        reference=locked.evidence_reference,
        reason=locked.reason,
    )
    movements = {movement.effect_key: movement for movement in entry.movements.all()}

    total = ZERO
    credits: dict[int, Decimal] = {}
    for stored in stored_lines:
        value = stored.transfer_line.remaining_value
        stored.allocated_value = value
        stored.unit_cost = quantize_unit_price(value / stored.base_quantity)
        stored.transit_movement = movements[f"transfer-shortage:{stored.line_uid}"]
        total += value
        account = transit_accounts[stored.transfer_line_id]
        credits[account.pk] = credits.get(account.pk, ZERO) + value
    by_id = {account.pk: account for account in transit_accounts.values()}

    locked.shortage_number = next_document_number(
        organization=transfer.organization,
        document_type=InventoryDocumentType.TRANSFER_SHORTAGE,
        year=period.fiscal_year.year,
    )
    journal = post_entry(
        organization=transfer.organization,
        accounting_date=day.business_date,
        lines=[
            PostingLine(
                account=loss.account,
                branch=source_branch,
                cost_center=locked.cost_center,
                debit=total,
            ),
            *[
                PostingLine(account=by_id[account_id], branch=source_branch, credit=amount)
                for account_id, amount in sorted(
                    credits.items(), key=lambda pair: by_id[pair[0]].code
                )
            ],
        ],
        idempotency_key=f"transfer-shortage-journal:{locked.public_id}",
        document_date=day.business_date,
        narration=locked.reason,
        source_document_type=TRANSFER_SHORTAGE_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        source_event=SourceEvent.POSTED,
        posting_rule_version=SHORTAGE_POSTING_RULE,
    )
    link_journal_entry(entry=entry, journal=journal)

    debit_line = journal.lines.filter(debit__gt=ZERO).first()
    for stored in stored_lines:
        stored.journal_line = debit_line
        stored.save(
            update_fields=[
                "allocated_value",
                "unit_cost",
                "transit_movement",
                "journal_line",
                "updated_at",
            ]
        )
        line = stored.transfer_line
        line.remaining_quantity = ZERO
        line.remaining_value = ZERO
        line.save(update_fields=["remaining_quantity", "remaining_value", "updated_at"])

    locked.business_date = day.business_date
    locked.business_date_timezone = day.timezone_name
    locked.business_day_start = day.day_start
    locked.stock_entry = entry
    locked.journal_entry = journal
    locked.status = InventoryDocumentStatus.POSTED
    locked.closed_by = actor
    locked.posted_at = timezone.now()
    locked.save(
        update_fields=[
            "business_date",
            "business_date_timezone",
            "business_day_start",
            "shortage_number",
            "stock_entry",
            "journal_entry",
            "status",
            "closed_by",
            "posted_at",
            "updated_at",
        ]
    )
    recompute_transfer_status(transfer)
    record_audit_event(
        action=AuditAction.POSTED,
        target=locked,
        branch=source_branch,
        new_state=snapshot(locked),
        reason=locked.reason,
        source_document_type=TRANSFER_SHORTAGE_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={
            "shortage_number": locked.shortage_number,
            "cost_center": locked.cost_center.code,
            "value": str(total),
            "line_count": len(stored_lines),
        },
    )
    return locked


@transaction.atomic
def reverse_shortage(*, shortage: StockTransferShortage, reason: str) -> StockTransferShortage:
    """Put the written-off goods back into transit and reopen the transfer."""
    transfer = StockTransfer.objects.select_for_update().get(pk=shortage.transfer_id)
    locked = StockTransferShortage.objects.select_for_update().get(pk=shortage.pk)
    if locked.status == InventoryDocumentStatus.REVERSED:
        raise ValidationError(_("This closure is already reversed."), code="already_reversed")
    _require_event_status(locked, InventoryDocumentStatus.POSTED, "not_posted")
    if not reason.strip():
        raise ValidationError(_("A reversal needs a reason."), code="reason_required")
    actor = _actor()

    lines = list(locked.lines.select_related("transfer_line"))
    targets = {
        target.pk: target
        for target in StockTransferLine.objects.select_for_update()
        .filter(pk__in=sorted({line.transfer_line_id for line in lines}))
        .order_by("pk")
    }

    now = timezone.now()
    source_branch = transfer.source_warehouse.branch
    business_date = resolve_business_day(source_branch, now).business_date

    assert locked.stock_entry is not None  # noqa: S101 - POSTED links one
    assert locked.journal_entry is not None  # noqa: S101
    acquire_movement_key_locks(
        list(
            locked.stock_entry.movements.select_related(
                "warehouse", "warehouse__branch", "item", "lot"
            )
        )
    )
    reversing = reverse_stock_entry(
        entry=locked.stock_entry,
        idempotency_key=f"transfer-shortage-reverse:{locked.public_id}",
        reason=reason.strip(),
        effective_at=now,
        business_date=business_date,
    )
    reversal_journal = reverse_entry(
        entry=locked.journal_entry,
        idempotency_key=f"transfer-shortage-journal-reverse:{locked.public_id}",
        reason=reason.strip(),
        accounting_date=business_date,
    )
    link_journal_entry(entry=reversing, journal=reversal_journal)

    for line in lines:
        target = targets[line.transfer_line_id]
        target.remaining_quantity = quantize_quantity(
            target.remaining_quantity + line.base_quantity
        )
        target.remaining_value = quantize_money(
            target.remaining_value + (line.allocated_value or ZERO)
        )
        target.save(update_fields=["remaining_quantity", "remaining_value", "updated_at"])

    locked.status = InventoryDocumentStatus.REVERSED
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
    recompute_transfer_status(transfer)
    record_audit_event(
        action=AuditAction.REVERSED,
        target=locked,
        branch=source_branch,
        new_state=snapshot(locked),
        reason=reason.strip(),
        source_document_type=TRANSFER_SHORTAGE_SOURCE_TYPE,
        source_document_id=str(locked.public_id),
        metadata={"reversal_journal": reversal_journal.entry_number},
    )
    return locked


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def recompute_transfer_status(transfer: StockTransfer) -> StockTransfer:
    """
    Derive the aggregate's status from what has actually been posted.

    Never set by a caller. A status somebody can write is a status that can
    disagree with the events underneath it, and "how much of this transfer has
    arrived" is a question only those events can answer. Called after every
    posted child event and after every reversal of one, inside the same
    transaction.
    """
    if transfer.status in (StockTransferStatus.DRAFT, StockTransferStatus.REVERSED):
        return transfer

    remaining = sum((line.remaining_quantity for line in transfer.lines.all()), ZERO)
    has_receipt = transfer.receipts.filter(status=InventoryDocumentStatus.POSTED).exists()
    has_shortage = transfer.shortages.filter(status=InventoryDocumentStatus.POSTED).exists()

    if has_shortage:
        status = StockTransferStatus.CLOSED_WITH_SHORTAGE
    elif remaining == ZERO:
        status = StockTransferStatus.COMPLETED
    elif has_receipt:
        status = StockTransferStatus.PARTIALLY_RECEIVED
    else:
        status = StockTransferStatus.DISPATCHED

    if status != transfer.status:
        transfer.status = status
        transfer.save(update_fields=["status", "updated_at"])
    return transfer


def in_transit_positions(organization: Organization) -> list[StockTransferLine]:
    """Transfer lines with goods still standing in transit, for the report."""
    return list(
        StockTransferLine.objects.filter(
            transfer__organization=organization,
            transfer__status__in=[
                StockTransferStatus.DISPATCHED,
                StockTransferStatus.PARTIALLY_RECEIVED,
            ],
            remaining_quantity__gt=ZERO,
        )
        .select_related(
            "transfer",
            "transfer__source_warehouse",
            "transfer__source_warehouse__branch",
            "transfer__destination_warehouse",
            "transfer__destination_warehouse__branch",
            "item",
            "item__base_unit",
            "lot",
        )
        .order_by("transfer__transfer_number", "sequence")
    )


__all__ = [
    "ReceiptLineInput",
    "TransferLineInput",
    "add_receipt_line",
    "add_transfer_line",
    "allocate",
    "create_receipt",
    "create_shortage",
    "create_transfer",
    "delete_receipt",
    "delete_shortage",
    "delete_transfer",
    "delete_transfer_line",
    "dispatch_transfer",
    "in_transit_positions",
    "post_receipt",
    "post_shortage",
    "recompute_transfer_status",
    "replace_receipt_lines",
    "replace_transfer_lines",
    "reverse_dispatch",
    "reverse_receipt",
    "reverse_shortage",
    "update_receipt",
    "update_transfer",
]
