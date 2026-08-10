"""
The inventory demo seed: its guards, its idempotency, and its realism.

Three things are worth proving about a demo command. That it cannot run
anywhere it would do harm. That running it twice is the same as running it
once — otherwise nobody dares run it when unsure. And that what it produces is
genuinely the product of the posting services, not a convincing forgery,
because a forgery would show the screens working and prove nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django.utils import timezone

from apps.accounting.models import JournalEntry
from apps.inventory.demo import (
    DEMO_ORGANIZATION_CODE,
    NAMESPACE,
    DemoSelectionError,
    reset_demo,
    seed_inventory_demo,
)
from apps.inventory.models import (
    AdjustmentLineKind,
    InventoryAdjustmentLine,
    InventoryItem,
    InventoryLot,
    InventoryMovementDocument,
    InventoryReasonCode,
    ItemCategory,
    PackageUnit,
    StockBalance,
    StockCount,
    StockCountStatus,
    StockLedgerEntry,
    StockMovement,
    StockTransfer,
    StockTransferStatus,
    Warehouse,
)
from apps.inventory.reconciliation import verify_inventory_accounting
from apps.organizations.models import Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(units: None) -> User:
    """The person who will sign in to review the seeded data."""
    return User.objects.create_user(username="demo-owner", password="pw-not-real-1234")


def run_seed(stdout: StringIO | None = None, **options: object) -> str:
    out = stdout if stdout is not None else StringIO()
    call_command("seed_inventory_demo", stdout=out, **options)
    return out.getvalue()


def demo_organization() -> Organization:
    return Organization.objects.get(code=DEMO_ORGANIZATION_CODE)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class TestGuards:
    def test_the_command_refuses_to_run_outside_debug(self, owner: User, settings: object) -> None:
        """
        Production is checked before an argument is read.

        A demo dataset there would be indistinguishable from real stock in
        every report the business runs, and its postings could not be deleted.
        """
        settings.DEBUG = False  # type: ignore[attr-defined]
        with pytest.raises(CommandError, match="DEBUG=True"):
            run_seed(user=owner.username, confirm_demo=True)
        assert not Organization.objects.filter(code=DEMO_ORGANIZATION_CODE).exists()

    def test_without_confirm_demo_master_data_is_seeded_but_nothing_posts(
        self, owner: User, settings: object
    ) -> None:
        """
        The flag guards the irreversible half, not the command.

        Master data can be recreated. A posted movement and its journal cannot,
        so they are what needs the deliberate keystroke.
        """
        settings.DEBUG = True  # type: ignore[attr-defined]
        output = run_seed(user=owner.username)

        assert InventoryItem.objects.filter(code__startswith="DEMO-").count() == 5
        assert not StockMovement.objects.exists()
        assert not JournalEntry.objects.exists()
        assert "--confirm-demo not given" in output

    def test_an_unknown_user_fails_with_the_known_ones_listed(
        self, owner: User, settings: object
    ) -> None:
        settings.DEBUG = True  # type: ignore[attr-defined]
        with pytest.raises(CommandError) as failure:
            run_seed(user="nobody-at-all", confirm_demo=True)
        assert "No user matches" in str(failure.value)
        assert owner.username in str(failure.value)

    def test_an_ambiguous_user_is_refused_rather_than_guessed(
        self, owner: User, settings: object
    ) -> None:
        """
        Two matches end the command.

        A user whose username is the digits of another user's id is a real
        collision, and picking either would write into the wrong account
        exactly once — which is once more than the ledger can undo.
        """
        settings.DEBUG = True  # type: ignore[attr-defined]
        User.objects.create_user(username=str(owner.pk), password="pw-not-real-1234")
        with pytest.raises(CommandError) as failure:
            run_seed(user=str(owner.pk), confirm_demo=True)
        assert "more than one user" in str(failure.value)

    def test_an_unknown_organization_lists_the_real_ones(self, owner: User) -> None:
        """Only the demo organization is created on demand; others must exist."""
        with pytest.raises(DemoSelectionError, match="No organization with code"):
            seed_inventory_demo(user=owner, organization_code="NOT-REAL")

    def test_one_branch_cannot_be_both_ends_of_a_transfer(self, owner: User) -> None:
        with pytest.raises(DemoSelectionError, match="source and destination"):
            seed_inventory_demo(
                user=owner,
                source_branch_code="DEMO-BUNOOK",
                destination_branch_code="DEMO-BUNOOK",
            )


# ---------------------------------------------------------------------------
# What one run produces
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(owner: User, settings: object) -> str:
    settings.DEBUG = True  # type: ignore[attr-defined]
    return run_seed(user=owner.username, confirm_demo=True)


class TestTheDataset:
    def test_only_demo_prefixed_master_data_is_created(self, seeded: str) -> None:
        """Nothing outside the namespace, so a database is readable at a glance."""
        organization = demo_organization()
        for model in (InventoryItem, ItemCategory, PackageUnit):
            codes = model.objects.filter(organization=organization).values_list("code", flat=True)
            assert codes, f"{model.__name__} seeded nothing"
            # Package units are the exception the brief asks for by name: SACK,
            # CARTON and CONTAINER are the vocabulary the conversions need, and
            # a "DEMO-SACK" would be a package unit nobody would ever reuse.
            if model is not PackageUnit:
                assert all(code.startswith("DEMO-") for code in codes), sorted(codes)

        assert set(
            PackageUnit.objects.filter(organization=organization).values_list("code", flat=True)
        ) == {"SACK", "CARTON", "CONTAINER"}

    def test_every_document_carries_the_namespace(self, seeded: str) -> None:
        organization = demo_organization()
        references = list(
            InventoryMovementDocument.objects.filter(organization=organization).values_list(
                "evidence_reference", flat=True
            )
        ) + list(
            StockTransfer.objects.filter(organization=organization).values_list(
                "evidence_reference", flat=True
            )
        )
        assert references
        assert all(reference.startswith(f"{NAMESPACE}/") for reference in references)

    def test_the_planned_balances_are_what_the_kernel_computed(self, seeded: str) -> None:
        """
        The arithmetic of the whole scenario, checked at the end of it.

        These numbers are not asserted because the seed wrote them — the seed
        writes no balance at all. They are what the valuation kernel produced
        from thirty-three posted movements.
        """
        organization = demo_organization()

        def quantity(warehouse: str, item: str) -> Decimal:
            """Summed across lots: chicken is held in two."""
            return sum(
                (
                    balance.quantity
                    for balance in StockBalance.objects.filter(
                        organization=organization, warehouse__code=warehouse, item__code=item
                    )
                ),
                Decimal("0.000"),
            )

        assert quantity("DEMO-MAIN", "DEMO-RICE") == Decimal("145.000")
        assert quantity("DEMO-MAIN", "DEMO-OIL") == Decimal("65.000")
        # 1000 opening − 100 issued − 200 dispatched − 10 written down + 15 found
        assert quantity("DEMO-MAIN", "DEMO-CONTAINER") == Decimal("705.000")
        assert quantity("DEMO-MAIN", "DEMO-MEAT") == Decimal("75.650")
        # 50 in the good lot; the expired lot received 8 and lost all 8.
        assert quantity("DEMO-MAIN", "DEMO-CHICKEN") == Decimal("46.000")
        assert quantity("DEMO-KITCHEN", "DEMO-RICE") == Decimal("29.500")
        assert quantity("DEMO-DEST-MAIN", "DEMO-CONTAINER") == Decimal("120.000")

    def test_the_value_only_adjustment_moved_value_and_not_quantity(self, seeded: str) -> None:
        """
        The reason the adjustment aggregate exists at all.

        Rice is still 145.000 KG after a 5,000 write-down, and the average cost
        is what fell — a change no signed movement of goods could express.
        """
        balance = StockBalance.objects.get(
            organization=demo_organization(),
            warehouse__code="DEMO-MAIN",
            item__code="DEMO-RICE",
        )
        assert balance.quantity == Decimal("145.000")
        assert balance.value == Decimal("216642.857")

    def test_the_expired_lot_was_received_and_written_off_to_zero(self, seeded: str) -> None:
        """
        Expired stock arrives, and waste is what resolves it.

        Receiving an out-of-date batch is legitimate; issuing it is not. The
        batch is left at zero quantity and zero value rather than stranded.
        """
        organization = demo_organization()
        lot = InventoryLot.objects.get(item__code="DEMO-CHICKEN", code="DEMO-CHK-LOT-02")
        assert lot.expiry_date is not None
        assert lot.expiry_date < timezone.localdate()

        balance = StockBalance.objects.get(
            organization=organization, warehouse__code="DEMO-MAIN", lot=lot
        )
        assert balance.quantity == Decimal("0.000")
        assert balance.value == Decimal("0.000")

    def test_all_three_adjustment_kinds_are_represented(self, seeded: str) -> None:
        kinds = set(
            InventoryAdjustmentLine.objects.filter(
                document__organization=demo_organization()
            ).values_list("kind", flat=True)
        )
        assert kinds == {
            AdjustmentLineKind.QUANTITY_GAIN,
            AdjustmentLineKind.QUANTITY_LOSS,
            AdjustmentLineKind.VALUE_ONLY,
        }

    def test_a_zero_balance_carries_zero_value(self, seeded: str) -> None:
        """Full depletion surrenders the whole remaining book value."""
        emptied = StockBalance.objects.filter(
            organization=demo_organization(), quantity=Decimal("0.000")
        )
        assert emptied.exists()
        assert all(balance.value == Decimal("0.000") for balance in emptied)

    def test_the_partial_transfer_leaves_stock_in_transit(self, seeded: str) -> None:
        """Otherwise the in-transit screen is a heading over an empty table."""
        organization = demo_organization()
        transfer = StockTransfer.objects.get(
            organization=organization, status=StockTransferStatus.PARTIALLY_RECEIVED
        )
        line = transfer.lines.get()
        assert line.base_quantity == Decimal("200.000")
        assert line.remaining_quantity == Decimal("80.000")

        in_transit = StockBalance.objects.get(
            organization=organization,
            warehouse__warehouse_type="IN_TRANSIT",
            item__code="DEMO-CONTAINER",
        )
        assert in_transit.quantity == Decimal("80.000")

    def test_the_shortage_transfer_reconciles_exactly(self, seeded: str) -> None:
        """dispatch = receipt + shortage, with nothing left over."""
        transfer = StockTransfer.objects.get(
            organization=demo_organization(), status=StockTransferStatus.CLOSED_WITH_SHORTAGE
        )
        line = transfer.lines.get()
        received = sum(
            (receipt_line.base_quantity for receipt_line in line.receipt_lines.all()),
            Decimal("0.000"),
        )
        shortage = sum(
            (
                shortage_line.base_quantity
                for shortage in transfer.shortages.all()
                for shortage_line in shortage.lines.all()
            ),
            Decimal("0.000"),
        )
        assert line.base_quantity == received + shortage
        assert line.remaining_quantity == Decimal("0.000")

    def test_the_reversed_receipt_stays_visible_with_its_reversal(self, seeded: str) -> None:
        """
        History keeps both halves.

        A correction is a reversal and a replacement, never an edit, so the
        original posting must still be there. The reversal is its own ledger
        entry rather than a second line on the original document — both name
        the same source document, one `POSTED` and one `REVERSED`.
        """
        organization = demo_organization()
        original = InventoryMovementDocument.objects.get(
            organization=organization,
            evidence_reference=f"{NAMESPACE}/RECEIPT-03-REVERSED",
        )
        assert original.status == "REVERSED"

        entries = StockLedgerEntry.objects.filter(
            organization=organization, source_document_id=str(original.public_id)
        )
        assert {entry.source_event for entry in entries} == {"POSTED", "REVERSED"}
        reversal = entries.get(source_event="REVERSED")
        assert reversal.reverses_id == entries.get(source_event="POSTED").pk

        movements = StockMovement.objects.filter(entry__source_document_id=str(original.public_id))
        assert movements.count() == 2, "the posting and its reversal are both movements"
        assert sum(movement.base_quantity for movement in movements) == Decimal("0.000")

    def test_all_four_transfer_states_are_represented(self, seeded: str) -> None:
        """A UI shown only terminal states has not been shown its lifecycle."""
        states = set(
            StockTransfer.objects.filter(organization=demo_organization()).values_list(
                "status", flat=True
            )
        )
        assert states == {
            StockTransferStatus.DRAFT,
            StockTransferStatus.COMPLETED,
            StockTransferStatus.PARTIALLY_RECEIVED,
            StockTransferStatus.CLOSED_WITH_SHORTAGE,
        }

    def test_the_whole_count_lifecycle_is_visible_at_once(self, seeded: str) -> None:
        """
        Four counts, four states.

        A screen shown only finished counts has not been shown the states a
        reviewer needs to judge: one being counted, one waiting for a second
        person, one accepted, one abandoned.
        """
        counts = dict(
            StockCount.objects.filter(organization=demo_organization()).values_list(
                "status", "warehouse__code"
            )
        )
        assert counts == {
            StockCountStatus.POSTED: "DEMO-KITCHEN",
            StockCountStatus.IN_PROGRESS: "DEMO-WIP",
            StockCountStatus.SUBMITTED: "DEMO-DEST-MAIN",
            StockCountStatus.CANCELLED: "DEMO-MAIN",
        }

    def test_a_cancelled_count_releases_its_freeze_and_is_kept(self, seeded: str) -> None:
        """
        Cancelling frees the store; deleting would erase why it was shut.

        The main warehouse must be usable afterwards — every other demo
        posting lives there, and a re-run would be refused if it were not.
        """
        cancelled = StockCount.objects.get(
            organization=demo_organization(), status=StockCountStatus.CANCELLED
        )
        assert Warehouse.objects.get(pk=cancelled.warehouse_id).frozen_by_count_id is None
        assert cancelled.cancelled_by is not None

    def test_a_submitted_count_still_holds_its_warehouse_freeze(self, seeded: str) -> None:
        """Submitted is an active state: the store stays shut until approval."""
        submitted = StockCount.objects.get(
            organization=demo_organization(), status=StockCountStatus.SUBMITTED
        )
        assert Warehouse.objects.get(pk=submitted.warehouse_id).frozen_by_count_id == submitted.pk
        assert submitted.approved_by is None

    def test_the_posted_count_was_approved_by_someone_else(self, seeded: str) -> None:
        """Maker-checker is a database constraint, so the two actors are real."""
        count = StockCount.objects.get(
            organization=demo_organization(), status=StockCountStatus.POSTED
        )
        assert count.conducted_by is not None
        assert count.approved_by is not None
        assert count.approved_by_id != count.conducted_by_id

    def test_the_active_count_owns_its_warehouse_freeze(self, seeded: str) -> None:
        active = StockCount.objects.get(
            organization=demo_organization(), status=StockCountStatus.IN_PROGRESS
        )
        frozen = Warehouse.objects.get(pk=active.warehouse_id)
        assert frozen.frozen_by_count_id == active.pk

    def test_the_in_transit_warehouse_is_system_owned(self, seeded: str) -> None:
        """Users never create or pick one; the service owns it."""
        transit = Warehouse.objects.filter(
            branch__organization=demo_organization(), warehouse_type="IN_TRANSIT"
        )
        assert transit.count() == 2
        assert all(warehouse.is_system for warehouse in transit)

    def test_reconciliation_is_clean(self, seeded: str) -> None:
        """
        The point of posting through services rather than writing rows.

        A hand-written balance would fail here, which is exactly why the seed
        does not write one.
        """
        assert verify_inventory_accounting(organization=demo_organization()) == []

    def test_the_output_survives_a_console_that_cannot_encode_arabic(
        self, owner: User, settings: object
    ) -> None:
        """
        Windows cp1252 cannot encode a single Arabic character.

        The failure that matters is not the mangled line: the command is
        atomic, so an exception raised while *printing* rolls back everything
        already seeded.
        """
        settings.DEBUG = True  # type: ignore[attr-defined]

        class Cp1252Stream(StringIO):
            encoding = "cp1252"

            def write(self, text: str) -> int:
                text.encode("cp1252")  # raises UnicodeEncodeError on Arabic
                return super().write(text)

        run_seed(user=owner.username, confirm_demo=True, stdout=Cp1252Stream())
        assert StockMovement.objects.filter(organization=demo_organization()).exists()


# ---------------------------------------------------------------------------
# Running it again
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_a_second_run_creates_nothing(self, seeded: str, owner: User) -> None:
        organization = demo_organization()
        before = {
            "movements": StockMovement.objects.filter(organization=organization).count(),
            "journals": JournalEntry.objects.filter(organization=organization).count(),
            "documents": InventoryMovementDocument.objects.filter(
                organization=organization
            ).count(),
            "transfers": StockTransfer.objects.filter(organization=organization).count(),
            "counts": StockCount.objects.filter(organization=organization).count(),
            "items": InventoryItem.objects.filter(organization=organization).count(),
        }

        output = run_seed(user=owner.username, confirm_demo=True)

        after = {
            "movements": StockMovement.objects.filter(organization=organization).count(),
            "journals": JournalEntry.objects.filter(organization=organization).count(),
            "documents": InventoryMovementDocument.objects.filter(
                organization=organization
            ).count(),
            "transfers": StockTransfer.objects.filter(organization=organization).count(),
            "counts": StockCount.objects.filter(organization=organization).count(),
            "items": InventoryItem.objects.filter(organization=organization).count(),
        }
        assert after == before
        assert "0 created" in output
        assert "reused" in output

    def test_a_second_run_leaves_the_balances_alone(self, seeded: str, owner: User) -> None:
        organization = demo_organization()
        before = sorted(
            StockBalance.objects.filter(organization=organization).values_list(
                "warehouse__code", "item__code", "quantity", "value"
            )
        )
        run_seed(user=owner.username, confirm_demo=True)
        after = sorted(
            StockBalance.objects.filter(organization=organization).values_list(
                "warehouse__code", "item__code", "quantity", "value"
            )
        )
        assert after == before


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_removes_drafts_and_keeps_posted_history(self, seeded: str) -> None:
        """
        The honest outcome, and the one this architecture requires.

        Deleting posted movements to make a reseed convenient would be the one
        operation the ledger exists to prevent, performed for the benefit of a
        development command.
        """
        organization = demo_organization()
        posted_before = StockMovement.objects.filter(organization=organization).count()

        report = reset_demo()

        assert any("draft" in line for line in report.removed)
        assert any("append-only" in line for line in report.kept)
        assert StockMovement.objects.filter(organization=organization).count() == posted_before
        assert not StockTransfer.objects.filter(
            organization=organization, status=StockTransferStatus.DRAFT
        ).exists()

    def test_reset_refuses_an_organization_it_does_not_own(self, seeded: str) -> None:
        """Ownership has to be provable before anything is deleted."""
        report = reset_demo(organization_code="KM")
        assert "Nothing to reset" in report.refused

    def test_reset_clears_unused_master_data_when_nothing_posted(
        self, owner: User, settings: object
    ) -> None:
        settings.DEBUG = True  # type: ignore[attr-defined]
        run_seed(user=owner.username)  # master data only

        report = reset_demo()

        assert not InventoryItem.objects.filter(code__startswith="DEMO-").exists()
        assert any("items" in line for line in report.removed)

    def test_reset_archives_reason_codes_rather_than_deleting_them(
        self, owner: User, settings: object
    ) -> None:
        """
        A reason code's code stays reserved, and a trigger enforces it.

        The reset does not get an exemption from an invariant for being
        development tooling — it archives, which is what the domain calls
        "gone".
        """
        settings.DEBUG = True  # type: ignore[attr-defined]
        run_seed(user=owner.username)

        reset_demo()

        codes = InventoryReasonCode.objects.filter(code__startswith="DEMO-")
        assert codes.exists(), "the codes stay reserved"
        assert not codes.filter(is_active=True).exists()

    def test_a_reseed_after_reset_revives_the_archived_reason_codes(
        self, owner: User, settings: object
    ) -> None:
        """An archived reason cannot be selected, so waste would have refused it."""
        settings.DEBUG = True  # type: ignore[attr-defined]
        run_seed(user=owner.username)
        reset_demo()

        run_seed(user=owner.username, confirm_demo=True)

        assert (
            InventoryReasonCode.objects.filter(code__startswith="DEMO-", is_active=True).count()
            == 4
        )
        assert StockMovement.objects.filter(organization=demo_organization()).exists()


# ---------------------------------------------------------------------------
# Every implemented section has something to show
# ---------------------------------------------------------------------------

#: Route name, and a string that must appear once the demo data is in place.
SECTIONS: list[tuple[str, str]] = [
    ("inventory:category_list", "DEMO-GRAINS"),
    ("inventory:package_unit_list", "CARTON"),
    ("inventory:item_list", "DEMO-RICE"),
    ("inventory:conversion_list", "DEMO-MEAT"),
    ("inventory:warehouse_list", "DEMO-KITCHEN"),
    ("inventory:stock_list", "DEMO-RICE"),
    ("inventory:movement_list", "DEMO-RICE"),
    ("inventory:opening_list", "OPN-"),
    ("inventory:mapping_list", "5-01-02-002"),
    ("inventory:inventory_receipt_list", "RCV-"),
    ("inventory:inventory_issue_list", "ISS-"),
    ("inventory:inventory_return_in_list", "RTN-"),
    ("inventory:transfer_list", "TRF-"),
    ("inventory:in_transit", "DEMO-CONTAINER"),
    ("inventory:inventory_waste_list", "WST-"),
    ("inventory:count_list", "CNT-"),
    ("inventory:adjustment_list", "ADJ-"),
    ("inventory:reason_code_list", "DEMO-SPOILED"),
]


class TestEverySectionHasData:
    def test_no_implemented_section_renders_empty(
        self, seeded: str, owner: User, client_for: Callable[[User], Client]
    ) -> None:
        """
        The whole point of the pass.

        One test rather than eighteen parametrised ones: the seed is the
        expensive part, and re-running it per screen would cost minutes to
        learn the same thing. Every failure is collected so the assertion
        names all of them, not just the first.
        """
        from django.urls import reverse

        client = client_for(owner)
        problems: list[str] = []
        for route, expected in SECTIONS:
            response = client.get(reverse(route))
            if response.status_code != 200:
                problems.append(f"{route}: HTTP {response.status_code}")
            elif expected not in response.content.decode():
                problems.append(f"{route}: no {expected!r} in the rendered page")
        assert not problems, "\n".join(problems)
