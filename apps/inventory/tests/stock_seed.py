"""
Put stock on a shelf, for tests that need some there before they start.

Until the un-invoiced receipt was withdrawn from the product, every test that
needed stock posted one. There is no inventory document that puts goods in any
more — a purchase goods receipt does it, and that belongs to Procurement — so
tests seed the way `apps.procurement.posting` does: the stock kernel for the
goods, and a balanced journal for the money.

**Both halves, or none.** Posting the movement alone would leave the inventory
book value ahead of the general ledger, and `verify_inventory_against_gl` would
then report drift on every test that used the helper — a fixture manufacturing
the very defect the reconciliation exists to catch. So this debits the control
account the stock stands in and credits goods-received-not-invoiced, which is
exactly the entry the withdrawn receipt made.

The control account is passed in rather than resolved here for the reason
procurement passes it: a balance carries the identity of the account its stock
is standing in, and an issue, a waste, a count variance and an adjustment all
credit **that** account rather than resolving a fresh one.
"""

from __future__ import annotations

import datetime
import itertools
from decimal import Decimal

from django.db import transaction

from apps.accounting.models import GOODS_RECEIVED_NOT_INVOICED, Account, SourceEvent
from apps.accounting.services import post_entry
from apps.accounting.validators import PostingLine
from apps.inventory.accounts import resolve_inventory_account
from apps.inventory.commands import post_stock_movements
from apps.inventory.ledger import MovementInput, link_journal_entry
from apps.inventory.models import (
    InventoryItem,
    InventoryLot,
    MovementType,
    StockLedgerEntry,
    Warehouse,
)
from apps.organizations.models import Organization
from apps.users.models import User

#: One per call. `post_stock_movements` refuses a repeated idempotency key
#: within an organization, and several tests seed more than once.
_SEQUENCE = itertools.count(1)


def seed_stock(
    *,
    actor: User,
    organization: Organization,
    warehouse: Warehouse,
    item: InventoryItem,
    quantity: str,
    unit_cost: str,
    control_account: Account,
    lot: InventoryLot | None = None,
    effective_at: datetime.datetime | None = None,
) -> StockLedgerEntry:
    """`quantity` of `item` standing in `warehouse` at `unit_cost` each."""
    nth = next(_SEQUENCE)
    key = f"test-seed-{nth}"
    entry = post_stock_movements(
        actor=actor,
        organization=organization,
        effects=[
            MovementInput(
                warehouse=warehouse,
                item=item,
                movement_type=MovementType.RECEIPT,
                quantity=Decimal(quantity),
                effect_key=key,
                lot=lot,
                unit_cost=Decimal(unit_cost),
                control_account=control_account,
            )
        ],
        idempotency_key=key,
        effective_at=effective_at,
        reference=key,
        reason="test stock seed",
    )

    # One transaction for the money: `link_journal_entry` takes a row lock, and
    # a caller in autocommit — a concurrency test's worker thread, say — would
    # otherwise be refused by the database rather than by anything meaningful.
    with transaction.atomic():
        clearing = resolve_inventory_account(
            organization=organization,
            role=GOODS_RECEIVED_NOT_INVOICED,
            item=None,
            on_date=entry.business_date,
        )
        value = Decimal(quantity) * Decimal(unit_cost)
        journal = post_entry(
            organization=organization,
            accounting_date=entry.business_date,
            lines=[
                PostingLine(account=control_account, branch=warehouse.branch, debit=value),
                PostingLine(account=clearing.account, branch=warehouse.branch, credit=value),
            ],
            idempotency_key=f"{key}-journal",
            document_date=entry.business_date,
            narration="test stock seed",
            source_document_type="INVENTORY_TEST_SEED",
            source_document_id=key,
            source_event=SourceEvent.POSTED,
            posting_rule_version="test-seed-v1",
        )
        link_journal_entry(entry=entry, journal=journal)
    return entry
