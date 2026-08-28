"""
Who may move stock, and where.

The kernel in `apps/inventory/ledger.py` knows inventory arithmetic and
nothing about users. This layer resolves scope, checks permissions, binds the
audit actor, and then calls the kernel — the same division
`apps/accounting/commands.py` makes over `apps/accounting/services.py`, for
the same reason: a posting rule that also has to know about roles ends up
knowing about neither properly.

Two consequences worth stating.

**The audited actor is the authorized actor.** Every kernel call runs inside
`audit_context(actor=...)` with the user the permission was checked against, so
"who was allowed" and "who is recorded" cannot disagree.

**Nothing here resolves an id without its caller.** A warehouse arrives as an
object already produced by `resolve_warehouse(user, id)`, so there is never a
moment where an out-of-scope object sits in a local variable waiting to be
used.

Task 1.2 exposes posting through this layer for the kernel's own tests and for
the tasks that follow. The operational documents — receipts, issues,
transfers, waste, counts — are Tasks 1.3 to 1.6 and are not built here.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import Account, AccountRole
from apps.accounting.permissions import MANAGE_ACCOUNT_MAPPINGS
from apps.core.context import audit_context
from apps.inventory import adjustments, counts, opening, operations, transfers
from apps.inventory.accounts import (
    archive_inventory_mapping,
    close_inventory_mapping,
    create_inventory_mapping,
)
from apps.inventory.ledger import MovementInput, post_stock_entry, reverse_stock_entry
from apps.inventory.models import (
    INBOUND_MOVEMENT_TYPES,
    InventoryAccountMapping,
    InventoryAdjustmentDocument,
    InventoryAdjustmentLine,
    InventoryDocumentType,
    InventoryItem,
    InventoryMovementDocument,
    InventoryMovementDocumentLine,
    ItemCategory,
    MovementType,
    OpeningStockDocument,
    OpeningStockLine,
    StockBalance,
    StockCount,
    StockLedgerEntry,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    StockTransferReceipt,
    StockTransferShortage,
    Warehouse,
)
from apps.inventory.permissions import (
    APPROVE_STOCK_COUNT,
    CLOSE_TRANSFER_SHORTAGE,
    CONDUCT_STOCK_COUNT,
    CREATE_DRAFT_MOVEMENT,
    CREATE_OPENING_STOCK,
    POST_ADJUSTMENT,
    POST_ISSUE,
    POST_OPENING_STOCK,
    POST_RECEIPT,
    POST_TRANSFER,
    POST_WASTE,
    REVERSE_MOVEMENT,
    VIEW_STOCK,
    VIEW_VALUATION,
)
from apps.organizations.authorization import (
    OutOfScope,
    accessible_warehouses,
    branches_with_permission,
    can_access_warehouse,
    require_branch_permission,
    require_organization_permission,
    require_warehouse_permission,
    resolve_organization,
)
from apps.organizations.models import Branch, Organization
from apps.users.models import User

#: Which permission each movement type needs at the warehouse it touches.
#: Opening stock is absent on purpose: it is organization authority, checked
#: separately, because it sets the ledger's starting point rather than moving
#: goods that are already in it.
MOVEMENT_PERMISSION: dict[str, str] = {
    MovementType.RECEIPT: POST_RECEIPT,
    MovementType.TRANSFER_IN: POST_RECEIPT,
    MovementType.ISSUE: POST_ISSUE,
    MovementType.TRANSFER_OUT: POST_TRANSFER,
    MovementType.WASTE: POST_WASTE,
    MovementType.PRODUCTION_IN: POST_RECEIPT,
    MovementType.PRODUCTION_OUT: POST_ISSUE,
    MovementType.COUNT_GAIN: POST_RECEIPT,
    MovementType.COUNT_LOSS: POST_ISSUE,
    MovementType.MANUAL_ADJUSTMENT: POST_RECEIPT,
}


@contextmanager
def _acting_as(actor: User) -> Iterator[None]:
    """Bind the authorized user as the audit actor for the whole posting."""
    with audit_context(actor=actor):
        yield


def _authorize_effects(actor: User, effects: Sequence[MovementInput]) -> None:
    """
    Every effect, at its own warehouse.

    Checked per effect rather than once per posting: a transfer names two
    warehouses, and authority over the one goods leave is not authority over
    the one they arrive at.
    """
    for effect in effects:
        if effect.movement_type == MovementType.OPENING:
            continue
        permission = MOVEMENT_PERMISSION.get(effect.movement_type)
        if permission is None:
            raise ValidationError(
                _("%(type)s has no permission mapping."),
                code="unmapped_movement_type",
                params={"type": effect.movement_type},
            )
        require_warehouse_permission(actor, permission, effect.warehouse)


def post_stock_movements(
    *,
    actor: User,
    organization: Organization,
    effects: Sequence[MovementInput],
    idempotency_key: str,
    effective_at: datetime.datetime | None = None,
    source_document_type: str = "",
    source_document_id: str = "",
    source_event: str = "",
    reference: str = "",
    reason: str = "",
) -> StockLedgerEntry:
    """
    Post stock effects on behalf of an authorized user.

    Opening stock is the one movement type that needs **organization**
    authority: it declares what the ledger starts from, which is an accounting
    decision covering every branch at once, not a warehouse operation. Everything
    else is answered at the warehouse the goods actually move through.
    """
    if any(effect.movement_type == MovementType.OPENING for effect in effects):
        require_organization_permission(actor, POST_OPENING_STOCK, organization)

    _authorize_effects(actor, effects)

    with _acting_as(actor):
        return post_stock_entry(
            organization=organization,
            effects=effects,
            idempotency_key=idempotency_key,
            effective_at=effective_at,
            source_document_type=source_document_type,
            source_document_id=source_document_id,
            source_event=source_event,
            reference=reference,
            reason=reason,
        )


def reverse_stock_movements(
    *,
    actor: User,
    entry: StockLedgerEntry,
    idempotency_key: str,
    reason: str,
    effective_at: datetime.datetime | None = None,
) -> StockLedgerEntry:
    """
    Reverse a posting, at every warehouse it touched.

    `reverse_movement` is required at each one. Authority over half a posting
    authorizes nothing: a reversal that could be applied to some of its
    movements and not others would leave the ledger in a state no document
    describes.
    """
    for movement in entry.movements.select_related("warehouse", "warehouse__branch"):
        require_warehouse_permission(actor, REVERSE_MOVEMENT, movement.warehouse)

    with _acting_as(actor):
        return reverse_stock_entry(
            entry=entry,
            idempotency_key=idempotency_key,
            reason=reason,
            effective_at=effective_at,
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def visible_stock(actor: User) -> QuerySet[StockBalance]:
    """
    Stock the caller may see, scoped by warehouse custody.

    `view_stock` is branch-scoped, but the *rows* are filtered by
    `accessible_warehouses`, which honours a `SELECTED` membership. Someone
    trusted with one warehouse does not thereby see the branch's whole
    position.
    """
    if not actor.is_authenticated or not actor.is_active:
        return StockBalance.objects.none()
    if not actor.has_perm(VIEW_STOCK):
        return StockBalance.objects.none()
    return StockBalance.objects.filter(warehouse__in=accessible_warehouses(actor)).select_related(
        "warehouse", "warehouse__branch", "item", "item__base_unit", "lot"
    )


def visible_movements(actor: User) -> QuerySet[StockMovement]:
    """Movement history the caller may see, scoped the same way."""
    if not actor.is_authenticated or not actor.is_active:
        return StockMovement.objects.none()
    if not actor.has_perm(VIEW_STOCK):
        return StockMovement.objects.none()
    return StockMovement.objects.filter(warehouse__in=accessible_warehouses(actor)).select_related(
        "entry", "warehouse", "warehouse__branch", "item", "item__base_unit", "lot"
    )


def resolve_movement(actor: User, movement_id: int) -> StockMovement:
    """Turn a submitted movement id into one the caller may read."""
    movement = visible_movements(actor).filter(pk=movement_id).first()
    if movement is None:
        raise OutOfScope(_("Movement %(id)s does not exist.") % {"id": movement_id})
    return movement


def may_see_cost(actor: User) -> bool:
    """
    Whether cost and value may be shown at all.

    A storekeeper holds `view_stock` and not `view_valuation`: they must know
    what they are moving and have no business knowing what it cost. The API
    and the screens both omit the columns rather than blanking them, because a
    blanked column still says "there is a number here".
    """
    return bool(actor.is_authenticated and actor.is_active and actor.has_perm(VIEW_VALUATION))


def stock_value_of(balance: StockBalance) -> tuple[str, str]:
    """The value and average of one position, as exact strings."""
    return (f"{balance.value:f}", f"{balance.average_cost:f}")


def is_inbound(movement_type: str) -> bool:
    return movement_type in INBOUND_MOVEMENT_TYPES


# ---------------------------------------------------------------------------
# Inventory account-mapping overrides (Task 1.3)
# ---------------------------------------------------------------------------
#
# The overrides use the same `accounting.manage_account_mappings` authority as
# the organization defaults, checked the same way: an OrganizationMembership
# role that carries it. Choosing where an item's value posts is one decision
# regardless of which table records it.


def visible_inventory_mappings(
    actor: User, organization_id: int
) -> QuerySet[InventoryAccountMapping]:
    """One organization's overrides, for a caller who reaches it."""
    organization = resolve_organization(actor, organization_id)
    return (
        InventoryAccountMapping.objects.filter(organization=organization)
        .select_related("account_role", "account", "item", "category")
        .order_by("account_role__code", "-version")
    )


def _resolve_inventory_mapping(actor: User, mapping_id: int) -> InventoryAccountMapping:
    mapping = (
        InventoryAccountMapping.objects.filter(pk=mapping_id)
        .select_related("organization", "account_role", "account", "item", "category")
        .first()
    )
    if mapping is None:
        raise OutOfScope(_("Account mapping %(id)s does not exist.") % {"id": mapping_id})
    resolve_organization(actor, mapping.organization_id)
    return mapping


def map_inventory_role(
    *,
    actor: User,
    organization: Organization,
    role: AccountRole,
    account: Account,
    item: InventoryItem | None = None,
    category: ItemCategory | None = None,
    effective_from: datetime.date,
    effective_to: datetime.date | None = None,
) -> InventoryAccountMapping:
    """Record an item/category override, with organization authority."""
    require_organization_permission(actor, MANAGE_ACCOUNT_MAPPINGS, organization)
    with _acting_as(actor):
        return create_inventory_mapping(
            organization=organization,
            role=role,
            account=account,
            item=item,
            category=category,
            effective_from=effective_from,
            effective_to=effective_to,
        )


def close_inventory_role_mapping(
    *, actor: User, mapping_id: int, effective_to: datetime.date, reason: str = ""
) -> InventoryAccountMapping:
    mapping = _resolve_inventory_mapping(actor, mapping_id)
    require_organization_permission(actor, MANAGE_ACCOUNT_MAPPINGS, mapping.organization)
    with _acting_as(actor):
        return close_inventory_mapping(mapping=mapping, effective_to=effective_to, reason=reason)


def archive_inventory_role_mapping(
    *, actor: User, mapping_id: int, reason: str = ""
) -> InventoryAccountMapping:
    mapping = _resolve_inventory_mapping(actor, mapping_id)
    require_organization_permission(actor, MANAGE_ACCOUNT_MAPPINGS, mapping.organization)
    with _acting_as(actor):
        return archive_inventory_mapping(mapping=mapping, reason=reason)


# ---------------------------------------------------------------------------
# Opening stock documents (Task 1.3)
# ---------------------------------------------------------------------------
#
# Preparing is branch work (`create_opening_stock` at the document's branch);
# posting is organization authority (`post_opening_stock` through an
# OrganizationMembership); reversal likewise (`reverse_movement` held through
# an OrganizationMembership — undoing the ledger's starting point is not a
# branch decision). Maker-checker is enforced in the domain service on the
# recorded acts, so holding every permission changes nothing.


def visible_opening_documents(actor: User) -> QuerySet[OpeningStockDocument]:
    """
    Openings at branches where a post the caller holds carries `view_stock`.

    Provenance-scoped like the master-data screens: a viewer post in another
    organization contributes nothing here.
    """
    return (
        OpeningStockDocument.objects.filter(branch__in=branches_with_permission(actor, VIEW_STOCK))
        .select_related(
            "organization",
            "branch",
            "created_by",
            "submitted_by",
            "posted_by",
            "reversed_by",
            "stock_entry",
            "journal_entry",
            "reversal_journal_entry",
        )
        .order_by("-created_at", "-id")
    )


def resolve_opening_document(actor: User, document_id: int) -> OpeningStockDocument:
    """A document id resolved with its caller — foreign or absent is one 404."""
    document = visible_opening_documents(actor).filter(pk=document_id).first()
    if document is None:
        raise OutOfScope(_("Opening document %(id)s does not exist.") % {"id": document_id})
    return document


def create_opening(
    *,
    actor: User,
    organization: Organization,
    branch: Branch,
    cutoff_at: datetime.datetime,
    evidence_reference: str,
    narration: str = "",
) -> OpeningStockDocument:
    require_branch_permission(actor, CREATE_OPENING_STOCK, branch)
    with _acting_as(actor):
        return opening.create_opening_document(
            organization=organization,
            branch=branch,
            cutoff_at=cutoff_at,
            evidence_reference=evidence_reference,
            narration=narration,
        )


def update_opening(
    *,
    actor: User,
    document: OpeningStockDocument,
    cutoff_at: datetime.datetime | None = None,
    evidence_reference: str | None = None,
    narration: str | None = None,
) -> OpeningStockDocument:
    require_branch_permission(actor, CREATE_OPENING_STOCK, document.branch)
    with _acting_as(actor):
        return opening.update_opening_document(
            document=document,
            cutoff_at=cutoff_at,
            evidence_reference=evidence_reference,
            narration=narration,
        )


def delete_opening(*, actor: User, document: OpeningStockDocument, reason: str = "") -> None:
    require_branch_permission(actor, CREATE_OPENING_STOCK, document.branch)
    with _acting_as(actor):
        opening.delete_opening_document(document=document, reason=reason)


def add_opening_line(
    *, actor: User, document: OpeningStockDocument, line: opening.OpeningLineInput
) -> OpeningStockLine:
    require_branch_permission(actor, CREATE_OPENING_STOCK, document.branch)
    with _acting_as(actor):
        return opening.add_opening_line(document=document, line=line)


def remove_opening_line(*, actor: User, line: OpeningStockLine, reason: str = "") -> None:
    require_branch_permission(actor, CREATE_OPENING_STOCK, line.document.branch)
    with _acting_as(actor):
        opening.delete_opening_line(line=line, reason=reason)


def replace_opening_lines(
    *,
    actor: User,
    document: OpeningStockDocument,
    lines: Sequence[opening.OpeningLineInput],
) -> OpeningStockDocument:
    require_branch_permission(actor, CREATE_OPENING_STOCK, document.branch)
    with _acting_as(actor):
        return opening.replace_opening_lines(document=document, lines=lines)


def submit_opening(*, actor: User, document: OpeningStockDocument) -> OpeningStockDocument:
    require_branch_permission(actor, CREATE_OPENING_STOCK, document.branch)
    with _acting_as(actor):
        return opening.submit_opening_document(document=document)


def return_opening_to_draft(
    *, actor: User, document: OpeningStockDocument, reason: str
) -> OpeningStockDocument:
    require_branch_permission(actor, CREATE_OPENING_STOCK, document.branch)
    with _acting_as(actor):
        return opening.return_opening_to_draft(document=document, reason=reason)


def post_opening(*, actor: User, document: OpeningStockDocument) -> OpeningStockDocument:
    """
    Post to both ledgers. Organization authority — setting the ledger's
    starting point covers every branch's figures at once — and maker-checker
    on top of it: the submitter is refused whatever they hold.
    """
    require_organization_permission(actor, POST_OPENING_STOCK, document.organization)
    with _acting_as(actor):
        return opening.post_opening_document(document=document)


def reverse_opening(
    *, actor: User, document: OpeningStockDocument, reason: str
) -> OpeningStockDocument:
    require_organization_permission(actor, REVERSE_MOVEMENT, document.organization)
    with _acting_as(actor):
        return opening.reverse_opening_document(document=document, reason=reason)


# ---------------------------------------------------------------------------
# Operational documents: receipt, issue, return-in (Task 1.4)
# ---------------------------------------------------------------------------
#
# Every act is answered at the **warehouse** the goods move through, because
# that is what a movement names and what custody actually means. Preparing a
# draft needs `create_draft_movement` at the branch; posting needs the
# type's own permission at the warehouse; reversing needs `reverse_movement`.

#: Which permission each document type's posting requires.
DOCUMENT_PERMISSION: dict[str, str] = {
    InventoryDocumentType.ISSUE: POST_ISSUE,
    InventoryDocumentType.WASTE: POST_WASTE,
}


def visible_documents(
    actor: User, *, document_type: str | None = None
) -> QuerySet[InventoryMovementDocument]:
    """
    Operational documents at warehouses the caller has custody of.

    Scoped by `accessible_warehouses` rather than by branch, so a membership
    restricted to one warehouse sees that warehouse's documents and not the
    branch's whole traffic.
    """
    if not actor.is_authenticated or not actor.is_active:
        return InventoryMovementDocument.objects.none()
    if not actor.has_perm(VIEW_STOCK):
        return InventoryMovementDocument.objects.none()
    documents = InventoryMovementDocument.objects.filter(
        warehouse__in=accessible_warehouses(actor)
    ).select_related(
        "organization",
        "branch",
        "warehouse",
        "cost_center",
        "created_by",
        "posted_by",
        "reversed_by",
        "stock_entry",
        "journal_entry",
        "reversal_journal_entry",
    )
    if document_type is not None:
        documents = documents.filter(document_type=document_type)
    return documents.order_by("-created_at", "-id")


def resolve_document(
    actor: User, document_id: int, *, document_type: str | None = None
) -> InventoryMovementDocument:
    """A document id resolved with its caller — foreign or absent is one 404."""
    document = visible_documents(actor, document_type=document_type).filter(pk=document_id).first()
    if document is None:
        raise OutOfScope(_("Document %(id)s does not exist.") % {"id": document_id})
    return document


def resolve_document_line(actor: User, line_id: int) -> InventoryMovementDocumentLine:
    """A line id resolved through its document's scope."""
    line = (
        InventoryMovementDocumentLine.objects.filter(
            pk=line_id, document__warehouse__in=accessible_warehouses(actor)
        )
        .select_related("document", "document__warehouse", "item", "lot")
        .first()
    )
    if line is None:
        raise OutOfScope(_("Line %(id)s does not exist.") % {"id": line_id})
    return line


def create_document(
    *,
    actor: User,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    document_type: str,
    effective_at: datetime.datetime,
    evidence_reference: str,
    narration: str = "",
    cost_center: Any = None,
) -> InventoryMovementDocument:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, warehouse)
    with _acting_as(actor):
        return operations.create_document(
            organization=organization,
            branch=branch,
            warehouse=warehouse,
            document_type=document_type,
            effective_at=effective_at,
            evidence_reference=evidence_reference,
            narration=narration,
            cost_center=cost_center,
        )


def update_document(
    *,
    actor: User,
    document: InventoryMovementDocument,
    effective_at: datetime.datetime | None = None,
    evidence_reference: str | None = None,
    narration: str | None = None,
    cost_center: Any = None,
    clear_cost_center: bool = False,
) -> InventoryMovementDocument:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, document.warehouse)
    with _acting_as(actor):
        return operations.update_document(
            document=document,
            effective_at=effective_at,
            evidence_reference=evidence_reference,
            narration=narration,
            cost_center=cost_center,
            clear_cost_center=clear_cost_center,
        )


def delete_document(*, actor: User, document: InventoryMovementDocument, reason: str = "") -> None:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, document.warehouse)
    with _acting_as(actor):
        operations.delete_document(document=document, reason=reason)


def add_document_line(
    *,
    actor: User,
    document: InventoryMovementDocument,
    line: operations.DocumentLineInput,
) -> Any:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, document.warehouse)
    with _acting_as(actor):
        return operations.add_line(document=document, line=line)


def remove_document_line(*, actor: User, line: Any, reason: str = "") -> None:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, line.document.warehouse)
    with _acting_as(actor):
        operations.delete_line(line=line, reason=reason)


def replace_document_lines(
    *,
    actor: User,
    document: InventoryMovementDocument,
    lines: Sequence[operations.DocumentLineInput],
) -> InventoryMovementDocument:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, document.warehouse)
    with _acting_as(actor):
        return operations.replace_lines(document=document, lines=lines)


def post_document(*, actor: User, document: InventoryMovementDocument) -> InventoryMovementDocument:
    """
    Post at the warehouse, with the permission this document type needs.

    A return is authorized as its own act rather than as an issue done
    backwards: putting stock back is a different decision from taking it out,
    and a deployment that trusts one and not the other must be able to say so.
    """
    permission = DOCUMENT_PERMISSION.get(document.document_type)
    if permission is None:  # pragma: no cover - the model constrains the type
        raise ValidationError(
            _("%(type)s has no permission mapping."),
            code="unmapped_document_type",
            params={"type": document.document_type},
        )
    require_warehouse_permission(actor, permission, document.warehouse)
    with _acting_as(actor):
        return operations.post_document(document=document)


def reverse_document(
    *, actor: User, document: InventoryMovementDocument, reason: str
) -> InventoryMovementDocument:
    require_warehouse_permission(actor, REVERSE_MOVEMENT, document.warehouse)
    with _acting_as(actor):
        return operations.reverse_document(document=document, reason=reason)


# ---------------------------------------------------------------------------
# Transfers, receipts and shortages (Task 1.5)
# ---------------------------------------------------------------------------
#
# Three different authorities, because they are three different acts.
#
# **Dispatch** is answered at the source warehouse — that is where custody
# ends — plus *reach* to the destination, so nobody can push goods into a
# warehouse they are not entitled to know about. Within one branch the
# destination's own `post_transfer` is required as well: moving stock between
# two stores of one branch is an operation at both of them (§F).
#
# **Receipt** is answered at the destination warehouse alone. Taking delivery
# must not imply any authority over the source: the receiving storekeeper
# confirms what arrived and nothing else.
#
# **Shortage closure** is answered at the **source branch**, with its own
# sensitive permission. The goods are no longer in anybody's warehouse, and
# the branch that dispatched them is the one whose books carry the loss —
# so warehouse custody is the wrong question and a storekeeper is the wrong
# person to answer it.


def visible_transfers(actor: User) -> QuerySet[StockTransfer]:
    """
    Transfers the caller can see from either end.

    Both ends, deliberately: a destination storekeeper must be able to look up
    what is coming to them, and a source manager must be able to see where
    their goods went. Neither view grants any authority over the other end.
    """
    if not actor.is_authenticated or not actor.is_active:
        return StockTransfer.objects.none()
    if not actor.has_perm(VIEW_STOCK):
        return StockTransfer.objects.none()
    reachable = accessible_warehouses(actor)
    return (
        StockTransfer.objects.filter(
            Q(source_warehouse__in=reachable) | Q(destination_warehouse__in=reachable)
        )
        .select_related(
            "organization",
            "source_warehouse",
            "source_warehouse__branch",
            "destination_warehouse",
            "destination_warehouse__branch",
            "created_by",
            "dispatched_by",
            "reversed_by",
            "stock_entry",
            "journal_entry",
            "reversal_journal_entry",
        )
        .distinct()
        .order_by("-created_at", "-id")
    )


def resolve_transfer(actor: User, transfer_id: int) -> StockTransfer:
    """A transfer id resolved with its caller — foreign or absent is one 404."""
    transfer = visible_transfers(actor).filter(pk=transfer_id).first()
    if transfer is None:
        raise OutOfScope(_("Transfer %(id)s does not exist.") % {"id": transfer_id})
    return transfer


def resolve_transfer_line(actor: User, line_id: int) -> StockTransferLine:
    """A transfer line id resolved through its transfer's scope."""
    line = (
        StockTransferLine.objects.filter(pk=line_id, transfer__in=visible_transfers(actor))
        .select_related("transfer", "item", "item__base_unit", "lot", "package_conversion")
        .first()
    )
    if line is None:
        raise OutOfScope(_("Transfer line %(id)s does not exist.") % {"id": line_id})
    return line


def resolve_receipt(
    actor: User, receipt_id: int, *, transfer: StockTransfer | None = None
) -> StockTransferReceipt:
    """
    A receipt id resolved through its transfer's scope.

    `transfer` constrains the route: a receipt id submitted under another
    transfer's URL is a 404, not somebody else's receipt returned politely.
    """
    receipts = StockTransferReceipt.objects.filter(transfer__in=visible_transfers(actor))
    if transfer is not None:
        receipts = receipts.filter(transfer=transfer)
    receipt = (
        receipts.select_related(
            "transfer",
            "transfer__source_warehouse",
            "transfer__source_warehouse__branch",
            "transfer__destination_warehouse",
            "transfer__destination_warehouse__branch",
            "received_by",
            "reversed_by",
        )
        .filter(pk=receipt_id)
        .first()
    )
    if receipt is None:
        raise OutOfScope(_("Transfer receipt %(id)s does not exist.") % {"id": receipt_id})
    return receipt


def resolve_shortage(
    actor: User, shortage_id: int, *, transfer: StockTransfer | None = None
) -> StockTransferShortage:
    """A shortage id resolved through its transfer's scope, route-constrained."""
    shortages = StockTransferShortage.objects.filter(transfer__in=visible_transfers(actor))
    if transfer is not None:
        shortages = shortages.filter(transfer=transfer)
    shortage = (
        shortages.select_related(
            "transfer",
            "transfer__source_warehouse",
            "transfer__source_warehouse__branch",
            "cost_center",
            "closed_by",
            "reversed_by",
        )
        .filter(pk=shortage_id)
        .first()
    )
    if shortage is None:
        raise OutOfScope(_("Transfer shortage %(id)s does not exist.") % {"id": shortage_id})
    return shortage


def _authorize_dispatch_side(actor: User, *, source: Warehouse, destination: Warehouse) -> None:
    """`post_transfer` at the source, reach at the destination, both within a branch."""
    require_warehouse_permission(actor, POST_TRANSFER, source)
    if source.branch_id == destination.branch_id:
        require_warehouse_permission(actor, POST_TRANSFER, destination)
        return
    # Cross-branch: authority stays with the dispatching side, but the actor
    # must still reach the destination through the ordinary warehouse-scope
    # design. An unscoped selector offering every foreign warehouse in the
    # organization is exactly what §F refuses.
    if not can_access_warehouse(actor, destination):
        raise OutOfScope(_("Warehouse %(id)s does not exist.") % {"id": destination.pk})


def create_transfer(
    *,
    actor: User,
    organization: Organization,
    source_warehouse: Warehouse,
    destination_warehouse: Warehouse,
    effective_at: datetime.datetime,
    evidence_reference: str,
    narration: str = "",
) -> StockTransfer:
    _authorize_dispatch_side(actor, source=source_warehouse, destination=destination_warehouse)
    with _acting_as(actor):
        return transfers.create_transfer(
            organization=organization,
            source_warehouse=source_warehouse,
            destination_warehouse=destination_warehouse,
            effective_at=effective_at,
            evidence_reference=evidence_reference,
            narration=narration,
        )


def update_transfer(
    *,
    actor: User,
    transfer: StockTransfer,
    effective_at: datetime.datetime | None = None,
    evidence_reference: str | None = None,
    narration: str | None = None,
) -> StockTransfer:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, transfer.source_warehouse)
    with _acting_as(actor):
        return transfers.update_transfer(
            transfer=transfer,
            effective_at=effective_at,
            evidence_reference=evidence_reference,
            narration=narration,
        )


def delete_transfer(*, actor: User, transfer: StockTransfer, reason: str = "") -> None:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, transfer.source_warehouse)
    with _acting_as(actor):
        transfers.delete_transfer(transfer=transfer, reason=reason)


def add_transfer_line(
    *, actor: User, transfer: StockTransfer, line: transfers.TransferLineInput
) -> StockTransferLine:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, transfer.source_warehouse)
    with _acting_as(actor):
        return transfers.add_transfer_line(transfer=transfer, line=line)


def remove_transfer_line(*, actor: User, line: StockTransferLine, reason: str = "") -> None:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, line.transfer.source_warehouse)
    with _acting_as(actor):
        transfers.delete_transfer_line(line=line, reason=reason)


def replace_transfer_lines(
    *,
    actor: User,
    transfer: StockTransfer,
    lines: Sequence[transfers.TransferLineInput],
) -> StockTransfer:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, transfer.source_warehouse)
    with _acting_as(actor):
        return transfers.replace_transfer_lines(transfer=transfer, lines=lines)


def dispatch_transfer(*, actor: User, transfer: StockTransfer) -> StockTransfer:
    _authorize_dispatch_side(
        actor,
        source=transfer.source_warehouse,
        destination=transfer.destination_warehouse,
    )
    with _acting_as(actor):
        return transfers.dispatch_transfer(transfer=transfer)


def reverse_dispatch(*, actor: User, transfer: StockTransfer, reason: str) -> StockTransfer:
    require_warehouse_permission(actor, REVERSE_MOVEMENT, transfer.source_warehouse)
    with _acting_as(actor):
        return transfers.reverse_dispatch(transfer=transfer, reason=reason)


def create_transfer_receipt(
    *,
    actor: User,
    transfer: StockTransfer,
    effective_at: datetime.datetime,
    evidence_reference: str,
    narration: str = "",
) -> StockTransferReceipt:
    require_warehouse_permission(actor, CREATE_DRAFT_MOVEMENT, transfer.destination_warehouse)
    with _acting_as(actor):
        return transfers.create_receipt(
            transfer=transfer,
            effective_at=effective_at,
            evidence_reference=evidence_reference,
            narration=narration,
        )


def update_transfer_receipt(
    *,
    actor: User,
    receipt: StockTransferReceipt,
    effective_at: datetime.datetime | None = None,
    evidence_reference: str | None = None,
    narration: str | None = None,
) -> StockTransferReceipt:
    require_warehouse_permission(
        actor, CREATE_DRAFT_MOVEMENT, receipt.transfer.destination_warehouse
    )
    with _acting_as(actor):
        return transfers.update_receipt(
            receipt=receipt,
            effective_at=effective_at,
            evidence_reference=evidence_reference,
            narration=narration,
        )


def delete_transfer_receipt(
    *, actor: User, receipt: StockTransferReceipt, reason: str = ""
) -> None:
    require_warehouse_permission(
        actor, CREATE_DRAFT_MOVEMENT, receipt.transfer.destination_warehouse
    )
    with _acting_as(actor):
        transfers.delete_receipt(receipt=receipt, reason=reason)


def replace_transfer_receipt_lines(
    *,
    actor: User,
    receipt: StockTransferReceipt,
    lines: Sequence[transfers.ReceiptLineInput],
) -> StockTransferReceipt:
    require_warehouse_permission(
        actor, CREATE_DRAFT_MOVEMENT, receipt.transfer.destination_warehouse
    )
    with _acting_as(actor):
        return transfers.replace_receipt_lines(receipt=receipt, lines=lines)


def post_transfer_receipt(*, actor: User, receipt: StockTransferReceipt) -> StockTransferReceipt:
    """
    Post at the **destination** warehouse, and nowhere else.

    Deliberately asks nothing about the source: a branch taking delivery
    confirms what arrived, and giving that act authority over the dispatching
    branch's stock would make every receiving storekeeper a reader of another
    branch's warehouse.
    """
    require_warehouse_permission(actor, POST_TRANSFER, receipt.transfer.destination_warehouse)
    with _acting_as(actor):
        return transfers.post_receipt(receipt=receipt)


def reverse_transfer_receipt(
    *, actor: User, receipt: StockTransferReceipt, reason: str
) -> StockTransferReceipt:
    require_warehouse_permission(actor, REVERSE_MOVEMENT, receipt.transfer.destination_warehouse)
    with _acting_as(actor):
        return transfers.reverse_receipt(receipt=receipt, reason=reason)


def create_transfer_shortage(
    *,
    actor: User,
    transfer: StockTransfer,
    effective_at: datetime.datetime,
    reason: str,
    evidence_reference: str,
    cost_center: Any,
) -> StockTransferShortage:
    require_branch_permission(actor, CLOSE_TRANSFER_SHORTAGE, transfer.source_warehouse.branch)
    with _acting_as(actor):
        return transfers.create_shortage(
            transfer=transfer,
            effective_at=effective_at,
            reason=reason,
            evidence_reference=evidence_reference,
            cost_center=cost_center,
        )


def delete_transfer_shortage(
    *, actor: User, shortage: StockTransferShortage, reason: str = ""
) -> None:
    require_branch_permission(
        actor, CLOSE_TRANSFER_SHORTAGE, shortage.transfer.source_warehouse.branch
    )
    with _acting_as(actor):
        transfers.delete_shortage(shortage=shortage, reason=reason)


def post_transfer_shortage(
    *, actor: User, shortage: StockTransferShortage
) -> StockTransferShortage:
    """Turn missing stock into an expense — the most sensitive act in the module."""
    require_branch_permission(
        actor, CLOSE_TRANSFER_SHORTAGE, shortage.transfer.source_warehouse.branch
    )
    with _acting_as(actor):
        return transfers.post_shortage(shortage=shortage)


def reverse_transfer_shortage(
    *, actor: User, shortage: StockTransferShortage, reason: str
) -> StockTransferShortage:
    require_branch_permission(actor, REVERSE_MOVEMENT, shortage.transfer.source_warehouse.branch)
    with _acting_as(actor):
        return transfers.reverse_shortage(shortage=shortage, reason=reason)


# ---------------------------------------------------------------------------
# Waste, counts and adjustments (Task 1.6)
# ---------------------------------------------------------------------------
#
# Three authorities, because they are three different kinds of act.
#
# **Waste** is a custody act at one warehouse, authorized exactly as an issue
# is, with its own permission because destroying stock is a different decision
# from consuming it.
#
# **A count** splits in two on purpose. Conducting is warehouse custody; a
# storekeeper does it. Approving is a branch-level authority over the figures,
# which is why an accounting manager holds it and a storekeeper does not — and
# why the same person cannot do both to one count.
#
# **An adjustment** is answered at the branch, not the warehouse: it is a
# correction to the books rather than a movement of goods, and the authority
# that owns the books is the branch's.


# --- Physical counts --------------------------------------------------------


def visible_counts(actor: User) -> QuerySet[StockCount]:
    """Counts at warehouses the caller has custody of, or reach to."""
    if not actor.is_authenticated or not actor.is_active:
        return StockCount.objects.none()
    if not actor.has_perm(VIEW_STOCK):
        return StockCount.objects.none()
    return (
        StockCount.objects.filter(warehouse__in=accessible_warehouses(actor))
        .select_related(
            "organization",
            "branch",
            "warehouse",
            "cost_center",
            "conducted_by",
            "submitted_by",
            "approved_by",
            "cancelled_by",
            "reversed_by",
            "stock_entry",
            "journal_entry",
        )
        .order_by("-created_at", "-id")
    )


def resolve_count(actor: User, count_id: int) -> StockCount:
    count = visible_counts(actor).filter(pk=count_id).first()
    if count is None:
        raise OutOfScope(_("Count %(id)s does not exist.") % {"id": count_id})
    return count


def resolve_count_line(actor: User, line_id: int, *, count: StockCount | None = None) -> Any:
    """
    A count line resolved through its count's scope.

    `count` narrows the lookup to one parent so a line id submitted through
    another count's route is a 404 rather than a line somebody could write
    through the wrong document (§AD).
    """
    from apps.inventory.models import StockCountLine

    lines = StockCountLine.objects.filter(
        pk=line_id, count__warehouse__in=accessible_warehouses(actor)
    )
    if count is not None:
        lines = lines.filter(count=count)
    line = lines.select_related("count", "count__warehouse", "item", "lot").first()
    if line is None:
        raise OutOfScope(_("Count line %(id)s does not exist.") % {"id": line_id})
    return line


def create_stock_count(
    *,
    actor: User,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    reference: str = "",
    reason: str = "",
    cost_center: Any = None,
) -> StockCount:
    require_warehouse_permission(actor, CONDUCT_STOCK_COUNT, warehouse)
    with _acting_as(actor):
        return counts.create_count(
            organization=organization,
            branch=branch,
            warehouse=warehouse,
            reference=reference,
            reason=reason,
            cost_center=cost_center,
        )


def update_stock_count(
    *,
    actor: User,
    count: StockCount,
    reference: str = "",
    reason: str = "",
    cost_center: Any = None,
) -> StockCount:
    require_warehouse_permission(actor, CONDUCT_STOCK_COUNT, count.warehouse)
    with _acting_as(actor):
        return counts.update_count(
            count=count, reference=reference, reason=reason, cost_center=cost_center
        )


def delete_stock_count(*, actor: User, count: StockCount, reason: str = "") -> None:
    require_warehouse_permission(actor, CONDUCT_STOCK_COUNT, count.warehouse)
    with _acting_as(actor):
        counts.delete_count(count=count, reason=reason)


def start_stock_count(
    *, actor: User, count: StockCount, effective_at: datetime.datetime | None = None
) -> StockCount:
    require_warehouse_permission(actor, CONDUCT_STOCK_COUNT, count.warehouse)
    with _acting_as(actor):
        return counts.start_count(count=count, effective_at=effective_at)


def record_stock_counts(
    *, actor: User, count: StockCount, entries: list[counts.CountEntry]
) -> list[Any]:
    require_warehouse_permission(actor, CONDUCT_STOCK_COUNT, count.warehouse)
    with _acting_as(actor):
        return counts.record_counts(count=count, entries=entries)


def add_unexpected_count_line(
    *,
    actor: User,
    count: StockCount,
    item: InventoryItem,
    lot: Any = None,
    base_quantity: Any = None,
    package_conversion: Any = None,
    entered_package_quantity: Any = None,
    measured_base_quantity: Any = None,
    note: str = "",
) -> Any:
    require_warehouse_permission(actor, CONDUCT_STOCK_COUNT, count.warehouse)
    with _acting_as(actor):
        return counts.add_unexpected_line(
            count=count,
            item=item,
            lot=lot,
            base_quantity=base_quantity,
            package_conversion=package_conversion,
            entered_package_quantity=entered_package_quantity,
            measured_base_quantity=measured_base_quantity,
            note=note,
        )


def submit_stock_count(*, actor: User, count: StockCount) -> StockCount:
    require_warehouse_permission(actor, CONDUCT_STOCK_COUNT, count.warehouse)
    with _acting_as(actor):
        return counts.submit_count(count=count)


def approve_stock_count(
    *, actor: User, count: StockCount, costs: list[counts.ApprovedCost] | None = None
) -> StockCount:
    """
    Approve and post, at the **branch**.

    Branch scope rather than warehouse: approval is a judgement about the
    figures, and the accounting manager who makes it holds no custody of any
    shelf. Supplying an approved unit cost also needs `view_valuation` — a
    person who cannot see what stock costs cannot meaningfully decide what
    found stock is worth.
    """
    require_branch_permission(actor, APPROVE_STOCK_COUNT, count.branch)
    if costs:
        require_branch_permission(actor, VIEW_VALUATION, count.branch)
    with _acting_as(actor):
        return counts.approve_count(count=count, costs=costs)


def cancel_stock_count(*, actor: User, count: StockCount, reason: str) -> StockCount:
    require_warehouse_permission(actor, CONDUCT_STOCK_COUNT, count.warehouse)
    with _acting_as(actor):
        return counts.cancel_count(count=count, reason=reason)


def reverse_stock_count(*, actor: User, count: StockCount, reason: str) -> StockCount:
    require_branch_permission(actor, REVERSE_MOVEMENT, count.branch)
    with _acting_as(actor):
        return counts.reverse_count(count=count, reason=reason)


def blind_count_sheet(*, actor: User, count: StockCount) -> list[dict[str, Any]]:
    """
    The counting sheet, carrying nothing the conductor must not see.

    `view_valuation` deliberately makes **no** difference here. A manager who
    can see cost everywhere else still gets a blind sheet, because the control
    is over what the person doing the counting knows at the moment they count —
    not over what they are otherwise entitled to look up (§K).
    """
    require_warehouse_permission(actor, CONDUCT_STOCK_COUNT, count.warehouse)
    return counts.blind_lines(count)


# --- Manual adjustments -----------------------------------------------------


def visible_adjustments(actor: User) -> QuerySet[InventoryAdjustmentDocument]:
    if not actor.is_authenticated or not actor.is_active:
        return InventoryAdjustmentDocument.objects.none()
    if not actor.has_perm(VIEW_STOCK):
        return InventoryAdjustmentDocument.objects.none()
    return (
        InventoryAdjustmentDocument.objects.filter(warehouse__in=accessible_warehouses(actor))
        .select_related(
            "organization",
            "branch",
            "warehouse",
            "cost_center",
            "created_by",
            "posted_by",
            "reversed_by",
            "stock_entry",
            "journal_entry",
            "reversal_journal_entry",
        )
        .order_by("-created_at", "-id")
    )


def resolve_adjustment(actor: User, document_id: int) -> InventoryAdjustmentDocument:
    document = visible_adjustments(actor).filter(pk=document_id).first()
    if document is None:
        raise OutOfScope(_("Adjustment %(id)s does not exist.") % {"id": document_id})
    return document


def resolve_adjustment_line(
    actor: User, line_id: int, *, document: InventoryAdjustmentDocument | None = None
) -> Any:
    lines = InventoryAdjustmentLine.objects.filter(
        pk=line_id, document__warehouse__in=accessible_warehouses(actor)
    )
    if document is not None:
        lines = lines.filter(document=document)
    line = lines.select_related("document", "document__warehouse", "item", "lot").first()
    if line is None:
        raise OutOfScope(_("Adjustment line %(id)s does not exist.") % {"id": line_id})
    return line


def create_adjustment(
    *,
    actor: User,
    organization: Organization,
    branch: Branch,
    warehouse: Warehouse,
    effective_at: datetime.datetime,
    evidence_reference: str,
    reason: str,
    cost_center: Any = None,
) -> InventoryAdjustmentDocument:
    require_branch_permission(actor, POST_ADJUSTMENT, branch)
    with _acting_as(actor):
        return adjustments.create_adjustment(
            organization=organization,
            branch=branch,
            warehouse=warehouse,
            effective_at=effective_at,
            evidence_reference=evidence_reference,
            reason=reason,
            cost_center=cost_center,
        )


def update_adjustment(
    *,
    actor: User,
    document: InventoryAdjustmentDocument,
    effective_at: datetime.datetime | None = None,
    evidence_reference: str | None = None,
    reason: str | None = None,
    cost_center: Any = None,
) -> InventoryAdjustmentDocument:
    require_branch_permission(actor, POST_ADJUSTMENT, document.branch)
    with _acting_as(actor):
        return adjustments.update_adjustment(
            document=document,
            effective_at=effective_at,
            evidence_reference=evidence_reference,
            reason=reason,
            cost_center=cost_center,
        )


def delete_adjustment(
    *, actor: User, document: InventoryAdjustmentDocument, reason: str = ""
) -> None:
    require_branch_permission(actor, POST_ADJUSTMENT, document.branch)
    with _acting_as(actor):
        adjustments.delete_adjustment(document=document, reason=reason)


def add_adjustment_line(
    *,
    actor: User,
    document: InventoryAdjustmentDocument,
    line: adjustments.AdjustmentLineInput,
) -> Any:
    require_branch_permission(actor, POST_ADJUSTMENT, document.branch)
    with _acting_as(actor):
        return adjustments.add_adjustment_line(document=document, line=line)


def delete_adjustment_line(*, actor: User, line: Any, reason: str = "") -> None:
    require_branch_permission(actor, POST_ADJUSTMENT, line.document.branch)
    with _acting_as(actor):
        adjustments.delete_adjustment_line(line=line, reason=reason)


def post_adjustment(
    *, actor: User, document: InventoryAdjustmentDocument
) -> InventoryAdjustmentDocument:
    require_branch_permission(actor, POST_ADJUSTMENT, document.branch)
    with _acting_as(actor):
        return adjustments.post_adjustment(document=document)


def reverse_adjustment(
    *, actor: User, document: InventoryAdjustmentDocument, reason: str
) -> InventoryAdjustmentDocument:
    require_branch_permission(actor, REVERSE_MOVEMENT, document.branch)
    with _acting_as(actor):
        return adjustments.reverse_adjustment(document=document, reason=reason)
