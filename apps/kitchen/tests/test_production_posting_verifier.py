"""
The posting verifier: what it catches, and the one thing only it can catch.

Every check here is exercised against a **planted** defect. A verifier asserted
only against correct data proves that it does not crash, which is not the claim
anybody needs from it.

The planting is done with `.update()` and raw SQL deliberately: the services
and the triggers refuse most of these shapes, which is the point — the verifier
exists for the day something reaches the database another way, and the only
honest way to test it is to put the row there another way.

## The proof that earns the module

`test_a_missing_journal_is_caught_and_a_correct_silence_is_not` is the pair
that matters. Both batches have `journal_entry_id IS NULL`. One is right and one
is wrong, they are indistinguishable by inspection, and only recomputing the
per-account nets from the movements tells them apart (RCP-112 proof 5).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction

from apps.accounting.models import Account
from apps.inventory.models import InventoryItem, Warehouse
from apps.kitchen.models import ProductionBatch, ProductionBatchStatus
from apps.kitchen.production import record_production_output
from apps.kitchen.production_posting import post_production_batch, reverse_production_batch
from apps.kitchen.production_posting_reconciliation import (
    account_nets,
    posted_batch_findings,
    verify_production,
)
from apps.organizations.models import Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db


@contextlib.contextmanager
def without_guards(*tables: str) -> Iterator[None]:
    """
    Plant a defect the way reality would, not the way a service would.

    Every shape below is refused by a trigger — that is the first line of
    defence and it works, as the first run of these tests demonstrated by
    failing on `RestrictViolation` twelve times over. So the guards are
    switched off for the planting statement and switched straight back on.

    That is not a loophole in the test; it is the **premise** of the verifier.
    A restored dump, a data migration run with `DISABLE TRIGGER`, a replication
    artefact and a hand-run `UPDATE` from a psql prompt all reach the table
    without passing a trigger. The verifier exists for exactly that day, and a
    verifier tested only against data the triggers already refuse is a verifier
    tested against nothing.
    """
    with connection.cursor() as cursor:
        # Migration 0015's consistency trigger is DEFERRABLE INITIALLY
        # DEFERRED, so the posting above leaves pending events and PostgreSQL
        # refuses to ALTER a table that has them. Flushing first is the same
        # idiom `accounting/tests/test_posting.py` uses to observe a deferred
        # constraint at all.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        for table in tables:
            cursor.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")


BATCH = "kitchen_productionbatch"
MOVEMENT = "inventory_stockmovement"
ENTRY = "inventory_stockledgerentry"


def codes(batch: ProductionBatch) -> set[str]:
    return {finding.code for finding in posted_batch_findings(batch)}


def _reread(batch: ProductionBatch) -> ProductionBatch:
    return ProductionBatch.objects.get(pk=batch.pk)


@pytest.fixture
def posted(
    posting_store: Warehouse, production_draft: ProductionBatch, manager: User
) -> ProductionBatch:
    item = production_draft.recipe.output_item
    assert item is not None
    record_production_output(
        batch=production_draft,
        entered_quantity=Decimal("40"),
        entered_unit=item.base_unit,
        actor=manager,
    )
    return post_production_batch(
        batch=ProductionBatch.objects.get(pk=production_draft.pk),
        idempotency_key="VERIFY-1",
        actor=manager,
    )


class TestACorrectPostingIsClean:
    def test_nothing_is_reported_about_a_correct_posting(self, posted: ProductionBatch) -> None:
        assert codes(posted) == set()

    def test_the_composed_verifier_covers_drafts_and_postings(
        self, posted: ProductionBatch, organization: Organization
    ) -> None:
        """One command, one answer — and no readiness noise about a posted batch."""
        findings = verify_production(organization)
        blocking = sorted({row.code for row in findings if row.is_blocking})
        assert blocking == [], blocking


class TestPlantedDefects:
    def test_the_database_refuses_to_blank_a_posted_number(self, posted: ProductionBatch) -> None:
        """
        This one never reaches the verifier, and that is the finding.

        `production_batch_posting_evidence_is_complete` refuses the row with
        the triggers already disabled, so a posted batch cannot lose its number
        even by raw SQL. The verifier still carries the check for the day a row
        arrives from a restore, but the first line of defence is the schema and
        the honest test of it is this.
        """
        with without_guards(BATCH), pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kitchen_productionbatch SET number = '' WHERE id = %s", [posted.pk]
                )

    def test_a_missing_actor_is_caught(self, posted: ProductionBatch) -> None:
        with without_guards(BATCH):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kitchen_productionbatch SET posted_by_id = NULL WHERE id = %s",
                    [posted.pk],
                )
        assert "posted_batch_has_no_actor" in codes(_reread(posted))

    def test_the_database_refuses_to_break_value_conservation(
        self, posted: ProductionBatch
    ) -> None:
        """
        RCP-034 as a property of the schema rather than of one code path.

        `production_batch_conserves_value` refuses `input_value != output_value`
        with the triggers off, so the invariant this whole task exists to keep
        cannot be broken by a bulk update, a data migration or a psql prompt.
        """
        with without_guards(BATCH), pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kitchen_productionbatch SET input_value = input_value + 1 "
                    "WHERE id = %s",
                    [posted.pk],
                )

    def test_a_consumption_value_that_drifts_from_the_movements_is_caught(
        self, posted: ProductionBatch
    ) -> None:
        """
        The pair the constraint cannot catch: both stored values move together,
        so the row stays legal and only the **movements** disagree with it.
        """
        with without_guards(BATCH):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kitchen_productionbatch "
                    "SET input_value = input_value + 1, output_value = output_value + 1 "
                    "WHERE id = %s",
                    [posted.pk],
                )
        found = codes(_reread(posted))
        assert "posted_consumption_value_mismatch" in found
        assert "posted_output_value_mismatch" in found

    def test_a_changed_output_quantity_is_caught(self, posted: ProductionBatch) -> None:
        with without_guards(BATCH):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kitchen_productionbatch "
                    "SET actual_output_base_quantity = actual_output_base_quantity + 1 WHERE id = %s",
                    [posted.pk],
                )
        assert "posted_output_quantity_mismatch" in codes(_reread(posted))

    def test_a_consumption_movement_that_no_longer_names_its_row_is_caught(
        self, posted: ProductionBatch
    ) -> None:
        """
        Deleting the movement is not the shape to plant: a `StockBalance` names
        it, so the foreign key refuses even with every trigger off. What a
        corrupt restore actually leaves is a movement whose `effect_key` no
        longer matches the consumption row it was written for, and the verifier
        must notice that the row has nothing pointing at it.
        """
        with without_guards(MOVEMENT):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE inventory_stockmovement SET effect_key = 'production-actual:stray' "
                    "WHERE entry_id = %s AND movement_type = 'PRODUCTION_OUT'",
                    [posted.stock_entry_id],
                )
        assert "posted_actual_has_no_movement" in codes(_reread(posted))

    def test_an_output_movement_of_the_wrong_type_is_caught(self, posted: ProductionBatch) -> None:
        """A posting must carry exactly one inbound, and it must be a production one."""
        with without_guards(MOVEMENT):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE inventory_stockmovement SET movement_type = 'RECEIPT' "
                    "WHERE entry_id = %s AND movement_type = 'PRODUCTION_IN'",
                    [posted.stock_entry_id],
                )
        assert "posted_batch_output_movement_count" in codes(_reread(posted))

    def test_a_broken_source_identity_is_caught(self, posted: ProductionBatch) -> None:
        with without_guards(ENTRY):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE inventory_stockledgerentry SET source_document_id = 'nonsense' "
                    "WHERE id = %s",
                    [posted.stock_entry_id],
                )
        assert "posted_batch_source_identity_mismatch" in codes(_reread(posted))

    def test_an_output_lot_for_an_untracked_item_is_caught(
        self, posted: ProductionBatch, cooked_rice: InventoryItem
    ) -> None:
        from apps.inventory.models import InventoryLot

        lot = InventoryLot.objects.create(
            organization=posted.organization, item=cooked_rice, code="STRAY"
        )
        with without_guards(BATCH):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kitchen_productionbatch SET output_lot_id = %s WHERE id = %s",
                    [lot.pk, posted.pk],
                )
        assert "posted_output_lot_not_required" in codes(_reread(posted))

    def test_a_missing_output_lot_is_caught(
        self, posted: ProductionBatch, cooked_rice: InventoryItem
    ) -> None:
        cooked_rice.tracks_lots = True
        cooked_rice.save(update_fields=["tracks_lots", "updated_at"])
        assert "posted_output_lot_missing" in codes(_reread(posted))


class TestTheJournalAndItsSilence:
    def test_a_correct_silence_is_not_a_finding(self, posted: ProductionBatch) -> None:
        """Every account nets to zero, so the absent journal is right."""
        movements = list(posted.stock_entry.movements.all())
        assert all(net == Decimal("0") for net in account_nets(movements).values())
        assert posted.journal_entry_id is None
        assert "posted_batch_journal_is_missing" not in codes(posted)

    def test_a_missing_journal_is_caught_and_a_correct_silence_is_not(
        self,
        posting_store: Warehouse,
        separate_output_account: Account,
        production_draft: ProductionBatch,
        manager: User,
    ) -> None:
        """
        The pair. Both batches end with no journal on the row; one is a defect.

        Planted by detaching a journal that legitimately existed, which is the
        shape a bad data migration or a hand-run DELETE would leave behind.
        """
        item = production_draft.recipe.output_item
        assert item is not None
        record_production_output(
            batch=production_draft,
            entered_quantity=Decimal("40"),
            entered_unit=item.base_unit,
            actor=manager,
        )
        posted = post_production_batch(
            batch=ProductionBatch.objects.get(pk=production_draft.pk),
            idempotency_key="VERIFY-J",
            actor=manager,
        )
        assert posted.journal_entry_id is not None, "the override should give it a journal"
        assert codes(posted) == set()

        with without_guards(BATCH):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kitchen_productionbatch SET journal_entry_id = NULL WHERE id = %s",
                    [posted.pk],
                )
        assert "posted_batch_journal_is_missing" in codes(_reread(posted))

    def test_a_journal_that_should_have_been_silent_is_caught(
        self, posted: ProductionBatch, kitchen_accounts: Account, manager: User
    ) -> None:
        """A journal attached to a batch whose accounts all net to zero."""
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine

        entry = post_entry(
            organization=posted.organization,
            accounting_date=posted.planned_business_date,
            lines=[
                PostingLine(
                    account=kitchen_accounts,
                    branch=posted.branch,
                    debit=Decimal("1"),
                ),
                PostingLine(
                    account=kitchen_accounts,
                    branch=posted.branch,
                    credit=Decimal("1"),
                ),
            ],
            idempotency_key="STRAY-JOURNAL",
        )
        with without_guards(BATCH):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kitchen_productionbatch SET journal_entry_id = %s WHERE id = %s",
                    [entry.pk, posted.pk],
                )
        assert "posted_batch_journal_should_be_silent" in codes(_reread(posted))


class TestReversal:
    def test_a_correct_reversal_is_clean(self, posted: ProductionBatch, manager: User) -> None:
        reversed_batch = reverse_production_batch(
            batch=posted, idempotency_key="VERIFY-REV", reason="خطأ", actor=manager
        )
        assert codes(_reread(reversed_batch)) == set()
        assert reversed_batch.status == ProductionBatchStatus.REVERSED

    def test_a_reversal_that_does_not_mirror_is_caught(
        self, posted: ProductionBatch, manager: User
    ) -> None:
        reversed_batch = reverse_production_batch(
            batch=posted, idempotency_key="VERIFY-REV-2", reason="خطأ", actor=manager
        )
        with without_guards(MOVEMENT):
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE inventory_stockmovement SET base_quantity = base_quantity + 1 "
                    "WHERE entry_id = %s AND id = ("
                    "  SELECT id FROM inventory_stockmovement WHERE entry_id = %s LIMIT 1)",
                    [
                        reversed_batch.reversal_stock_entry_id,
                        reversed_batch.reversal_stock_entry_id,
                    ],
                )
        assert "reversal_does_not_mirror_the_posting" in codes(_reread(reversed_batch))

    def test_the_database_refuses_a_reversal_without_a_reason(
        self, posted: ProductionBatch, manager: User
    ) -> None:
        """
        `production_batch_reversal_evidence_is_complete` holds who, when and why
        together, so a reversal cannot quietly lose the sentence somebody had to
        write to make it.
        """
        reversed_batch = reverse_production_batch(
            batch=posted, idempotency_key="VERIFY-REV-3", reason="خطأ", actor=manager
        )
        with without_guards(BATCH), pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kitchen_productionbatch SET reversal_reason = '' WHERE id = %s",
                    [reversed_batch.pk],
                )


class TestTheVerifierNeverRepairs:
    def test_verification_changes_nothing(
        self, posted: ProductionBatch, organization: Organization
    ) -> None:
        """
        A verifier that could change a figure it verifies would be the one place
        a discrepancy could be made to disappear.
        """
        from apps.inventory.models import StockBalance, StockMovement

        before = (
            posted.input_value,
            posted.output_value,
            posted.number,
            StockMovement.objects.count(),
            [(row.pk, row.quantity, row.value) for row in StockBalance.objects.order_by("pk")],
        )
        verify_production(organization)
        after_batch = _reread(posted)
        after = (
            after_batch.input_value,
            after_batch.output_value,
            after_batch.number,
            StockMovement.objects.count(),
            [(row.pk, row.quantity, row.value) for row in StockBalance.objects.order_by("pk")],
        )
        assert before == after
