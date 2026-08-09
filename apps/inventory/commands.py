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

from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.core.context import audit_context
from apps.inventory.ledger import MovementInput, post_stock_entry, reverse_stock_entry
from apps.inventory.models import (
    INBOUND_MOVEMENT_TYPES,
    MovementType,
    StockBalance,
    StockLedgerEntry,
    StockMovement,
)
from apps.inventory.permissions import (
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
    require_organization_permission,
    require_warehouse_permission,
)
from apps.organizations.models import Organization
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
