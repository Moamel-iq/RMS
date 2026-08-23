"""Inventory-side posting adapter for direct-stock sales lines.

This module deliberately knows nothing about the sales application's models.
The sales service plans the inventory effects before it mutates stock, posts
them through the inventory kernel, merges the returned COGS lines into its one
day journal, and finally links that journal back to the stock entry.

The adapter never creates a ``JournalEntry``.  Keeping that boundary is what
allows revenue, tender, commission, tax, and direct-stock COGS to share the
same source identity and the same balanced sales-day journal.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    INVENTORY_CONSUMPTION,
    Account,
    CostCenter,
    JournalEntry,
    JournalEntryStatus,
    SourceEvent,
)
from apps.accounting.validators import (
    PostingLine,
    validate_accounts_are_postable,
    validate_balanced,
    validate_branches_are_active,
    validate_cost_centers,
    validate_line_sides,
    validate_lines_present,
    validate_organization_consistency,
)
from apps.core.locks import lock_account_mappings_shared
from apps.core.money import quantize_money, quantize_unit_price
from apps.core.quantity import quantize_quantity
from apps.core.source_identity import canonical_source_identity
from apps.inventory.accounts import ResolvedAccount, resolve_inventory_account
from apps.inventory.ledger import (
    MovementInput,
    link_journal_entry,
    post_stock_entry,
    reverse_stock_entry,
)
from apps.inventory.models import (
    InventoryItem,
    InventoryLot,
    MovementType,
    StockLedgerEntry,
    StockMovement,
    Warehouse,
)
from apps.organizations.models import Branch, Organization

ZERO = Decimal("0")
DIRECT_SALES_SOURCE_DOCUMENT_TYPE = "SALES.SALESDAY"
DIRECT_RESTOCK_EFFECT_PREFIX = "sale-restock:"


@dataclass(frozen=True)
class DirectStockSaleLine:
    """One sold base-unit quantity that leaves inventory directly."""

    line_key: str
    # Sales persists these on an XOR-shaped line, so its model fields are
    # nullable even though the DIRECT_STOCK branch requires all three.  The
    # adapter accepts that honest type and turns an incomplete snapshot into a
    # named refusal before dereferencing it or touching stock.
    warehouse: Warehouse | None
    item: InventoryItem | None
    quantity: Decimal | None
    cost_center: CostCenter
    lot: InventoryLot | None = None


@dataclass(frozen=True)
class PlannedSalesStockLine:
    """A validated direct sale with its effective-dated COGS resolution."""

    line_key: str
    warehouse: Warehouse
    item: InventoryItem
    quantity: Decimal
    cost_center: CostCenter
    lot: InventoryLot | None
    consumption: ResolvedAccount
    effect_key: str


@dataclass(frozen=True)
class SalesStockPlan:
    """Immutable, account-resolved input to the stock mutation step."""

    organization: Organization
    branch: Branch
    business_date: datetime.date
    lines: tuple[PlannedSalesStockLine, ...]
    fingerprint: str


@dataclass(frozen=True)
class SalesStockEvidence:
    """The exact book cost produced by the kernel for one external line."""

    line_key: str
    movement: StockMovement
    consumption: ResolvedAccount
    cost_center: CostCenter
    cogs_value: Decimal


@dataclass(frozen=True)
class SalesStockPosting:
    """Stock result and journal-ready COGS evidence for the sales service."""

    entry: StockLedgerEntry
    evidence: tuple[SalesStockEvidence, ...]
    posting_lines: tuple[PostingLine, ...]
    total_cost: Decimal

    @property
    def movements(self) -> dict[str, StockMovement]:
        """External sales line key -> immutable stock movement."""
        return {row.line_key: row.movement for row in self.evidence}


@dataclass(frozen=True)
class DirectStockReturnLine:
    """A requested restock against frozen direct-sale fulfillment evidence."""

    line_key: str
    source_movement: StockMovement
    fulfilled_quantity: Decimal
    fulfilled_cogs_value: Decimal
    quantity: Decimal
    control_account: Account | None
    consumption_account: Account
    cost_center: CostCenter


@dataclass(frozen=True)
class PlannedSalesRestockLine:
    """One validated return with the exact value this posting must restore."""

    line_key: str
    source_movement: StockMovement
    fulfilled_quantity: Decimal
    fulfilled_cogs_value: Decimal
    quantity: Decimal
    value: Decimal
    returned_before_quantity: Decimal
    returned_before_value: Decimal
    control_account: Account | None
    consumption_account: Account
    cost_center: CostCenter
    effect_key: str


@dataclass(frozen=True)
class SalesRestockPlan:
    """Concurrency-sensitive plan for a direct-sale physical return."""

    organization: Organization
    branch: Branch
    business_date: datetime.date
    source_document_type: str
    source_document_id: str
    lines: tuple[PlannedSalesRestockLine, ...]
    fingerprint: str


@dataclass(frozen=True)
class SalesRestockEvidence:
    """Exact inventory value restored for one sales adjustment line."""

    line_key: str
    source_movement: StockMovement
    movement: StockMovement
    quantity: Decimal
    cogs_value: Decimal
    consumption_account: Account
    cost_center: CostCenter


@dataclass(frozen=True)
class SalesRestockPosting:
    """Restocked movements and journal-ready reversal of their original COGS."""

    entry: StockLedgerEntry
    evidence: tuple[SalesRestockEvidence, ...]
    posting_lines: tuple[PostingLine, ...]
    total_cost: Decimal

    @property
    def movements(self) -> dict[str, StockMovement]:
        return {row.line_key: row.movement for row in self.evidence}


def _resolved_identity(resolved: ResolvedAccount) -> tuple[int, int | None, int | None]:
    return (
        resolved.account.pk,
        resolved.inventory_mapping.pk if resolved.inventory_mapping is not None else None,
        resolved.organization_mapping.pk if resolved.organization_mapping is not None else None,
    )


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _effect_key(
    *,
    organization: Organization,
    branch: Branch,
    business_date: datetime.date,
    line_key: str,
    warehouse: Warehouse,
    item: InventoryItem,
    lot: InventoryLot | None,
    quantity: Decimal,
    cost_center: CostCenter,
    consumption: ResolvedAccount,
) -> str:
    """Make COGS-only facts part of the inventory kernel's request identity."""
    payload = {
        "organization": organization.pk,
        "branch": branch.pk,
        "business_date": business_date.isoformat(),
        "line_key": line_key,
        "warehouse": warehouse.pk,
        "item": item.pk,
        "lot": lot.pk if lot is not None else None,
        "quantity": str(quantity),
        "cost_center": cost_center.pk,
        "consumption": _resolved_identity(consumption),
    }
    # The two snapshot ids stay visible so a later return can prove that the
    # consumption account and cost centre supplied by sales evidence are the
    # ones this issue was planned with, without inventory importing a sales
    # model.  The digest still binds the complete mapping/version payload.
    return f"sale:{consumption.account.pk}:{cost_center.pk}:{_digest(payload)}"


def _sale_evidence_ids(effect_key: str) -> tuple[int, int] | None:
    parts = effect_key.split(":", maxsplit=3)
    if len(parts) != 4 or parts[0] != "sale":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _validate_line_scope(
    *,
    organization: Organization,
    branch: Branch,
    line: DirectStockSaleLine,
) -> tuple[Warehouse, InventoryItem, Decimal]:
    warehouse = line.warehouse
    item = line.item
    quantity = line.quantity
    if warehouse is None or item is None or quantity is None:
        raise ValidationError(
            _("A direct-stock sales line has an incomplete frozen stock snapshot."),
            code="sales_stock_line_incomplete",
        )
    if branch.organization_id != organization.pk:
        raise ValidationError(
            _("The sales branch belongs to another organization."),
            code="branch_organization_mismatch",
        )
    if not branch.is_active:
        raise ValidationError(_("The sales branch is closed."), code="branch_inactive")
    if warehouse.branch_id != branch.pk:
        raise ValidationError(
            _("A direct sale must issue from a warehouse of its sales branch."),
            code="sales_stock_warehouse_branch_mismatch",
        )
    if not warehouse.is_active:
        raise ValidationError(
            _("A direct sale cannot issue from an archived warehouse."),
            code="warehouse_inactive",
        )
    if warehouse.is_system:
        raise ValidationError(
            _("A direct sale cannot issue from a system warehouse."),
            code="system_warehouse_not_allowed",
        )
    if item.organization_id != organization.pk:
        raise ValidationError(
            _("The direct-sale item belongs to another organization."),
            code="item_organization_mismatch",
        )
    if not item.is_active:
        raise ValidationError(
            _("A direct sale cannot issue an archived item."), code="item_inactive"
        )
    if line.cost_center.organization_id != organization.pk:
        raise ValidationError(
            _("The sales cost center belongs to another organization."),
            code="cost_center_organization_mismatch",
        )
    if not line.cost_center.is_active:
        raise ValidationError(_("The sales cost center is archived."), code="cost_center_inactive")

    if item.tracks_lots and line.lot is None:
        raise ValidationError(_("This item requires a lot."), code="lot_required")
    if not item.tracks_lots and line.lot is not None:
        raise ValidationError(_("This item does not accept a lot."), code="lot_not_allowed")
    if line.lot is not None:
        if line.lot.organization_id != organization.pk:
            raise ValidationError(
                _("The lot belongs to another organization."),
                code="lot_organization_mismatch",
            )
        if line.lot.item_id != item.pk:
            raise ValidationError(_("The lot belongs to another item."), code="lot_item_mismatch")
        if not line.lot.is_active:
            raise ValidationError(_("The lot is archived."), code="lot_inactive")
        if item.tracks_expiry and line.lot.expiry_date is None:
            raise ValidationError(
                _("This item requires an expiry date on its lot."),
                code="lot_expiry_required",
            )
    return warehouse, item, quantity


def _validate_consumption_line(
    *, organization: Organization, branch: Branch, line: PlannedSalesStockLine
) -> None:
    probe = PostingLine(
        account=line.consumption.account,
        branch=branch,
        debit=Decimal("1"),
        cost_center=line.cost_center,
    )
    rows = [probe]
    validate_accounts_are_postable(rows)
    validate_cost_centers(rows)
    validate_organization_consistency(organization, rows)
    validate_branches_are_active(rows)


def _build_plan(
    *,
    organization: Organization,
    branch: Branch,
    business_date: datetime.date,
    lines: Sequence[DirectStockSaleLine],
) -> SalesStockPlan:
    if not lines:
        raise ValidationError(
            _("A direct-stock sales posting needs at least one line."),
            code="sales_stock_has_no_lines",
        )

    seen: set[str] = set()
    planned: list[PlannedSalesStockLine] = []
    for raw in lines:
        line_key = raw.line_key.strip()
        if not line_key:
            raise ValidationError(
                _("Every direct-stock sales line needs a stable key."),
                code="sales_stock_line_key_required",
            )
        if line_key in seen:
            raise ValidationError(
                _("Direct-stock sales line key %(key)s is repeated."),
                code="sales_stock_line_key_duplicate",
                params={"key": line_key},
            )
        seen.add(line_key)
        warehouse, item, raw_quantity = _validate_line_scope(
            organization=organization, branch=branch, line=raw
        )
        quantity = quantize_quantity(raw_quantity)
        if quantity <= ZERO:
            raise ValidationError(
                _("A direct-stock sales quantity must be positive."),
                code="sales_stock_quantity_not_positive",
            )
        consumption = resolve_inventory_account(
            organization=organization,
            role=INVENTORY_CONSUMPTION,
            item=item,
            on_date=business_date,
        )
        row = PlannedSalesStockLine(
            line_key=line_key,
            warehouse=warehouse,
            item=item,
            quantity=quantity,
            cost_center=raw.cost_center,
            lot=raw.lot,
            consumption=consumption,
            effect_key=_effect_key(
                organization=organization,
                branch=branch,
                business_date=business_date,
                line_key=line_key,
                warehouse=warehouse,
                item=item,
                lot=raw.lot,
                quantity=quantity,
                cost_center=raw.cost_center,
                consumption=consumption,
            ),
        )
        _validate_consumption_line(organization=organization, branch=branch, line=row)
        planned.append(row)

    ordered = tuple(sorted(planned, key=lambda row: row.line_key))
    fingerprint = _digest(
        {
            "organization": organization.pk,
            "branch": branch.pk,
            "business_date": business_date.isoformat(),
            "effects": [row.effect_key for row in ordered],
        }
    )
    return SalesStockPlan(
        organization=organization,
        branch=branch,
        business_date=business_date,
        lines=ordered,
        fingerprint=fingerprint,
    )


@transaction.atomic
def plan_sales_stock(
    *,
    organization: Organization,
    branch: Branch,
    business_date: datetime.date,
    lines: Sequence[DirectStockSaleLine],
) -> SalesStockPlan:
    """Resolve and validate every consumption account without moving stock."""
    lock_account_mappings_shared(organization.pk)
    return _build_plan(
        organization=organization,
        branch=branch,
        business_date=business_date,
        lines=lines,
    )


def _raw_lines(plan: SalesStockPlan) -> list[DirectStockSaleLine]:
    return [
        DirectStockSaleLine(
            line_key=row.line_key,
            warehouse=row.warehouse,
            item=row.item,
            quantity=row.quantity,
            cost_center=row.cost_center,
            lot=row.lot,
        )
        for row in plan.lines
    ]


def _movement_input(line: PlannedSalesStockLine) -> MovementInput:
    return MovementInput(
        warehouse=line.warehouse,
        item=line.item,
        movement_type=MovementType.ISSUE,
        quantity=line.quantity,
        effect_key=line.effect_key,
        lot=line.lot,
    )


def _posting_lines(
    *,
    organization: Organization,
    branch: Branch,
    evidence: Sequence[SalesStockEvidence],
) -> tuple[PostingLine, ...]:
    inventory: dict[int, tuple[Account, Decimal]] = {}
    consumption: dict[tuple[int, int], tuple[Account, CostCenter, Decimal]] = {}

    for row in evidence:
        # Zero is evidence too: it says the kernel issued a currently unvalued
        # position.  It has no journal effect, so it must not become a zero
        # PostingLine (which the accounting kernel correctly refuses).
        if row.cogs_value == ZERO:
            continue
        control = row.movement.control_account
        if control is None:
            raise ValidationError(
                _("A valued direct-stock issue has no inventory control account."),
                code="sales_stock_control_account_missing",
            )
        old_account, old_value = inventory.get(control.pk, (control, ZERO))
        inventory[control.pk] = (old_account, quantize_money(old_value + row.cogs_value))

        key = (row.consumption.account.pk, row.cost_center.pk)
        old_account, old_center, old_value = consumption.get(
            key, (row.consumption.account, row.cost_center, ZERO)
        )
        consumption[key] = (
            old_account,
            old_center,
            quantize_money(old_value + row.cogs_value),
        )

    lines: list[PostingLine] = []
    for account_id in sorted(inventory):
        account, value = inventory[account_id]
        lines.append(
            PostingLine(
                account=account,
                branch=branch,
                credit=value,
                narration="Direct-stock sales: inventory issued",
            )
        )
    for key in sorted(consumption):
        account, cost_center, value = consumption[key]
        lines.append(
            PostingLine(
                account=account,
                branch=branch,
                debit=value,
                cost_center=cost_center,
                narration="Direct-stock sales: cost of goods sold",
            )
        )

    if not lines:
        return ()
    validate_lines_present(lines)
    validate_line_sides(lines)
    validate_balanced(lines)
    validate_accounts_are_postable(lines)
    validate_cost_centers(lines)
    validate_organization_consistency(organization, lines)
    validate_branches_are_active(lines)
    return tuple(lines)


def _require_aware(value: datetime.datetime) -> None:
    if timezone.is_naive(value):
        raise ValidationError(
            _("The effective timestamp must include a timezone."),
            code="effective_at_timezone_required",
        )


@transaction.atomic
def post_sales_stock(
    *,
    plan: SalesStockPlan,
    effective_at: datetime.datetime,
    idempotency_key: str,
    source_document_type: str,
    source_document_id: str,
    reference: str = "",
    reason: str = "",
) -> SalesStockPosting:
    """Post all direct lines as one stock event and return journal-ready COGS."""
    _require_aware(effective_at)
    if not idempotency_key.strip():
        raise ValidationError(_("An idempotency key is required."), code="idempotency_key_required")
    if not source_document_type.strip() or not source_document_id.strip():
        raise ValidationError(
            _("Direct sales stock requires the complete sales source identity."),
            code="sales_stock_source_identity_required",
        )

    # Account mappings are re-resolved under the posting transaction.  A plan
    # made before a mapping change is refused rather than posting COGS through
    # a stale account snapshot.
    lock_account_mappings_shared(plan.organization.pk)
    current = _build_plan(
        organization=plan.organization,
        branch=plan.branch,
        business_date=plan.business_date,
        lines=_raw_lines(plan),
    )
    if current.fingerprint != plan.fingerprint:
        raise ValidationError(
            _("The direct-stock sales plan is stale; plan it again."),
            code="sales_stock_plan_stale",
        )

    entry = post_stock_entry(
        organization=current.organization,
        effects=[_movement_input(row) for row in current.lines],
        idempotency_key=idempotency_key.strip(),
        effective_at=effective_at,
        business_date=current.business_date,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        source_event=SourceEvent.POSTED,
        reference=reference,
        reason=reason,
    )
    movements = {
        row.effect_key: row for row in entry.movements.select_related("control_account").all()
    }
    evidence: list[SalesStockEvidence] = []
    for planned in current.lines:
        movement = movements.get(planned.effect_key)
        if movement is None:  # pragma: no cover - protected by kernel effect identity
            raise ValidationError(
                _("The stock kernel omitted a planned sales effect."),
                code="sales_stock_movement_missing",
            )
        if movement.movement_type != MovementType.ISSUE:
            raise ValidationError(
                _("The stock kernel returned a non-issue for a direct sale."),
                code="sales_stock_movement_type_mismatch",
            )
        expected_quantity = -planned.quantity
        if movement.base_quantity != expected_quantity:  # pragma: no cover - kernel invariant
            raise ValidationError(
                _("The posted direct-sale quantity differs from its plan."),
                code="sales_stock_quantity_mismatch",
            )
        cogs_value = quantize_money(-movement.inventory_value)
        if cogs_value < ZERO:  # pragma: no cover - outbound kernel invariant
            raise ValidationError(
                _("A direct-stock issue returned a negative COGS value."),
                code="sales_stock_cost_negative",
            )
        evidence.append(
            SalesStockEvidence(
                line_key=planned.line_key,
                movement=movement,
                consumption=planned.consumption,
                cost_center=planned.cost_center,
                cogs_value=cogs_value,
            )
        )

    posting_lines = _posting_lines(
        organization=current.organization,
        branch=current.branch,
        evidence=evidence,
    )
    return SalesStockPosting(
        entry=entry,
        evidence=tuple(evidence),
        posting_lines=posting_lines,
        total_cost=quantize_money(sum((row.cogs_value for row in evidence), ZERO)),
    )


# ---------------------------------------------------------------------------
# Direct-sale physical returns
# ---------------------------------------------------------------------------


def _restock_prefix(source_movement: StockMovement) -> str:
    return f"{DIRECT_RESTOCK_EFFECT_PREFIX}{source_movement.pk}:"


def _restock_effect_key(
    *,
    source_movement: StockMovement,
    source_document_type: str,
    source_document_id: str,
    line_key: str,
    quantity: Decimal,
    value: Decimal,
    consumption_account: Account,
    cost_center: CostCenter,
) -> str:
    payload = {
        "source_movement": source_movement.pk,
        "source_document_type": source_document_type,
        "source_document_id": source_document_id,
        "line_key": line_key,
        "quantity": str(quantity),
        "value": str(value),
        "consumption_account": consumption_account.pk,
        "cost_center": cost_center.pk,
    }
    return f"{_restock_prefix(source_movement)}{_digest(payload)}"


def _return_sources(
    lines: Sequence[DirectStockReturnLine], *, lock: bool
) -> dict[int, StockMovement]:
    source_ids = [line.source_movement.pk for line in lines]
    query = StockMovement.objects.select_related(
        "entry",
        "entry__journal_entry",
        "warehouse",
        "item",
        "lot",
        "control_account",
    ).filter(pk__in=source_ids, reversed_by__isnull=True)
    if lock:
        # Lock only the movement rows.  Some selected relations are nullable,
        # and PostgreSQL correctly refuses FOR UPDATE on an outer-join side.
        query = query.select_for_update(of=("self",))
    found = {movement.pk: movement for movement in query.order_by("pk")}
    if len(found) != len(source_ids):
        raise ValidationError(
            _("A direct-sale source movement is missing or has been reversed."),
            code="sales_restock_source_unavailable",
        )
    return found


def _active_returns(
    *,
    source_movement: StockMovement,
    source_document_type: str,
    source_document_id: str,
    lock: bool,
) -> list[StockMovement]:
    query = StockMovement.objects.filter(
        movement_type=MovementType.RETURN_IN,
        effect_key__startswith=_restock_prefix(source_movement),
        reversed_by__isnull=True,
    ).exclude(
        entry__source_document_type=source_document_type,
        entry__source_document_id=source_document_id,
        entry__source_event=SourceEvent.POSTED,
    )
    if lock:
        query = query.select_for_update(of=("self",))
    rows = list(query.order_by("pk"))
    for movement in rows:
        same_position = (
            movement.organization_id == source_movement.organization_id
            and movement.branch_id == source_movement.branch_id
            and movement.warehouse_id == source_movement.warehouse_id
            and movement.item_id == source_movement.item_id
            and movement.lot_id == source_movement.lot_id
            and movement.control_account_id == source_movement.control_account_id
        )
        if not same_position or movement.base_quantity <= ZERO or movement.inventory_value < ZERO:
            raise ValidationError(
                _("Stored direct-sale return evidence conflicts with its source issue."),
                code="sales_restock_evidence_conflict",
            )
    return rows


def _validate_return_evidence(
    *,
    organization: Organization,
    branch: Branch,
    raw: DirectStockReturnLine,
    source: StockMovement,
) -> tuple[Decimal, Decimal, Decimal]:
    if branch.organization_id != organization.pk or not branch.is_active:
        raise ValidationError(
            _("The return branch is closed or belongs to another organization."),
            code="sales_restock_branch_unusable",
        )
    if source.organization_id != organization.pk or source.branch_id != branch.pk:
        raise ValidationError(
            _("The source issue belongs to another sales scope."),
            code="sales_restock_source_scope_mismatch",
        )
    if (
        source.movement_type != MovementType.ISSUE
        or _sale_evidence_ids(source.effect_key) is None
        or source.entry.source_document_type != DIRECT_SALES_SOURCE_DOCUMENT_TYPE
        or source.entry.source_event != SourceEvent.POSTED
        or source.entry.journal_entry_id is None
    ):
        raise ValidationError(
            _("The return source is not a linked direct-stock sales issue."),
            code="sales_restock_direct_issue_required",
        )

    evidence_ids = _sale_evidence_ids(source.effect_key)
    assert evidence_ids is not None  # noqa: S101 - validated in the branch above
    consumption_account_id, cost_center_id = evidence_ids
    if raw.consumption_account.pk != consumption_account_id or raw.cost_center.pk != cost_center_id:
        raise ValidationError(
            _("The supplied COGS account or cost center differs from the source issue evidence."),
            code="sales_restock_cogs_evidence_mismatch",
        )

    fulfilled_quantity = quantize_quantity(raw.fulfilled_quantity)
    fulfilled_value = quantize_money(raw.fulfilled_cogs_value)
    quantity = quantize_quantity(raw.quantity)
    if fulfilled_quantity <= ZERO or quantity <= ZERO or fulfilled_value < ZERO:
        raise ValidationError(
            _("Direct-sale fulfillment and return quantities must be positive."),
            code="sales_restock_quantity_not_positive",
        )
    if fulfilled_quantity != -source.base_quantity or fulfilled_value != quantize_money(
        -source.inventory_value
    ):
        raise ValidationError(
            _("The supplied fulfillment totals do not match the source issue."),
            code="sales_restock_fulfillment_mismatch",
        )
    if raw.control_account != source.control_account:
        raise ValidationError(
            _("The supplied inventory control account does not match the source issue."),
            code="sales_restock_control_account_mismatch",
        )
    if fulfilled_value > ZERO and raw.control_account is None:
        raise ValidationError(
            _("A valued return source has no inventory control account."),
            code="sales_restock_control_account_missing",
        )
    if raw.consumption_account.organization_id != organization.pk:
        raise ValidationError(
            _("The original consumption account belongs to another organization."),
            code="account_organization_mismatch",
        )
    if raw.cost_center.organization_id != organization.pk:
        raise ValidationError(
            _("The original cost center belongs to another organization."),
            code="cost_center_organization_mismatch",
        )
    probe = PostingLine(
        account=raw.consumption_account,
        branch=branch,
        credit=Decimal("1"),
        cost_center=raw.cost_center,
    )
    validate_accounts_are_postable([probe])
    validate_cost_centers([probe])
    validate_organization_consistency(organization, [probe])
    return fulfilled_quantity, fulfilled_value, quantity


def _build_restock_plan(
    *,
    organization: Organization,
    branch: Branch,
    business_date: datetime.date,
    source_document_type: str,
    source_document_id: str,
    lines: Sequence[DirectStockReturnLine],
    lock: bool,
) -> SalesRestockPlan:
    if not lines:
        raise ValidationError(
            _("A direct-sales restock needs at least one line."),
            code="sales_restock_has_no_lines",
        )
    source = canonical_source_identity(
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        source_event=SourceEvent.POSTED,
    )
    if not source.is_complete:
        raise ValidationError(
            _("A direct-sales restock needs a complete source identity."),
            code="sales_restock_source_identity_required",
        )

    line_keys = [line.line_key.strip() for line in lines]
    if any(not key for key in line_keys):
        raise ValidationError(
            _("Every direct-sales restock line needs a stable key."),
            code="sales_restock_line_key_required",
        )
    if len(set(line_keys)) != len(line_keys):
        raise ValidationError(
            _("A direct-sales restock line key is repeated."),
            code="sales_restock_line_key_duplicate",
        )
    source_ids = [line.source_movement.pk for line in lines]
    if len(set(source_ids)) != len(source_ids):
        raise ValidationError(
            _("One return event may name a fulfillment only once."),
            code="sales_restock_source_duplicate",
        )

    sources = _return_sources(lines, lock=lock)
    planned: list[PlannedSalesRestockLine] = []
    for raw, line_key in zip(lines, line_keys, strict=True):
        movement = sources[raw.source_movement.pk]
        fulfilled_quantity, fulfilled_value, quantity = _validate_return_evidence(
            organization=organization,
            branch=branch,
            raw=raw,
            source=movement,
        )
        existing = _active_returns(
            source_movement=movement,
            source_document_type=source.document_type,
            source_document_id=source.document_id,
            lock=lock,
        )
        returned_quantity = quantize_quantity(sum((row.base_quantity for row in existing), ZERO))
        returned_value = quantize_money(sum((row.inventory_value for row in existing), ZERO))
        remaining_quantity = quantize_quantity(fulfilled_quantity - returned_quantity)
        remaining_value = quantize_money(fulfilled_value - returned_value)
        if remaining_quantity < ZERO or remaining_value < ZERO:
            raise ValidationError(
                _("Existing return evidence exceeds its original fulfillment."),
                code="sales_restock_existing_over_return",
            )
        if quantity > remaining_quantity:
            raise ValidationError(
                _("This return exceeds the unreturned direct-sale quantity."),
                code="sales_restock_over_return",
            )

        if quantity == remaining_quantity:
            # The last return takes the exact stored remainder, closing every
            # three-decimal proportional rounding difference by construction.
            value = remaining_value
        elif fulfilled_value == ZERO:
            value = ZERO
        else:
            proportional = quantize_money(fulfilled_value * quantity / fulfilled_quantity)
            value = min(proportional, remaining_value)

        planned.append(
            PlannedSalesRestockLine(
                line_key=line_key,
                source_movement=movement,
                fulfilled_quantity=fulfilled_quantity,
                fulfilled_cogs_value=fulfilled_value,
                quantity=quantity,
                value=value,
                returned_before_quantity=returned_quantity,
                returned_before_value=returned_value,
                control_account=raw.control_account,
                consumption_account=raw.consumption_account,
                cost_center=raw.cost_center,
                effect_key=_restock_effect_key(
                    source_movement=movement,
                    source_document_type=source.document_type,
                    source_document_id=source.document_id,
                    line_key=line_key,
                    quantity=quantity,
                    value=value,
                    consumption_account=raw.consumption_account,
                    cost_center=raw.cost_center,
                ),
            )
        )

    ordered = tuple(sorted(planned, key=lambda row: row.line_key))
    fingerprint = _digest(
        {
            "organization": organization.pk,
            "branch": branch.pk,
            "business_date": business_date.isoformat(),
            "source": [source.document_type, source.document_id, source.event],
            "lines": [
                [
                    row.effect_key,
                    str(row.returned_before_quantity),
                    str(row.returned_before_value),
                ]
                for row in ordered
            ],
        }
    )
    return SalesRestockPlan(
        organization=organization,
        branch=branch,
        business_date=business_date,
        source_document_type=source.document_type,
        source_document_id=source.document_id,
        lines=ordered,
        fingerprint=fingerprint,
    )


@transaction.atomic
def plan_sales_restock(
    *,
    organization: Organization,
    branch: Branch,
    business_date: datetime.date,
    source_document_type: str,
    source_document_id: str,
    lines: Sequence[DirectStockReturnLine],
) -> SalesRestockPlan:
    """Plan a physical return without moving stock or writing accounting."""
    return _build_restock_plan(
        organization=organization,
        branch=branch,
        business_date=business_date,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        lines=lines,
        lock=False,
    )


def _raw_return_lines(plan: SalesRestockPlan) -> list[DirectStockReturnLine]:
    return [
        DirectStockReturnLine(
            line_key=row.line_key,
            source_movement=row.source_movement,
            fulfilled_quantity=row.fulfilled_quantity,
            fulfilled_cogs_value=row.fulfilled_cogs_value,
            quantity=row.quantity,
            control_account=row.control_account,
            consumption_account=row.consumption_account,
            cost_center=row.cost_center,
        )
        for row in plan.lines
    ]


def _restock_input(row: PlannedSalesRestockLine) -> MovementInput:
    unit_cost = ZERO if row.value == ZERO else quantize_unit_price(row.value / row.quantity)
    return MovementInput(
        warehouse=row.source_movement.warehouse,
        item=row.source_movement.item,
        movement_type=MovementType.RETURN_IN,
        quantity=row.quantity,
        effect_key=row.effect_key,
        lot=row.source_movement.lot,
        unit_cost=unit_cost,
        inbound_value=row.value,
        control_account=row.control_account,
    )


def _restock_posting_lines(
    *,
    organization: Organization,
    branch: Branch,
    evidence: Sequence[SalesRestockEvidence],
) -> tuple[PostingLine, ...]:
    inventory: dict[int, tuple[Account, Decimal]] = {}
    consumption: dict[tuple[int, int], tuple[Account, CostCenter, Decimal]] = {}
    for row in evidence:
        if row.cogs_value == ZERO:
            continue
        control = row.movement.control_account
        if control is None:  # pragma: no cover - refused while planning
            raise ValidationError(
                _("A valued direct-sales return has no inventory control account."),
                code="sales_restock_control_account_missing",
            )
        account, value = inventory.get(control.pk, (control, ZERO))
        inventory[control.pk] = (account, quantize_money(value + row.cogs_value))
        key = (row.consumption_account.pk, row.cost_center.pk)
        account, center, value = consumption.get(
            key, (row.consumption_account, row.cost_center, ZERO)
        )
        consumption[key] = (account, center, quantize_money(value + row.cogs_value))

    lines: list[PostingLine] = []
    for account_id in sorted(inventory):
        account, value = inventory[account_id]
        lines.append(
            PostingLine(
                account=account,
                branch=branch,
                debit=value,
                narration="Direct-stock return: inventory restored",
            )
        )
    for key in sorted(consumption):
        account, center, value = consumption[key]
        lines.append(
            PostingLine(
                account=account,
                branch=branch,
                credit=value,
                cost_center=center,
                narration="Direct-stock return: cost of goods sold reversed",
            )
        )
    if not lines:
        return ()
    validate_lines_present(lines)
    validate_line_sides(lines)
    validate_balanced(lines)
    validate_accounts_are_postable(lines)
    validate_cost_centers(lines)
    validate_organization_consistency(organization, lines)
    validate_branches_are_active(lines)
    return tuple(lines)


@transaction.atomic
def post_sales_restock(
    *,
    plan: SalesRestockPlan,
    effective_at: datetime.datetime,
    idempotency_key: str,
    reference: str = "",
    reason: str = "",
) -> SalesRestockPosting:
    """Atomically revalidate, restock, and return exact COGS reversal lines."""
    _require_aware(effective_at)
    if not idempotency_key.strip():
        raise ValidationError(_("An idempotency key is required."), code="idempotency_key_required")

    current = _build_restock_plan(
        organization=plan.organization,
        branch=plan.branch,
        business_date=plan.business_date,
        source_document_type=plan.source_document_type,
        source_document_id=plan.source_document_id,
        lines=_raw_return_lines(plan),
        lock=True,
    )
    if current.fingerprint != plan.fingerprint:
        raise ValidationError(
            _("The direct-sales return plan is stale; plan it again."),
            code="sales_restock_plan_stale",
        )

    entry = post_stock_entry(
        organization=current.organization,
        effects=[_restock_input(row) for row in current.lines],
        idempotency_key=idempotency_key.strip(),
        effective_at=effective_at,
        business_date=current.business_date,
        source_document_type=current.source_document_type,
        source_document_id=current.source_document_id,
        source_event=SourceEvent.POSTED,
        reference=reference,
        reason=reason,
    )
    movements = {
        movement.effect_key: movement
        for movement in entry.movements.select_related("control_account").all()
    }
    evidence: list[SalesRestockEvidence] = []
    for row in current.lines:
        movement = movements.get(row.effect_key)
        if movement is None:  # pragma: no cover - kernel identity invariant
            raise ValidationError(
                _("The stock kernel omitted a planned return effect."),
                code="sales_restock_movement_missing",
            )
        if (
            movement.movement_type != MovementType.RETURN_IN
            or movement.base_quantity != row.quantity
            or quantize_money(movement.inventory_value) != row.value
        ):
            raise ValidationError(
                _("The posted return differs from its exact plan."),
                code="sales_restock_movement_mismatch",
            )
        evidence.append(
            SalesRestockEvidence(
                line_key=row.line_key,
                source_movement=row.source_movement,
                movement=movement,
                quantity=row.quantity,
                cogs_value=row.value,
                consumption_account=row.consumption_account,
                cost_center=row.cost_center,
            )
        )

    posting_lines = _restock_posting_lines(
        organization=current.organization,
        branch=current.branch,
        evidence=evidence,
    )
    return SalesRestockPosting(
        entry=entry,
        evidence=tuple(evidence),
        posting_lines=posting_lines,
        total_cost=quantize_money(sum((row.cogs_value for row in evidence), ZERO)),
    )


@transaction.atomic
def link_sales_stock_journal(*, entry: StockLedgerEntry, journal: JournalEntry) -> StockLedgerEntry:
    """Link the caller's one posted sales journal to its matching stock event."""
    locked = StockLedgerEntry.objects.select_for_update().get(pk=entry.pk)
    posted_journal = JournalEntry.objects.select_for_update().get(pk=journal.pk)
    if posted_journal.status != JournalEntryStatus.POSTED:
        raise ValidationError(
            _("Only a posted sales journal can be linked to stock."),
            code="sales_stock_journal_not_posted",
        )
    if locked.organization_id != posted_journal.organization_id:
        raise ValidationError(
            _("The sales journal and stock entry belong to different organizations."),
            code="sales_stock_journal_organization_mismatch",
        )
    stock_source = (
        locked.source_document_type,
        locked.source_document_id,
        locked.source_event,
    )
    journal_source = (
        posted_journal.source_document_type,
        posted_journal.source_document_id,
        posted_journal.source_event,
    )
    if not all(stock_source) or stock_source != journal_source:
        raise ValidationError(
            _("The sales journal does not carry the stock entry's source identity."),
            code="sales_stock_journal_source_mismatch",
        )
    if locked.journal_entry_id == posted_journal.pk:
        return locked
    return link_journal_entry(entry=locked, journal=posted_journal)


@transaction.atomic
def reverse_sales_stock(
    *,
    entry: StockLedgerEntry,
    idempotency_key: str,
    reason: str,
    effective_at: datetime.datetime,
    business_date: datetime.date,
) -> StockLedgerEntry:
    """Reverse one linked direct-sales stock event through the exact kernel path."""
    _require_aware(effective_at)
    locked = (
        StockLedgerEntry.objects.select_for_update(of=("self",))
        .select_related("journal_entry")
        .get(pk=entry.pk)
    )
    if locked.source_event != SourceEvent.POSTED or not (
        locked.source_document_type and locked.source_document_id
    ):
        raise ValidationError(
            _("Only an original sourced sales stock entry can be reversed here."),
            code="sales_stock_original_required",
        )
    linked_journal = locked.journal_entry
    if linked_journal is None:
        raise ValidationError(
            _("Link the sales journal before reversing its stock."),
            code="sales_stock_journal_link_required",
        )
    if linked_journal.status != JournalEntryStatus.POSTED:
        raise ValidationError(
            _("The linked sales journal is not posted."),
            code="sales_stock_linked_journal_not_posted",
        )
    movement_types = set(locked.movements.values_list("movement_type", flat=True))
    if movement_types not in ({MovementType.ISSUE}, {MovementType.RETURN_IN}):
        raise ValidationError(
            _("This stock entry is not a direct-sales issue or restock event."),
            code="sales_stock_event_required",
        )
    return reverse_stock_entry(
        entry=locked,
        idempotency_key=idempotency_key,
        reason=reason,
        effective_at=effective_at,
        business_date=business_date,
    )
