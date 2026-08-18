"""
Immutable cost snapshots: what they record, what they refuse, and what no
amount of privilege can change about one afterwards.

The three claims worth stating up front:

* **Append-only means the database refuses it**, not that no service offers it.
  A Python guard would be bypassed by a bulk update, raw SQL, the admin, a data
  migration and anybody with a psql prompt — precisely the people a costing
  record exists to keep honest. The trigger tests use raw SQL for exactly that
  reason: a test that went through the ORM would prove only that the ORM has no
  `update()` call in it.
* **Idempotency is a key *and* a fingerprint.** A retry returns the original; a
  different request under the same key is a conflict, never a silent hand-back.
* **A snapshot is only ever built from a complete authoritative card.** No
  partial mode, no force, no "snapshot what we have" — a costing record with a
  hole in it looks like a total.

The verifier's own tests live here too, because what it checks is only
meaningful against a snapshot somebody wrote.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.inventory.models import InventoryItem, Warehouse
from apps.kitchen.cost_reconciliation import (
    recompute_findings,
    snapshot_findings,
    snapshots_checked,
    verify_cost_snapshots,
)
from apps.kitchen.costing import CALCULATION_VERSION, cost_recipe_version, preview_recipe_cost
from apps.kitchen.models import (
    RecipeCostSnapshot,
    RecipeCostSnapshotLine,
    RecipeCostSnapshotServing,
    RecipeVersion,
)
from apps.kitchen.snapshots import create_recipe_cost_snapshot
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import build_complete_draft, carry_to_active, make_child_recipe

pytestmark = pytest.mark.django_db


def _today() -> datetime.date:
    from django.utils import timezone

    return timezone.localdate()


def _codes(error: Any) -> set[str]:
    if hasattr(error, "message"):
        return {error.code or ""}
    if hasattr(error, "error_dict"):
        return {
            code for errors in error.error_dict.values() for item in errors for code in _codes(item)
        }
    if hasattr(error, "error_list"):
        return {code for item in error.error_list for code in _codes(item)}
    return set()


@pytest.fixture
def snapshot(
    valued_store: Warehouse, costable_version: RecipeVersion, manager: User
) -> RecipeCostSnapshot:
    card = cost_recipe_version(
        version=costable_version, warehouse=valued_store, as_of_date=_today()
    )
    return create_recipe_cost_snapshot(
        card=card,
        actor=manager,
        idempotency_key="SNAP-1",
        reference="KM-RCP-004/2026/07",
        reason="مراجعة قائمة الطعام",
    )


class TestWhatIsRecorded:
    def test_the_snapshot_carries_its_whole_explanation(
        self, snapshot: RecipeCostSnapshot, valued_store: Warehouse
    ) -> None:
        assert snapshot.warehouse_code == valued_store.code
        assert snapshot.as_of_date == _today()
        assert snapshot.valuation_mode == "POSTED_AS_OF"
        assert snapshot.calculation_version == CALCULATION_VERSION
        assert snapshot.is_authoritative is True
        assert snapshot.version_status == "ACTIVE"
        assert snapshot.ledger_cutoff_sequence > 0
        assert snapshot.total_material_cost == Decimal("6000.000")

    def test_the_lines_sum_to_the_header_total(self, snapshot: RecipeCostSnapshot) -> None:
        lines = list(snapshot.lines.all())
        assert lines
        assert sum(line.allocated_extension for line in lines) == snapshot.total_material_cost

    def test_the_class_totals_sum_to_the_header_total(self, snapshot: RecipeCostSnapshot) -> None:
        """A database check constraint holds this, not a service."""
        assert (
            snapshot.food_total + snapshot.packaging_total + snapshot.accompaniment_total
            == snapshot.total_material_cost
        )

    def test_the_serving_scenarios_allocate_the_whole_total(
        self, snapshot: RecipeCostSnapshot
    ) -> None:
        servings = list(snapshot.servings.all())
        assert servings
        for serving in servings:
            assert serving.allocated_total == snapshot.total_material_cost

    def test_each_line_keeps_the_valuation_evidence_behind_its_unit_cost(
        self, snapshot: RecipeCostSnapshot
    ) -> None:
        """`value / quantity` across the lots, visible on the row that used it."""
        line = snapshot.lines.first()
        assert line is not None
        assert line.valuation_quantity == Decimal("200.000")
        assert line.valuation_value == Decimal("300000.000")
        assert line.valuation_lot_count == 1
        assert line.unit_cost == Decimal("1500.000000")

    def test_each_line_keeps_the_identities_it_can_be_read_back_through(
        self, snapshot: RecipeCostSnapshot, costable_version: RecipeVersion
    ) -> None:
        line = snapshot.lines.first()
        assert line is not None
        assert line.source_version_public_id == costable_version.public_id
        assert line.recipe_line_public_id == costable_version.lines.get().public_id
        assert line.item_code == "RICE"
        assert line.component_path == ""

    def test_the_raw_extension_is_exact_to_twelve_places(
        self, snapshot: RecipeCostSnapshot
    ) -> None:
        """
        Quantity has six places and a unit cost has six, so the product has at
        most twelve and is stored with nothing lost. Only the *total* rounds.
        """
        line = snapshot.lines.first()
        assert line is not None
        assert line.raw_extension == line.effective_quantity * line.unit_cost

    def test_a_later_inventory_movement_does_not_rewrite_the_snapshot(
        self,
        snapshot: RecipeCostSnapshot,
        organization: Organization,
        valued_store: Warehouse,
        rice: InventoryItem,
    ) -> None:
        """
        Stock moved. That is what stock does, and the record stays what it said.
        """
        from .conftest import post_receipt

        before = snapshot.total_material_cost
        post_receipt(
            organization=organization,
            warehouse=valued_store,
            item=rice,
            quantity="100",
            unit_cost="9000",
            key="after-the-snapshot",
        )
        assert RecipeCostSnapshot.objects.get(pk=snapshot.pk).total_material_cost == before


class TestWhatIsRefused:
    def test_a_preview_cannot_become_a_snapshot(
        self, valued_store: Warehouse, complete_draft: RecipeVersion, manager: User
    ) -> None:
        card = preview_recipe_cost(
            version=complete_draft, warehouse=valued_store, as_of_date=_today()
        )
        with pytest.raises(ValidationError) as refusal:
            create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="NOPE-1")
        assert "recipe_cost_version_not_authoritative" in _codes(refusal.value)
        assert RecipeCostSnapshot.objects.count() == 0

    def test_an_incomplete_card_cannot_become_a_snapshot(
        self,
        valued_store: Warehouse,
        organization: Organization,
        kilogram: UnitOfMeasure,
        cooked_rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """A costing record with a hole in it looks like a total. There is no force."""
        recipe = make_child_recipe(organization=organization, code="UNVALUED-1", author=manager)
        draft = build_complete_draft(recipe=recipe, unit=kilogram, item=cooked_rice, author=manager)
        version = carry_to_active(
            draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert not card.is_complete
        with pytest.raises(ValidationError) as refusal:
            create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="HOLE-1")
        assert "recipe_cost_snapshot_requires_complete_cost" in _codes(refusal.value)
        assert RecipeCostSnapshot.objects.count() == 0

    def test_an_empty_idempotency_key_is_refused(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> None:
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        with pytest.raises(ValidationError) as refusal:
            create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="   ")
        assert "idempotency_key_required" in _codes(refusal.value)


class TestAppendOnly:
    def test_a_raw_update_of_the_header_is_refused(self, snapshot: RecipeCostSnapshot) -> None:
        """
        Raw SQL, deliberately. The ORM having no `update()` call proves nothing
        about a psql prompt, and the psql prompt is what the trigger is for.
        """
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_recipecostsnapshot SET total_material_cost = 1 WHERE id = %s",
                [snapshot.pk],
            )

    def test_a_raw_delete_of_the_header_is_refused(self, snapshot: RecipeCostSnapshot) -> None:
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM kitchen_recipecostsnapshot WHERE id = %s", [snapshot.pk])

    def test_a_raw_update_of_a_line_is_refused(self, snapshot: RecipeCostSnapshot) -> None:
        """
        A header nobody may edit beside lines anybody may edit would be a
        document whose total no longer agreed with the figures behind it.
        """
        line = snapshot.lines.first()
        assert line is not None
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_recipecostsnapshotline SET unit_cost = 1 WHERE id = %s",
                [line.pk],
            )

    def test_a_raw_delete_of_a_line_is_refused(self, snapshot: RecipeCostSnapshot) -> None:
        line = snapshot.lines.first()
        assert line is not None
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM kitchen_recipecostsnapshotline WHERE id = %s", [line.pk])

    def test_a_raw_update_of_a_serving_row_is_refused(self, snapshot: RecipeCostSnapshot) -> None:
        serving = snapshot.servings.first()
        assert serving is not None
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_recipecostsnapshotserving SET allocated_total = 1 WHERE id = %s",
                [serving.pk],
            )

    def test_the_admin_is_read_only_for_everyone(self) -> None:
        """Including a superuser: the rows refuse both verbs underneath anyway."""
        from django.contrib import admin

        from apps.kitchen.admin import (
            RecipeCostSnapshotAdmin,
            RecipeCostSnapshotLineAdmin,
            RecipeCostSnapshotServingAdmin,
        )

        for model, klass in (
            (RecipeCostSnapshot, RecipeCostSnapshotAdmin),
            (RecipeCostSnapshotLine, RecipeCostSnapshotLineAdmin),
            (RecipeCostSnapshotServing, RecipeCostSnapshotServingAdmin),
        ):
            registered = klass(model, admin.site)
            assert registered.has_add_permission(None) is False  # type: ignore[arg-type]
            assert registered.has_change_permission(None) is False  # type: ignore[arg-type]
            assert registered.has_delete_permission(None) is False  # type: ignore[arg-type]


class TestIdempotency:
    def test_the_same_key_and_request_returns_the_original(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> None:
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        first = create_recipe_cost_snapshot(
            card=card, actor=manager, idempotency_key="RETRY-1", reference="R"
        )
        second = create_recipe_cost_snapshot(
            card=card, actor=manager, idempotency_key="RETRY-1", reference="R"
        )
        assert first.pk == second.pk
        assert RecipeCostSnapshot.objects.count() == 1
        assert RecipeCostSnapshotLine.objects.count() == len(card.lines)
        assert RecipeCostSnapshotServing.objects.count() == len(card.servings)

    def test_the_same_key_with_a_changed_request_is_a_conflict(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> None:
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        create_recipe_cost_snapshot(
            card=card, actor=manager, idempotency_key="CONFLICT-1", reference="first"
        )
        with pytest.raises(ValidationError) as refusal:
            create_recipe_cost_snapshot(
                card=card, actor=manager, idempotency_key="CONFLICT-1", reference="second"
            )
        assert "idempotency_key_conflict" in _codes(refusal.value)
        assert RecipeCostSnapshot.objects.count() == 1

    def test_a_different_date_under_the_same_key_is_a_conflict(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> None:
        """The as-of date is part of the request, so changing it changes it."""
        today = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        create_recipe_cost_snapshot(card=today, actor=manager, idempotency_key="DATE-1")
        tomorrow = cost_recipe_version(
            version=costable_version,
            warehouse=valued_store,
            as_of_date=_today() + datetime.timedelta(days=1),
        )
        with pytest.raises(ValidationError) as refusal:
            create_recipe_cost_snapshot(card=tomorrow, actor=manager, idempotency_key="DATE-1")
        assert "idempotency_key_conflict" in _codes(refusal.value)

    def test_two_different_keys_intentionally_make_two_snapshots(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> None:
        """
        A menu is repriced more than once, and the second decision is real.

        There is deliberately no uniqueness on `(version, warehouse, date)`:
        one would forbid the second decision in the name of preventing a
        duplicate the key already prevents.
        """
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        first = create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="A")
        second = create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="B")
        assert first.pk != second.pk
        assert RecipeCostSnapshot.objects.count() == 2

    def test_the_fingerprint_ignores_the_figures_it_produced(
        self,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
        organization: Organization,
        rice: InventoryItem,
        manager: User,
    ) -> None:
        """
        Two identical requests a week apart legitimately produce different
        totals — stock moved. Hashing the answer would turn every honest re-run
        into a permanent conflict.
        """
        from .conftest import post_receipt

        first_card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        original = create_recipe_cost_snapshot(
            card=first_card, actor=manager, idempotency_key="STABLE-1"
        )
        post_receipt(
            organization=organization,
            warehouse=valued_store,
            item=rice,
            quantity="100",
            unit_cost="9000",
            key="moves-the-average",
        )
        second_card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        assert second_card.total_material_cost != first_card.total_material_cost
        replayed = create_recipe_cost_snapshot(
            card=second_card, actor=manager, idempotency_key="STABLE-1"
        )
        assert replayed.pk == original.pk
        assert replayed.total_material_cost == original.total_material_cost


class TestTheVerifier:
    def test_a_healthy_snapshot_reports_nothing(
        self, snapshot: RecipeCostSnapshot, organization: Organization
    ) -> None:
        assert verify_cost_snapshots(organization) == []
        assert snapshot_findings(snapshot) == []
        assert snapshots_checked(organization) == 1

    def test_a_later_inventory_movement_is_not_a_finding(
        self,
        snapshot: RecipeCostSnapshot,
        organization: Organization,
        valued_store: Warehouse,
        rice: InventoryItem,
    ) -> None:
        """
        The one comparison the verifier must never make.

        A March snapshot whose items cost more in September is correct in every
        particular, and a red list full of them would stop being read.
        """
        from .conftest import post_receipt

        post_receipt(
            organization=organization,
            warehouse=valued_store,
            item=rice,
            quantity="100",
            unit_cost="9000",
            key="later-movement",
        )
        assert verify_cost_snapshots(organization) == []

    @pytest.mark.django_db(transaction=True)
    def test_planted_drift_in_the_header_is_reported_and_not_repaired(
        self, snapshot: RecipeCostSnapshot, organization: Organization
    ) -> None:
        """
        Plants the impossible state by disabling the trigger, inside a test.

        The verifier reports; nothing is corrected. There is no `--repair`, and
        the tables would refuse one anyway.
        """
        _plant(
            "UPDATE kitchen_recipecostsnapshot SET food_total = food_total + 1, "
            "total_material_cost = total_material_cost + 1 WHERE id = %s",
            [snapshot.pk],
            table="kitchen_recipecostsnapshot",
        )
        refreshed = RecipeCostSnapshot.objects.get(pk=snapshot.pk)
        findings = snapshot_findings(refreshed)
        codes = {finding.code for finding in findings}
        assert "cost_snapshot_lines_do_not_sum_to_total" in codes
        assert "cost_snapshot_class_total_disagrees_with_lines" in codes
        # Reported, not repaired.
        assert (
            RecipeCostSnapshot.objects.get(pk=snapshot.pk).total_material_cost
            == refreshed.total_material_cost
        )

    @pytest.mark.django_db(transaction=True)
    def test_planted_drift_in_a_line_is_reported(self, snapshot: RecipeCostSnapshot) -> None:
        line = snapshot.lines.first()
        assert line is not None
        _plant(
            "UPDATE kitchen_recipecostsnapshotline SET unit_cost = unit_cost + 1 WHERE id = %s",
            [line.pk],
            table="kitchen_recipecostsnapshotline",
        )
        codes = {
            finding.code
            for finding in snapshot_findings(RecipeCostSnapshot.objects.get(pk=snapshot.pk))
        }
        assert "cost_snapshot_line_extension_disagrees" in codes
        assert "cost_snapshot_unit_cost_disagrees_with_valuation" in codes

    @pytest.mark.django_db(transaction=True)
    def test_an_unsupported_calculation_version_is_reported(
        self, snapshot: RecipeCostSnapshot
    ) -> None:
        """The verifier refuses to certify arithmetic it does not understand."""
        _plant(
            "UPDATE kitchen_recipecostsnapshot SET calculation_version = 'RCP-COST-99' "
            "WHERE id = %s",
            [snapshot.pk],
            table="kitchen_recipecostsnapshot",
        )
        codes = {
            finding.code
            for finding in snapshot_findings(RecipeCostSnapshot.objects.get(pk=snapshot.pk))
        }
        assert "cost_snapshot_unsupported_calculation_version" in codes

    @pytest.mark.django_db(transaction=True)
    def test_a_line_number_gap_is_reported(self, snapshot: RecipeCostSnapshot) -> None:
        line = snapshot.lines.first()
        assert line is not None
        _plant(
            "UPDATE kitchen_recipecostsnapshotline SET line_number = 9 WHERE id = %s",
            [line.pk],
            table="kitchen_recipecostsnapshotline",
        )
        codes = {
            finding.code
            for finding in snapshot_findings(RecipeCostSnapshot.objects.get(pk=snapshot.pk))
        }
        assert "cost_snapshot_line_numbers_have_gaps" in codes

    @pytest.mark.django_db(transaction=True)
    def test_a_broken_fingerprint_is_reported(self, snapshot: RecipeCostSnapshot) -> None:
        _plant(
            "UPDATE kitchen_recipecostsnapshot SET reference = 'changed' WHERE id = %s",
            [snapshot.pk],
            table="kitchen_recipecostsnapshot",
        )
        codes = {
            finding.code
            for finding in snapshot_findings(RecipeCostSnapshot.objects.get(pk=snapshot.pk))
        }
        assert "cost_snapshot_fingerprint_does_not_match_request" in codes

    def test_recomputation_at_the_recorded_cutoff_agrees(
        self, snapshot: RecipeCostSnapshot
    ) -> None:
        """
        The explicit second mode: re-read the ledger at the snapshot's own
        cutoff and re-derive the unit costs. Still not a comparison with today.
        """
        assert recompute_findings(snapshot) == []

    def test_recomputation_survives_a_later_posting(
        self,
        snapshot: RecipeCostSnapshot,
        organization: Organization,
        valued_store: Warehouse,
        rice: InventoryItem,
    ) -> None:
        from .conftest import post_receipt

        post_receipt(
            organization=organization,
            warehouse=valued_store,
            item=rice,
            quantity="100",
            unit_cost="9000",
            key="after-cutoff",
        )
        assert recompute_findings(snapshot) == []


def _plant(sql: str, params: list[int], *, table: str) -> None:
    """
    Write an impossible state past the append-only trigger.

    Only ever inside a test, and never in seeded data. The trigger being
    disabled for one statement is the whole reason these findings are reachable
    at all: without it there would be no way to prove the verifier can see them.

    Callers run with `transaction=True`, because `ALTER TABLE` is refused while
    a transaction still has trigger events queued — which every ordinary test in
    this file has. The table is truncated afterwards, so the impossible row
    never outlives the test that planted it.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        try:
            cursor.execute(sql, params)
        finally:
            cursor.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
