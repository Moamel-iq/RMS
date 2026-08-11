"""
Stock locations: quantity without value, and an invariant that cannot be false.

The tests that matter here are the ones about what locations *do not* do. A bin
holds no money, a move between bins posts no `StockMovement`, and an issue that
never mentions a bin still leaves the location ledger consistent — because if
any of those were untrue, locations would have quietly become a second
valuation dimension.
"""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction

from apps.core.context import audit_context
from apps.inventory import locations
from apps.inventory.models import (
    InventoryItem,
    LocationMovementType,
    StockBalance,
    StockLocation,
    StockLocationBalance,
    StockLocationMovement,
    StockMovement,
    Warehouse,
)
from apps.inventory.reconciliation import verify_inventory_accounting, verify_locations
from apps.organizations.models import Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(units: None) -> User:
    return User.objects.create_user(username="loc-owner", password="pw-not-real-1234")


@pytest.fixture
def seeded(owner: User, settings: object) -> None:
    settings.DEBUG = True  # type: ignore[attr-defined]
    call_command("seed_inventory_demo", user=owner.username, confirm_demo=True, stdout=StringIO())


@pytest.fixture
def organization(seeded: None) -> Organization:
    return Organization.objects.get(code="DEMO-KHAN-MANDI")


@pytest.fixture
def main(organization: Organization) -> Warehouse:
    """
    The kitchen store, deliberately — the demo puts stock away in DEMO-MAIN.

    These tests are about locations in the abstract, so they use the one
    demo warehouse the seed leaves entirely unlocated. Pointing them at
    DEMO-MAIN made them track the demo's own put-away and they broke the day
    it gained bins, which is a test coupled to a fixture rather than to a rule.
    """
    return Warehouse.objects.get(branch__organization=organization, code="DEMO-KITCHEN")


@pytest.fixture
def rice(organization: Organization) -> InventoryItem:
    return InventoryItem.objects.get(organization=organization, code="DEMO-RICE")


@pytest.fixture
def bins(main: Warehouse, owner: User) -> tuple[StockLocation, StockLocation]:
    with audit_context(actor=owner):
        first = locations.create_location(warehouse=main, code="TEST-A", name_ar="رف أ")
        second = locations.create_location(warehouse=main, code="TEST-B", name_ar="رف ب")
    return first, second


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


class TestLocationMasterData:
    def test_a_location_belongs_to_one_warehouse_and_is_code_unique(
        self, main: Warehouse, owner: User, bins: tuple[StockLocation, StockLocation]
    ) -> None:
        from django.db.utils import IntegrityError

        with pytest.raises((ValidationError, IntegrityError)), transaction.atomic():
            with audit_context(actor=owner):
                locations.create_location(warehouse=main, code="TEST-A", name_ar="مكرر")

    def test_a_system_warehouse_takes_no_locations(
        self, organization: Organization, owner: User
    ) -> None:
        """In-transit is a bookkeeping place, not a room with shelves."""
        transit = Warehouse.objects.filter(
            branch__organization=organization, warehouse_type="IN_TRANSIT"
        ).first()
        assert transit is not None
        with pytest.raises(ValidationError) as refusal, audit_context(actor=owner):
            locations.create_location(warehouse=transit, code="TEST-X", name_ar="رف")
        assert refusal.value.code == "location_in_system_warehouse"

    def test_a_location_holding_stock_cannot_be_archived(
        self, bins: tuple[StockLocation, StockLocation], rice: InventoryItem, owner: User
    ) -> None:
        first, _second = bins
        with audit_context(actor=owner):
            locations.put_away(location=first, item=rice, quantity=Decimal("5.000"))
            with pytest.raises(ValidationError) as refusal:
                locations.update_location(location=first, name_ar="رف أ", is_active=False)
        assert refusal.value.code == "location_not_empty"


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


class TestTheInvariant:
    def test_unlocated_is_derived_and_starts_as_the_whole_holding(
        self, main: Warehouse, rice: InventoryItem
    ) -> None:
        """A warehouse that has never used bins holds everything unlocated."""
        held = locations.warehouse_quantity(main, rice, None)
        assert held == Decimal("29.500")
        assert locations.located_total(main, rice, None) == Decimal("0")
        assert locations.unlocated_quantity(main, rice, None) == held

    def test_put_away_moves_between_the_two_buckets_and_the_total_holds(
        self,
        bins: tuple[StockLocation, StockLocation],
        main: Warehouse,
        rice: InventoryItem,
        owner: User,
    ) -> None:
        first, _second = bins
        before = locations.warehouse_quantity(main, rice, None)
        with audit_context(actor=owner):
            locations.put_away(location=first, item=rice, quantity=Decimal("20.000"))

        assert locations.located_total(main, rice, None) == Decimal("20.000")
        assert locations.unlocated_quantity(main, rice, None) == before - Decimal("20.000")
        # The warehouse total is untouched: nothing entered or left.
        assert locations.warehouse_quantity(main, rice, None) == before

    def test_putting_away_more_than_is_unlocated_is_refused(
        self,
        bins: tuple[StockLocation, StockLocation],
        main: Warehouse,
        rice: InventoryItem,
        owner: User,
    ) -> None:
        """A bin cannot receive goods the warehouse has not got."""
        first, _second = bins
        held = locations.warehouse_quantity(main, rice, None)
        with pytest.raises(ValidationError) as refusal, audit_context(actor=owner):
            locations.put_away(location=first, item=rice, quantity=held + Decimal("1.000"))
        assert refusal.value.code == "location_put_away_exceeds_unlocated"

    def test_a_bin_cannot_release_more_than_it_holds(
        self, bins: tuple[StockLocation, StockLocation], rice: InventoryItem, owner: User
    ) -> None:
        first, _second = bins
        with audit_context(actor=owner):
            locations.put_away(location=first, item=rice, quantity=Decimal("10.000"))
            with pytest.raises(ValidationError) as refusal:
                locations.pick(location=first, item=rice, quantity=Decimal("11.000"))
        assert refusal.value.code == "location_insufficient_stock"

    def test_verify_locations_is_clean_on_the_seeded_organization(
        self, organization: Organization
    ) -> None:
        assert verify_locations(organization) == []

    def test_planted_over_allocation_is_detected(
        self,
        bins: tuple[StockLocation, StockLocation],
        main: Warehouse,
        rice: InventoryItem,
        organization: Organization,
        owner: User,
    ) -> None:
        """
        A bin claiming more than the warehouse holds is the failure this exists
        for. Written directly, because that is exactly the state the services
        refuse to create.
        """
        first, _second = bins
        with audit_context(actor=owner):
            locations.put_away(location=first, item=rice, quantity=Decimal("10.000"))
        balance = StockLocationBalance.objects.get(location=first, item=rice, lot=None)
        balance.quantity = Decimal("99999.000")
        balance.save(update_fields=["quantity"])

        problems = verify_locations(organization)
        assert any(problem.field == "located_exceeds_warehouse" for problem in problems)
        assert any(
            "located_exceeds_warehouse" in line
            for line in verify_inventory_accounting(organization)
        )


# ---------------------------------------------------------------------------
# No value, ever
# ---------------------------------------------------------------------------


class TestLocationsCarryNoValue:
    def test_the_balance_model_has_no_money_columns(self) -> None:
        """
        ADR-018 §2, asserted on the schema rather than trusted.

        A value column here would make locations a second valuation dimension
        the moment somebody populated it.
        """
        fields = {field.name for field in StockLocationBalance._meta.get_fields()}
        assert "quantity" in fields
        assert not fields & {"value", "average_cost", "control_account", "unit_cost"}

    def test_a_move_between_bins_posts_no_stock_movement(
        self,
        bins: tuple[StockLocation, StockLocation],
        main: Warehouse,
        rice: InventoryItem,
        owner: User,
    ) -> None:
        """
        The case that proves the split is real.

        Nothing entered or left the warehouse, so the valued ledger must not
        move at all — not by a movement, not by a journal, not by a re-average.
        """
        first, second = bins
        with audit_context(actor=owner):
            locations.put_away(location=first, item=rice, quantity=Decimal("20.000"))

        before_movements = StockMovement.objects.count()
        balance = StockBalance.objects.get(warehouse=main, item=rice, lot=None)
        before = (balance.quantity, balance.value, balance.average_cost, balance.version)

        with audit_context(actor=owner):
            out, into = locations.move_between_locations(
                source=first, destination=second, item=rice, quantity=Decimal("8.000")
            )

        assert StockMovement.objects.count() == before_movements
        balance.refresh_from_db()
        assert (balance.quantity, balance.value, balance.average_cost, balance.version) == before
        assert out.stock_movement_id is None and into.stock_movement_id is None
        assert out.movement_type == LocationMovementType.TRANSFER_OUT
        assert into.movement_type == LocationMovementType.TRANSFER_IN

    def test_a_move_cannot_cross_warehouses(
        self,
        main: Warehouse,
        organization: Organization,
        rice: InventoryItem,
        owner: User,
        bins: tuple[StockLocation, StockLocation],
    ) -> None:
        elsewhere_warehouse = Warehouse.objects.get(
            branch__organization=organization, code="DEMO-MAIN"
        )
        with audit_context(actor=owner):
            elsewhere = locations.create_location(
                warehouse=elsewhere_warehouse, code="TEST-K", name_ar="رف آخر"
            )
            locations.put_away(location=bins[0], item=rice, quantity=Decimal("5.000"))
            with pytest.raises(ValidationError) as refusal:
                locations.move_between_locations(
                    source=bins[0], destination=elsewhere, item=rice, quantity=Decimal("1.000")
                )
        assert refusal.value.code == "location_move_crosses_warehouses"


# ---------------------------------------------------------------------------
# The ledger hook
# ---------------------------------------------------------------------------


class TestOutboundRelease:
    def test_an_issue_that_names_no_bin_still_leaves_the_invariant_true(
        self,
        bins: tuple[StockLocation, StockLocation],
        main: Warehouse,
        rice: InventoryItem,
        organization: Organization,
        owner: User,
    ) -> None:
        """
        The case that makes "optional" survivable.

        All the rice is put away, then an ordinary issue is posted through the
        normal service with no mention of a location. Without the release hook
        the bins would claim more than the warehouse holds.
        """
        import datetime

        from apps.accounting.models import CostCenter
        from apps.inventory.demo import BAGHDAD
        from apps.inventory.models import InventoryDocumentType
        from apps.inventory.operations import (
            DocumentLineInput,
            add_line,
            create_document,
            post_document,
        )
        from apps.organizations.models import Branch

        first, _second = bins
        held = locations.warehouse_quantity(main, rice, None)
        with audit_context(actor=owner):
            locations.put_away(location=first, item=rice, quantity=held)
        assert locations.unlocated_quantity(main, rice, None) == Decimal("0.000")

        branch = Branch.objects.get(organization=organization, code="DEMO-BUNOOK")
        with audit_context(actor=owner):
            document = create_document(
                organization=organization,
                branch=branch,
                warehouse=main,
                document_type=InventoryDocumentType.ISSUE,
                effective_at=datetime.datetime(2026, 8, 10, 19, 0, tzinfo=BAGHDAD),
                evidence_reference="LOC-TEST/ISSUE",
                narration="صرف بلا موقع",
                cost_center=CostCenter.objects.get(organization=organization, code="KITCHEN"),
            )
            add_line(
                document=document,
                line=DocumentLineInput(item=rice, base_quantity=Decimal("9.000")),
            )
            post_document(document=document)

        assert locations.warehouse_quantity(main, rice, None) == held - Decimal("9.000")
        assert locations.located_total(main, rice, None) == held - Decimal("9.000")
        assert locations.unlocated_quantity(main, rice, None) == Decimal("0.000")
        assert verify_locations(organization) == []

        released = StockLocationMovement.objects.filter(reference="auto-release")
        assert released.count() == 1
        assert released.get().base_quantity == Decimal("-9.000")

    def test_the_unlocated_pool_absorbs_an_issue_first(
        self,
        bins: tuple[StockLocation, StockLocation],
        main: Warehouse,
        rice: InventoryItem,
        organization: Organization,
        owner: User,
    ) -> None:
        """
        A picker takes what is loose before opening a bin, and so does this.
        """
        first, _second = bins
        with audit_context(actor=owner):
            locations.put_away(location=first, item=rice, quantity=Decimal("10.000"))

        released = locations.release_for_outbound(
            warehouse_id=main.pk,
            item_id=rice.pk,
            lot_id=None,
            quantity_after=Decimal("100.000"),
        )
        assert released == [], "plenty unlocated, so no bin was touched"
        assert locations.located_total(main, rice, None) == Decimal("10.000")

    def test_a_warehouse_with_no_locations_is_untouched_by_the_hook(
        self, organization: Organization, rice: InventoryItem
    ) -> None:
        """The common case: one query, no writes."""
        bare = Warehouse.objects.get(branch__organization=organization, code="DEMO-WIP")
        before = StockLocationMovement.objects.filter(warehouse=bare).count()
        assert (
            locations.release_for_outbound(
                warehouse_id=bare.pk, item_id=rice.pk, lot_id=None, quantity_after=Decimal("0.000")
            )
            == []
        )
        assert StockLocationMovement.objects.filter(warehouse=bare).count() == before


@pytest.mark.django_db(transaction=True)
class TestLocationConcurrency:
    def test_two_put_aways_cannot_both_take_the_same_unlocated_stock(
        self, settings: object
    ) -> None:
        """
        Real COMMIT boundary.

        Both transactions read the same unlocated remainder; the advisory lock
        is on `(warehouse, item, lot)` rather than the bin, so the second waits
        and then sees what the first took. A per-bin lock would let both
        succeed and the bins would together claim more than the warehouse has.
        """
        import threading

        settings.DEBUG = True  # type: ignore[attr-defined]
        call_command("seed_units", verbosity=0)
        user = User.objects.create_user(username="race", password="pw-not-real-1234")
        call_command(
            "seed_inventory_demo", user=user.username, confirm_demo=True, stdout=StringIO()
        )

        organization = Organization.objects.get(code="DEMO-KHAN-MANDI")
        warehouse = Warehouse.objects.get(branch__organization=organization, code="DEMO-KITCHEN")
        item = InventoryItem.objects.get(organization=organization, code="DEMO-RICE")
        held = locations.warehouse_quantity(warehouse, item, None)

        with audit_context(actor=user):
            bin_a = locations.create_location(warehouse=warehouse, code="RACE-A", name_ar="أ")
            bin_b = locations.create_location(warehouse=warehouse, code="RACE-B", name_ar="ب")

        results: list[str] = []

        def put_away(location: StockLocation) -> None:
            from django.db import connection as thread_connection

            try:
                with transaction.atomic(), audit_context(actor=user):
                    locations.put_away(location=location, item=item, quantity=held)
                results.append("ok")
            except ValidationError as refusal:
                results.append(str(refusal.code))
            finally:
                thread_connection.close()

        threads = [
            threading.Thread(target=put_away, args=(bin_a,)),
            threading.Thread(target=put_away, args=(bin_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(results) == ["location_put_away_exceeds_unlocated", "ok"]
        assert locations.located_total(warehouse, item, None) == held
        assert verify_locations(organization) == []
