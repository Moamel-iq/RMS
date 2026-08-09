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
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import Account, AccountRole
from apps.accounting.permissions import MANAGE_ACCOUNT_MAPPINGS
from apps.core.context import audit_context
from apps.inventory import opening, operations
from apps.inventory.accounts import (
    archive_inventory_mapping,
    close_inventory_mapping,
    create_inventory_mapping,
)
from apps.inventory.ledger import MovementInput, post_stock_entry, reverse_stock_entry
from apps.inventory.models import (
    INBOUND_MOVEMENT_TYPES,
    InventoryAccountMapping,
    InventoryDocumentStatus,
    InventoryDocumentType,
    InventoryItem,
    InventoryMovementDocument,
    InventoryMovementDocumentLine,
    ItemCategory,
    MovementType,
    OpeningStockDocument,
    OpeningStockLine,
    StockBalance,
    StockLedgerEntry,
    StockMovement,
    Warehouse,
)
from apps.inventory.permissions import (
    CREATE_DRAFT_MOVEMENT,
    CREATE_OPENING_STOCK,
    POST_ISSUE,
    POST_OPENING_STOCK,
    POST_RECEIPT,
    POST_RETURN_IN,
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
    InventoryDocumentType.RECEIPT: POST_RECEIPT,
    InventoryDocumentType.ISSUE: POST_ISSUE,
    InventoryDocumentType.RETURN_IN: POST_RETURN_IN,
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


def returnable_issue_lines(actor: User) -> QuerySet[Any]:
    """
    Posted issue lines with something still returnable, for the return form.

    Filtered in Python on the remaining quantity because it is an aggregate
    over sibling rows; the queryset itself stays scoped in SQL.
    """
    return (
        InventoryMovementDocumentLine.objects.filter(
            document__document_type=InventoryDocumentType.ISSUE,
            document__status=InventoryDocumentStatus.POSTED,
            document__warehouse__in=accessible_warehouses(actor),
        )
        .select_related("document", "document__warehouse", "item", "item__base_unit", "lot")
        .order_by("-document__business_date", "-document_id", "sequence")
    )
