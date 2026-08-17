"""
Task 3.3's boundary: it reads, and it writes exactly one kind of row.

Costing is a **derived read**. The only business records it creates are the
append-only cost snapshots, and nothing it does moves stock, touches a balance,
writes a journal line, or produces a production batch. That claim is worth
counting rather than asserting: the failure mode is a service that grows one
posting call in a later task and nobody notices, because the test only checked
the total.

Every test below takes a full before/after census of six tables and compares
them, so a movement written anywhere in the call stack fails here.

The second half is the **Task 3.4 boundary**: no `ProductionBatch`, no flatten
endpoint, no production route, and no cost field on `Recipe` or `RecipeVersion`.
A cached cost on either model is the specific thing RCP-009 forbids, and it is
the sort of field that arrives as a "small optimisation".
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from apps.accounting.models import JournalEntry, JournalLine
from apps.inventory.models import (
    InventoryItem,
    StockBalance,
    StockLedgerEntry,
    StockLocationBalance,
    StockMovement,
    Warehouse,
)
from apps.kitchen.costing import cost_recipe_on_date, cost_recipe_version, preview_recipe_cost
from apps.kitchen.models import Recipe, RecipeCostSnapshot, RecipeVersion
from apps.kitchen.snapshots import create_recipe_cost_snapshot
from apps.organizations.models import Branch, Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db


def _today() -> datetime.date:
    from django.utils import timezone

    return timezone.localdate()


def _census() -> tuple[int, ...]:
    """
    Everything Task 3.3 must not create, counted in one place.

    A tuple rather than a dict so a test reads `before == after` and cannot
    accidentally compare a subset.
    """
    return (
        StockMovement.objects.count(),
        StockLedgerEntry.objects.count(),
        StockBalance.objects.count(),
        StockLocationBalance.objects.count(),
        JournalEntry.objects.count(),
        JournalLine.objects.count(),
    )


def _balances() -> list[tuple[int, Decimal, Decimal]]:
    """
    Not only *how many* balances, but what they say.

    A costing read that repriced a position would leave the count identical and
    the figures different, which is exactly the bug this catches.
    """
    return [(row.pk, row.quantity, row.value) for row in StockBalance.objects.order_by("pk")]


class TestZeroEffect:
    def test_a_preview_moves_nothing(
        self, valued_store: Warehouse, complete_draft: RecipeVersion
    ) -> None:
        before, balances = _census(), _balances()
        preview_recipe_cost(version=complete_draft, warehouse=valued_store, as_of_date=_today())
        assert _census() == before
        assert _balances() == balances

    def test_an_authoritative_cost_moves_nothing(
        self, valued_store: Warehouse, costable_version: RecipeVersion
    ) -> None:
        before, balances = _census(), _balances()
        cost_recipe_version(version=costable_version, warehouse=valued_store, as_of_date=_today())
        assert _census() == before
        assert _balances() == balances

    def test_a_historical_cost_moves_nothing(
        self, valued_store: Warehouse, costable_version: RecipeVersion, branch: Branch
    ) -> None:
        before, balances = _census(), _balances()
        cost_recipe_on_date(
            recipe=costable_version.recipe,
            branch=branch,
            warehouse=valued_store,
            on_date=_today(),
        )
        assert _census() == before
        assert _balances() == balances

    def test_creating_a_snapshot_moves_nothing_but_the_snapshot(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> None:
        """
        The one write, and it is not a posting.

        Nothing about a costing record touches a balance or a ledger; it is a
        statement about what the books already said.
        """
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        before, balances = _census(), _balances()
        create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="ZERO-1")
        assert _census() == before
        assert _balances() == balances
        assert RecipeCostSnapshot.objects.count() == 1

    def test_a_snapshot_retry_moves_nothing_and_adds_no_row(
        self, valued_store: Warehouse, costable_version: RecipeVersion, manager: User
    ) -> None:
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="ZERO-2")
        before, balances = _census(), _balances()
        snapshots = RecipeCostSnapshot.objects.count()
        create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="ZERO-2")
        assert _census() == before
        assert _balances() == balances
        assert RecipeCostSnapshot.objects.count() == snapshots

    def test_verifying_snapshots_moves_nothing(
        self,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
        organization: Organization,
        manager: User,
    ) -> None:
        """A verifier that could change a figure it verifies would be the one
        place a discrepancy could be made to disappear."""
        from apps.kitchen.cost_reconciliation import recompute_findings, verify_cost_snapshots

        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        snapshot = create_recipe_cost_snapshot(card=card, actor=manager, idempotency_key="ZERO-3")
        before, balances = _census(), _balances()
        totals = (
            snapshot.total_material_cost,
            list(snapshot.lines.values_list("allocated_extension", flat=True)),
        )
        verify_cost_snapshots(organization)
        recompute_findings(snapshot)
        assert _census() == before
        assert _balances() == balances
        refreshed = RecipeCostSnapshot.objects.get(pk=snapshot.pk)
        assert (
            refreshed.total_material_cost,
            list(refreshed.lines.values_list("allocated_extension", flat=True)),
        ) == totals

    def test_the_cost_screens_move_nothing(
        self,
        cost_reader_client: object,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        from django.urls import reverse

        before, balances = _census(), _balances()
        cost_reader_client.get(  # type: ignore[attr-defined]
            reverse("kitchen:cost_card", args=[costable_version.pk]),
            {"warehouse": valued_store.pk, "as_of_date": _today().isoformat()},
        )
        assert _census() == before
        assert _balances() == balances


class TestTheTaskBoundary:
    def test_no_production_row_carries_a_cost(self) -> None:
        """
        Task 3.3 asserted that no production model existed; **Task 3.4 built
        three**, and this test moved with them rather than being deleted.

        The claim it guards is the one that still matters here: a production
        draft carries **no money**. Costing owns cost, `view_recipe_cost` guards
        it, and a batch that stored one would be a second answer to a question
        the ledger already answers — going stale the moment stock moved.
        """
        from apps.kitchen.models import (
            ProductionBatch,
            ProductionBatchActualLine,
            ProductionBatchLine,
        )

        money = ("cost", "price", "value", "amount", "total")
        for model in (ProductionBatch, ProductionBatchLine, ProductionBatchActualLine):
            for field in model._meta.get_fields():
                if not getattr(field, "concrete", False):
                    continue
                name = field.name.lower()
                # `cost_class` is the recipe's FOOD / PACKAGING classification
                # and carries no figure; every other match would be an amount.
                if name == "cost_class":
                    continue
                assert not any(word in name for word in money), (
                    f"{model.__name__}.{field.name} looks like money"
                )

    def test_no_cost_field_was_added_to_recipe_or_version(self) -> None:
        """
        RCP-009: a recipe carries **no** cost field.

        A stored "current cost" would be a copy of the ledger's moving average
        that starts drifting the moment the next receipt posts — and it is
        exactly the field that arrives one day as a small optimisation.
        """
        forbidden = {"cost", "unit_cost", "current_cost", "standard_cost", "price"}
        for model in (Recipe, RecipeVersion):
            assert not ({field.name for field in model._meta.get_fields()} & forbidden)

    def test_the_router_publishes_drafting_screens_and_no_posting_screen(self) -> None:
        """
        Task 3.4 brought the drafting screens in, so the original blanket ban
        on `production` is now false and this was rewritten rather than
        deleted. What survives is the half about posting: no route may post,
        reverse, flatten, journal or touch an inventory document.
        """
        from apps.kitchen.urls import urlpatterns

        published = {
            getattr(route, "name", "") for route in urlpatterns if getattr(route, "name", "")
        }
        assert "production_list" in published, "Task 3.4 owns the drafting screens"
        for banned in ("flatten", "post", "reverse", "journal", "inventory", "lot", "location"):
            assert not any(banned in name for name in published), banned

    def test_costing_never_reaches_for_a_procurement_price(self) -> None:
        """
        RCP-023 names one basis: the warehouse moving average under
        POSTED_AS_OF. A supplier quotation, a last purchase price or a
        purchase-order price would each be a different number wearing this
        one's name.
        """
        import pathlib

        source = pathlib.Path("apps/kitchen/costing.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("*"))
        assert "procurement" not in code.lower().replace("procurement changes", "")
        assert "PurchaseOrder" not in code
        assert "SupplierCatalogue" not in code

    def test_inventory_does_not_import_kitchen(self) -> None:
        """
        The dependency direction, checked rather than remembered.

        Kitchen reads a public Inventory service; Inventory knows nothing about
        recipes. The new `apps/inventory/valuation.py` is the module most likely
        to break this, so it is read explicitly.
        """
        import pathlib

        for path in pathlib.Path("apps/inventory").rglob("*.py"):
            if "tests" in path.parts:
                continue
            assert "apps.kitchen" not in path.read_text(encoding="utf-8"), path

    def test_accounting_does_not_import_kitchen(self) -> None:
        import pathlib

        for path in pathlib.Path("apps/accounting").rglob("*.py"):
            if "tests" in path.parts:
                continue
            assert "apps.kitchen" not in path.read_text(encoding="utf-8"), path

    def test_no_second_inventory_or_recipe_ledger_was_created(self) -> None:
        """
        Costing reads the one ledger there is. A parallel table of unit costs
        would be a second opinion about a settled figure, and the two would
        disagree the first time somebody posted a receipt.
        """
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("kitchen").get_models()}
        for banned in ("StockMovement", "StockBalance", "StockLedgerEntry", "ItemCost"):
            assert banned not in names


class TestTheValuationServiceIsReadOnly:
    def test_it_writes_nothing(
        self,
        valued_store: Warehouse,
        organization: Organization,
        rice: InventoryItem,
    ) -> None:
        from apps.inventory.valuation import posted_cutoff, valuation_at_cutoff

        before, balances = _census(), _balances()
        cutoff = posted_cutoff(organization=organization, as_of_date=_today())
        valuation_at_cutoff(warehouse=valued_store, item_ids=[rice.pk], cutoff=cutoff)
        assert _census() == before
        assert _balances() == balances

    def test_its_source_contains_no_write(self) -> None:
        """
        A read module that grew a `save()` would be the first place a costing
        query started repricing what it reports.
        """
        import pathlib

        source = pathlib.Path("apps/inventory/valuation.py").read_text(encoding="utf-8")
        code = "\n".join(
            line
            for line in source.splitlines()
            if not line.strip().startswith("#") and not line.strip().startswith("*")
        )
        for banned in (".save(", ".create(", ".update(", ".delete(", "bulk_create"):
            assert banned not in code
