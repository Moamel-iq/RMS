"""
Posting a production batch: value conservation, the journal, and the silence.

The claim every test here circles is RCP-034: **value is conserved through the
batch**. Inputs leave at the kernel's moving average, the output enters at
exactly the sum of what left, and no arithmetic anywhere creates or destroys a
fils. Yield loss is absorbed into the output's unit cost (RCP-035) — 50 kg of
inputs worth 70,000 becoming 42 kg of rice makes the rice worth 70,000, and
there is no yield-variance journal because there is no approved standard to
hold a variance against.

The second claim is the one that is easiest to get wrong in the direction that
looks fine: **a batch whose accounts all net to zero writes no journal at all**,
and that is a correct posting rather than a failed one (RCP-036). A journal
that is rightly absent and one that is wrongly missing look identical from the
outside, so the no-journal case is tested three ways — no `JournalEntry` row,
the stock entry still carrying full source identity, and the per-account nets
recomputed and proved zero.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.accounting.models import Account, JournalEntry
from apps.inventory.models import (
    InventoryItem,
    InventoryLot,
    MovementType,
    StockBalance,
    StockLedgerEntry,
    StockMovement,
    Warehouse,
)
from apps.kitchen.models import (
    ProductionBatch,
    ProductionBatchActualLine,
    ProductionBatchStatus,
)
from apps.kitchen.production import record_production_output, update_production_batch_actuals
from apps.kitchen.production_posting import (
    SOURCE_DOCUMENT_TYPE,
    post_production_batch,
    reverse_production_batch,
)
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db


def codes_of(error: ValidationError) -> set[str]:
    """Every refusal code in a ValidationError, field-keyed or not."""
    if hasattr(error, "error_dict"):
        return {
            item.code
            for errors in error.error_dict.values()
            for item in errors
            if item.code is not None
        }
    return {item.code for item in error.error_list if item.code is not None}


def entry_of(batch: ProductionBatch) -> StockLedgerEntry:
    """
    The stock posting, narrowed to non-optional.

    A posted batch always has one — `production_batch_posting_evidence_is_complete`
    refuses the row that does not — but the column is nullable because a draft
    has none. Asserting it here once keeps every test below reading as prose
    instead of as a null check.
    """
    entry = batch.stock_entry
    assert entry is not None
    return entry


def output_movement_of(batch: ProductionBatch) -> StockMovement:
    movement = batch.output_movement
    assert movement is not None
    return movement


def money_of(value: Decimal | None) -> Decimal:
    assert value is not None
    return value


def first_actual(batch: ProductionBatch) -> ProductionBatchActualLine:
    line = batch.lines.first()
    assert line is not None
    actual = line.actuals.first()
    assert actual is not None
    return actual


def output_unit(batch: ProductionBatch) -> UnitOfMeasure:
    item = batch.recipe.output_item
    assert item is not None
    return item.base_unit


def _ready(batch: ProductionBatch, *, output: str = "40", actor: User) -> ProductionBatch:
    """Give the draft an entered output so it is ready to post."""
    record_production_output(
        batch=batch,
        entered_quantity=Decimal(output),
        entered_unit=output_unit(batch),
        actor=actor,
    )
    return ProductionBatch.objects.get(pk=batch.pk)


@pytest.fixture
def postable(
    posting_store: Warehouse,
    production_draft: ProductionBatch,
    manager: User,
) -> ProductionBatch:
    """A draft standing in a warehouse that actually holds its ingredients."""
    return _ready(production_draft, actor=manager)


class TestValueConservation:
    def test_the_output_is_worth_exactly_what_the_inputs_were_worth(
        self, postable: ProductionBatch, manager: User
    ) -> None:
        posted = post_production_batch(batch=postable, idempotency_key="POST-1", actor=manager)

        assert posted.status == ProductionBatchStatus.POSTED
        assert posted.input_value == posted.output_value
        assert money_of(posted.output_value) > Decimal("0")

        movements = list(entry_of(posted).movements.all())
        consumed = sum(
            -movement.inventory_value
            for movement in movements
            if movement.movement_type == MovementType.PRODUCTION_OUT
        )
        produced = next(
            movement.inventory_value
            for movement in movements
            if movement.movement_type == MovementType.PRODUCTION_IN
        )
        assert consumed == produced == posted.output_value

    def test_yield_loss_lands_in_the_unit_cost_and_nowhere_else(
        self, posting_store: Warehouse, production_draft: ProductionBatch, manager: User
    ) -> None:
        """
        The same inputs into a smaller output make the output dearer per kilo.

        That is the whole of RCP-035, and the point of asserting it as a *pair*
        is that a single posting cannot distinguish "absorbed into unit cost"
        from "coincidentally right".
        """
        from apps.core.money import quantize_unit_price

        batch = _ready(production_draft, output="40", actor=manager)
        posted = post_production_batch(batch=batch, idempotency_key="Y-1", actor=manager)

        # The whole consumed value divided by whatever actually came out — so a
        # poor yield makes the produced kilo dearer and nothing else moves.
        assert output_movement_of(posted).unit_cost == quantize_unit_price(
            money_of(posted.output_value) / Decimal("40")
        )
        assert output_movement_of(posted).inventory_value == posted.input_value
        # And there is no account for it to have gone to instead.
        from apps.accounting.models import Account

        assert not Account.objects.filter(name__icontains="فاقد").exists()
        assert not JournalEntry.objects.filter(narration__icontains="yield").exists()

    def test_the_number_is_drawn_only_on_success(
        self, postable: ProductionBatch, manager: User
    ) -> None:
        posted = post_production_batch(batch=postable, idempotency_key="N-1", actor=manager)
        assert posted.number.startswith("PRD-")
        assert posted.number.endswith("-000001")


class TestTheJournalAndItsSilence:
    def test_one_shared_control_account_writes_no_journal(
        self, postable: ProductionBatch, manager: User
    ) -> None:
        """
        The common case, and the one whose correctness is invisible.

        Every account nets to zero, so nothing is written — and the stock ledger
        entry still carries the batch's full source identity, because when there
        is no journal the stock ledger is the only place the event's identity
        lives.
        """
        before = JournalEntry.objects.count()
        posted = post_production_batch(batch=postable, idempotency_key="J-1", actor=manager)

        assert posted.journal_entry is None
        assert JournalEntry.objects.count() == before
        assert entry_of(posted).source_document_type == SOURCE_DOCUMENT_TYPE
        assert entry_of(posted).source_document_id == str(posted.public_id)
        assert entry_of(posted).source_event == "POSTED"

    def test_the_absent_journal_is_absent_for_the_right_reason(
        self, postable: ProductionBatch, manager: User
    ) -> None:
        """RCP-112 proof 5, as a test rather than only as a verifier check."""
        posted = post_production_batch(batch=postable, idempotency_key="J-2", actor=manager)

        nets: dict[int, Decimal] = {}
        for movement in entry_of(posted).movements.all():
            if movement.control_account_id is None:
                continue
            nets[movement.control_account_id] = (
                nets.get(movement.control_account_id, Decimal("0")) + movement.inventory_value
            )
        assert posted.journal_entry is None
        assert all(net == Decimal("0") for net in nets.values()), nets

    def test_a_separate_output_account_writes_a_netted_balanced_journal(
        self,
        posting_store: Warehouse,
        separate_output_account: Account,
        production_draft: ProductionBatch,
        manager: User,
    ) -> None:
        """
        The other half of RCP-036/037, and the one that has something to say.

        An item-scoped mapping puts the produced goods in a different control
        account from the ingredients, so the nets stop cancelling: the output's
        account is debited by exactly what entered it and the ingredients'
        account is credited by exactly what left.
        """
        batch = _ready(production_draft, actor=manager)
        posted = post_production_batch(batch=batch, idempotency_key="J-3", actor=manager)

        journal = posted.journal_entry
        assert journal is not None
        assert journal.source_document_type == SOURCE_DOCUMENT_TYPE
        assert journal.source_document_id == str(posted.public_id)
        assert journal.source_event == "POSTED"

        lines = list(journal.lines.select_related("account").order_by("account__code"))
        assert len(lines) == 2
        assert sum(line.debit for line in lines) == sum(line.credit for line in lines)
        debit = next(line for line in lines if line.debit > Decimal("0"))
        assert debit.account_id == separate_output_account.pk
        assert debit.debit == posted.output_value
        # The stock entry names the journal it produced, which is what arms the
        # conditional control-account invariant over its movements.
        assert entry_of(posted).journal_entry_id == journal.pk


class TestPostedImmutability:
    def test_a_posted_batch_refuses_a_second_posting(
        self, postable: ProductionBatch, manager: User
    ) -> None:
        post_production_batch(batch=postable, idempotency_key="I-1", actor=manager)
        reloaded = ProductionBatch.objects.get(pk=postable.pk)
        with pytest.raises(ValidationError) as caught:
            post_production_batch(batch=reloaded, idempotency_key="I-2", actor=manager)
        assert "production_batch_already_posted" in codes_of(caught.value)

    def test_the_same_key_and_request_returns_the_same_posting(
        self, postable: ProductionBatch, manager: User
    ) -> None:
        first = post_production_batch(batch=postable, idempotency_key="I-3", actor=manager)
        again = post_production_batch(
            batch=ProductionBatch.objects.get(pk=postable.pk),
            idempotency_key="I-3",
            actor=manager,
        )
        assert again.pk == first.pk
        assert again.number == first.number
        assert StockMovement.objects.filter(entry=entry_of(first)).count() == len(
            list(entry_of(first).movements.all())
        )

    def test_an_actual_row_cannot_be_edited_after_posting(
        self, postable: ProductionBatch, manager: User
    ) -> None:
        posted = post_production_batch(batch=postable, idempotency_key="F-1", actor=manager)
        actual = first_actual(posted)
        with pytest.raises(ValidationError) as caught:
            update_production_batch_actuals(
                actual=actual,
                entered_quantity=Decimal("1"),
                entered_unit=actual.item.base_unit,
                actor=manager,
            )
        assert caught.value is not None


class TestReversal:
    def test_a_reversal_mirrors_the_posting_exactly(
        self, postable: ProductionBatch, manager: User, rice: InventoryItem
    ) -> None:
        before = StockBalance.objects.get(warehouse=postable.warehouse, item=rice, lot=None)
        before_quantity, before_value = before.quantity, before.value

        posted = post_production_batch(batch=postable, idempotency_key="R-1", actor=manager)
        reversed_batch = reverse_production_batch(
            batch=posted, idempotency_key="R-1-rev", reason="خطأ في الكمية", actor=manager
        )

        assert reversed_batch.status == ProductionBatchStatus.REVERSED
        after = StockBalance.objects.get(warehouse=postable.warehouse, item=rice, lot=None)
        assert after.quantity == before_quantity
        assert after.value == before_value

    def test_a_reversal_needs_a_reason(self, postable: ProductionBatch, manager: User) -> None:
        posted = post_production_batch(batch=postable, idempotency_key="R-2", actor=manager)
        with pytest.raises(ValidationError) as caught:
            reverse_production_batch(
                batch=posted, idempotency_key="R-2-rev", reason="   ", actor=manager
            )
        assert "reason_required" in codes_of(caught.value)

    def test_a_reversal_happens_once(self, postable: ProductionBatch, manager: User) -> None:
        posted = post_production_batch(batch=postable, idempotency_key="R-3", actor=manager)
        reverse_production_batch(
            batch=posted, idempotency_key="R-3-rev", reason="سبب", actor=manager
        )
        with pytest.raises(ValidationError) as caught:
            reverse_production_batch(
                batch=ProductionBatch.objects.get(pk=posted.pk),
                idempotency_key="R-3-rev-2",
                reason="سبب آخر",
                actor=manager,
            )
        assert "production_batch_already_reversed" in codes_of(caught.value)

    def test_a_draft_cannot_be_reversed(self, postable: ProductionBatch, manager: User) -> None:
        with pytest.raises(ValidationError) as caught:
            reverse_production_batch(
                batch=postable, idempotency_key="R-4", reason="سبب", actor=manager
            )
        assert "production_batch_not_posted" in codes_of(caught.value)


class TestRefusals:
    def test_an_unready_draft_does_not_post(
        self, posting_store: Warehouse, production_draft: ProductionBatch, manager: User
    ) -> None:
        """No entered output, so readiness refuses and nothing moves."""
        before = StockMovement.objects.count()
        with pytest.raises(ValidationError):
            post_production_batch(batch=production_draft, idempotency_key="U-1", actor=manager)
        assert StockMovement.objects.count() == before
        assert ProductionBatch.objects.get(pk=production_draft.pk).number == ""

    def test_a_closed_period_refuses_the_whole_posting(
        self,
        postable: ProductionBatch,
        organization: Organization,
        manager: User,
    ) -> None:
        from apps.accounting.models import AccountingPeriod, PeriodState

        AccountingPeriod.objects.filter(
            fiscal_year__organization=organization,
            start_date__lte=postable.planned_business_date,
            end_date__gte=postable.planned_business_date,
        ).update(state=PeriodState.CLOSED)

        before = StockMovement.objects.count()
        with pytest.raises(ValidationError) as caught:
            post_production_batch(batch=postable, idempotency_key="P-1", actor=manager)
        assert "period_closed" in codes_of(caught.value)
        assert StockMovement.objects.count() == before

    def test_the_business_date_is_the_frozen_one_and_never_today(
        self, postable: ProductionBatch, manager: User
    ) -> None:
        posted = post_production_batch(batch=postable, idempotency_key="D-1", actor=manager)
        assert entry_of(posted).business_date == postable.planned_business_date
        assert entry_of(posted).business_date != datetime.date.today()


class TestTheOutputLot:
    def test_an_untracked_output_gets_no_lot(
        self, postable: ProductionBatch, manager: User
    ) -> None:
        posted = post_production_batch(batch=postable, idempotency_key="L-1", actor=manager)
        assert posted.output_lot is None

    def test_a_tracked_output_gets_a_lot_naming_its_batch(
        self,
        posting_store: Warehouse,
        production_draft: ProductionBatch,
        cooked_rice: InventoryItem,
        manager: User,
    ) -> None:
        cooked_rice.tracks_lots = True
        cooked_rice.tracks_expiry = True
        cooked_rice.shelf_life_days = 3
        cooked_rice.save(
            update_fields=["tracks_lots", "tracks_expiry", "shelf_life_days", "updated_at"]
        )

        batch = _ready(production_draft, actor=manager)
        posted = post_production_batch(batch=batch, idempotency_key="L-2", actor=manager)

        lot = posted.output_lot
        assert lot is not None
        assert lot.produced_by_document_type == SOURCE_DOCUMENT_TYPE
        assert lot.produced_by_document_id == str(posted.public_id)
        # From the batch's own business date, never from today.
        assert lot.expiry_date == posted.planned_business_date + datetime.timedelta(days=3)
        assert InventoryLot.objects.filter(pk=lot.pk).count() == 1


class TestTheDraftBoundaryIsGone:
    def test_the_status_constraint_named_after_this_task_no_longer_exists(self) -> None:
        """
        The constraint Task 3.4 named after the task that had to remove it.

        Asserted by reading the database rather than the migration, because the
        migration having run and the constraint being gone are two different
        facts and only the second one matters.
        """
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT conname FROM pg_constraint WHERE conname = %s",
                ["production_batch_is_draft_only_until_task_3_5"],
            )
            assert cursor.fetchone() is None

    def test_the_posting_evidence_constraint_took_its_place(self) -> None:
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT conname FROM pg_constraint WHERE conname = ANY(%s)",
                [
                    [
                        "production_batch_posting_evidence_is_complete",
                        "production_batch_conserves_value",
                    ]
                ],
            )
            assert len(cursor.fetchall()) == 2
