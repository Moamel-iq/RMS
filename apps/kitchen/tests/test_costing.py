"""
Standard recipe costing: the arithmetic, the valuation, and what it refuses.

The claims this file holds, and why each one is worth a test rather than a
docstring:

* **Two lots aggregate by quantity and value, never by averaging averages.**
  The fixture posts 100 KG at 1,000 and 100 KG at 2,000 precisely so the two
  answers differ from each other only when the lots hold unequal quantities —
  which is the case a pairwise average gets right by accident and this one
  gets right on purpose.
* **`POSTED_AS_OF` means a prefix of the posting order.** A movement posted
  after the as-of date is wholly outside the card, and the cutoff integer that
  says so is on the card.
* **Missing valuation is never zero.** Three shapes — no position, an emptied
  position, and a position with quantity and no value — and only the third is
  a cost.
* **Gross quantities, no loss, no yield, no substitutes.** Each of those is one
  multiplication away from a plausible wrong number.
* **The document total is the sum of its lines**, to the fils, including when
  the raw extensions do not divide evenly.

Nothing here posts, and `TestZeroEffect` in `test_cost_boundary.py` is what
holds that line.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.inventory.models import InventoryItem, Warehouse
from apps.inventory.valuation import ValuationState, posted_cutoff, valuation_at_cutoff
from apps.kitchen.costing import (
    CALCULATION_VERSION,
    cost_recipe_on_date,
    cost_recipe_version,
    preview_recipe_cost,
)
from apps.kitchen.models import Recipe, RecipeLineCostClass, RecipeVersion
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_line_substitute,
    add_recipe_serving,
    update_recipe_line,
)
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import post_issue, post_receipt

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 1, 1)


def codes_of(error: Any) -> set[str]:
    """
    Every stable code inside a `ValidationError`, however it was nested.

    `hasattr(error, "message")` is tested **first** and the recursion into
    `error_list` second, and the order is not cosmetic: a single-message
    `ValidationError` carries *both* attributes — its own `error_list` is
    `[self]` — so recursing first loops straight past the code this helper
    exists to read. Same shape as the helper in `test_components.py`.
    """
    if hasattr(error, "message"):
        return {error.code or ""}
    if hasattr(error, "error_dict"):
        return {
            code
            for errors in error.error_dict.values()
            for item in errors
            for code in codes_of(item)
        }
    if hasattr(error, "error_list"):
        return {code for item in error.error_list for code in codes_of(item)}
    return set()


def _today() -> datetime.date:
    """
    The date every card here is read at.

    `posted_at` is `auto_now_add`, so the fixtures' movements carry the wall
    clock. A fixed historical date would read an empty ledger and every test in
    this file would pass against a card full of holes — which is exactly the
    failure the missing-valuation tests exist to detect, and it must not be the
    accidental state of the others.
    """
    from django.utils import timezone

    return timezone.localdate()


# ---------------------------------------------------------------------------
# The Inventory read
# ---------------------------------------------------------------------------


class TestTheValuationRead:
    def test_two_lots_aggregate_by_quantity_and_value(
        self, valued_store: Warehouse, organization: Organization, rice: InventoryItem
    ) -> None:
        """
        100 @ 1,000 plus 100 @ 2,000 is 200 KG worth 300,000, so 1,500 each.

        Averaging the two averages gives the same 1,500 here only because the
        quantities happen to be equal; `test_unequal_lots_are_not_averaged_pairwise`
        is the one that separates the methods.
        """
        cutoff = posted_cutoff(organization=organization, as_of_date=_today())
        valuation = valuation_at_cutoff(warehouse=valued_store, item_ids=[rice.pk], cutoff=cutoff)[
            rice.pk
        ]
        assert valuation.state is ValuationState.AVAILABLE
        assert valuation.quantity == Decimal("200.000")
        assert valuation.value == Decimal("300000.000")
        assert valuation.unit_cost == Decimal("1500.000000")

    def test_unequal_lots_are_not_averaged_pairwise(
        self,
        open_period: object,
        organization: Organization,
        store: Warehouse,
        oil: InventoryItem,
    ) -> None:
        """
        Value-weighted, not count-weighted. The two methods disagree here.

        90 @ 1,000 + 10 @ 2,000 = 110,000 over 100 = **1,100**.
        The average of the averages would be 1,500 — a 36% error, and the whole
        reason ADR-018 says the warehouse average is derived from the totals.
        """
        post_receipt(
            organization=organization,
            warehouse=store,
            item=oil,
            quantity="90",
            unit_cost="1000",
            key="uneven-1",
        )
        post_receipt(
            organization=organization,
            warehouse=store,
            item=oil,
            quantity="10",
            unit_cost="2000",
            key="uneven-2",
        )
        cutoff = posted_cutoff(organization=organization, as_of_date=_today())
        valuation = valuation_at_cutoff(warehouse=store, item_ids=[oil.pk], cutoff=cutoff)[oil.pk]
        assert valuation.unit_cost == Decimal("1100.000000")

    def test_an_item_with_no_position_is_unavailable_not_free(
        self, valued_store: Warehouse, organization: Organization, cooked_rice: InventoryItem
    ) -> None:
        cutoff = posted_cutoff(organization=organization, as_of_date=_today())
        valuation = valuation_at_cutoff(
            warehouse=valued_store, item_ids=[cooked_rice.pk], cutoff=cutoff
        )[cooked_rice.pk]
        assert valuation.state is ValuationState.NO_POSITION
        assert not valuation.is_available

    def test_an_emptied_position_is_unavailable_not_free(
        self,
        open_period: object,
        organization: Organization,
        store: Warehouse,
        oil: InventoryItem,
    ) -> None:
        """Movements happened and the shelf is empty. `value / 0` has no answer."""
        post_receipt(
            organization=organization,
            warehouse=store,
            item=oil,
            quantity="10",
            unit_cost="1000",
            key="empty-1",
        )
        post_issue(
            organization=organization,
            warehouse=store,
            item=oil,
            quantity="10",
            key="empty-2",
        )
        cutoff = posted_cutoff(organization=organization, as_of_date=_today())
        valuation = valuation_at_cutoff(warehouse=store, item_ids=[oil.pk], cutoff=cutoff)[oil.pk]
        assert valuation.state is ValuationState.ZERO_QUANTITY
        assert not valuation.is_available

    def test_a_positive_quantity_with_no_value_is_a_real_zero_cost_position(
        self,
        open_period: object,
        organization: Organization,
        store: Warehouse,
        oil: InventoryItem,
    ) -> None:
        """Free stock is worth nothing and is still valued. That is not a hole."""
        post_receipt(
            organization=organization,
            warehouse=store,
            item=oil,
            quantity="10",
            unit_cost="0",
            key="free-1",
        )
        cutoff = posted_cutoff(organization=organization, as_of_date=_today())
        valuation = valuation_at_cutoff(warehouse=store, item_ids=[oil.pk], cutoff=cutoff)[oil.pk]
        assert valuation.state is ValuationState.AVAILABLE
        assert valuation.unit_cost == Decimal("0.000000")

    def test_a_movement_after_the_as_of_date_is_wholly_excluded(
        self, valued_store: Warehouse, organization: Organization, rice: InventoryItem
    ) -> None:
        """
        The cutoff is a prefix of the posting order, so yesterday's card cannot
        see today's receipt — and its own cutoff integer says which prefix.
        """
        yesterday = _today() - datetime.timedelta(days=1)
        cutoff = posted_cutoff(organization=organization, as_of_date=yesterday)
        assert cutoff.posted_sequence == 0
        valuation = valuation_at_cutoff(warehouse=valued_store, item_ids=[rice.pk], cutoff=cutoff)[
            rice.pk
        ]
        assert valuation.state is ValuationState.NO_POSITION

    def test_the_cutoff_is_captured_once_and_reported(
        self, valued_store: Warehouse, organization: Organization, rice: InventoryItem
    ) -> None:
        cutoff = posted_cutoff(organization=organization, as_of_date=_today())
        assert cutoff.posted_sequence > 0
        assert str(cutoff.mode) == "POSTED_AS_OF"

    def test_another_organizations_warehouse_is_a_programming_error(
        self,
        valued_store: Warehouse,
        organization: Organization,
        rival_store: Warehouse,
        rice: InventoryItem,
    ) -> None:
        cutoff = posted_cutoff(organization=organization, as_of_date=_today())
        with pytest.raises(ValueError):
            valuation_at_cutoff(warehouse=rival_store, item_ids=[rice.pk], cutoff=cutoff)

    def test_every_requested_item_gets_a_row(
        self,
        valued_store: Warehouse,
        organization: Organization,
        rice: InventoryItem,
        cooked_rice: InventoryItem,
    ) -> None:
        """A missing key would be indistinguishable from a zero cost to a caller."""
        cutoff = posted_cutoff(organization=organization, as_of_date=_today())
        valuations = valuation_at_cutoff(
            warehouse=valued_store, item_ids=[rice.pk, cooked_rice.pk], cutoff=cutoff
        )
        assert set(valuations) == {rice.pk, cooked_rice.pk}


# ---------------------------------------------------------------------------
# Direct lines
# ---------------------------------------------------------------------------


class TestDirectLines:
    def test_one_line_costs_quantity_times_the_warehouse_average(
        self, valued_store: Warehouse, costable_version: RecipeVersion
    ) -> None:
        """4 KG of rice at 1,500 is 6,000, and the total is the sum of the lines."""
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        assert len(card.lines) == 1
        line = card.lines[0]
        assert line.effective_quantity == Decimal("4.000000")
        assert line.unit_cost == Decimal("1500.000000")
        assert line.allocated_extension == Decimal("6000.000")
        assert card.total_material_cost == Decimal("6000.000")

    def test_several_lines_sum_exactly(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        litre: UnitOfMeasure,
        piece: UnitOfMeasure,
        rice: InventoryItem,
        oil: InventoryItem,
        box: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        version = _three_line_version(
            recipe=recipe,
            kilogram=kilogram,
            litre=litre,
            piece=piece,
            rice=rice,
            oil=oil,
            box=box,
            author=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert len(card.lines) == 3
        assert sum(line.allocated_extension for line in card.lines) == card.total_material_cost

    def test_class_totals_add_up_to_the_document_total(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        litre: UnitOfMeasure,
        piece: UnitOfMeasure,
        rice: InventoryItem,
        oil: InventoryItem,
        box: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """`KM-RCP-004`'s summary: food + packaging + accompaniment == total."""
        version = _three_line_version(
            recipe=recipe,
            kilogram=kilogram,
            litre=litre,
            piece=piece,
            rice=rice,
            oil=oil,
            box=box,
            author=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert card.food_total + card.packaging_total + card.accompaniment_total == (
            card.total_material_cost
        )
        assert card.packaging_total > Decimal("0")

    def test_an_optional_line_is_costed_by_default(
        self,
        valued_store: Warehouse,
        complete_draft: RecipeVersion,
        kilogram: UnitOfMeasure,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        RCP-021: an optional line is a real ingredient that is sometimes
        skipped, and costing it at zero would understate every plate that has
        it. Marking the only line optional must change nothing about the card.
        """
        # `update_recipe_line` replaces the whole line rather than patching a
        # field, so the quantity is restated unchanged.
        update_recipe_line(
            line=complete_draft.lines.get(),
            entered_quantity=Decimal("4"),
            entered_unit=kilogram,
            is_optional=True,
        )
        version = _activate(
            complete_draft,
            author=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert card.lines[0].recipe_line.is_optional is True
        assert card.total_material_cost == Decimal("6000.000")

    def test_a_substitute_never_enters_the_cost(
        self,
        valued_store: Warehouse,
        complete_draft: RecipeVersion,
        oil: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """RCP-022: a substitute is what a batch *may* use, not what the version says."""
        line = complete_draft.lines.get()
        add_recipe_line_substitute(line=line, substitute_item=oil, priority=1)
        version = _activate(
            complete_draft,
            author=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert [line.item.code for line in card.lines] == ["RICE"]

    def test_loss_and_yield_are_not_applied_a_second_time(
        self,
        valued_store: Warehouse,
        complete_draft: RecipeVersion,
        kilogram: UnitOfMeasure,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        RCP-018 / RCP-060: the costing input is the **gross** approved quantity.

        The line records a 25% loss rate. If costing multiplied by it, the
        extension would be 4,500 rather than 6,000 — and the loss would have
        been counted twice, because the gross figure already expresses it.
        """
        line = complete_draft.lines.get()
        update_recipe_line(
            line=line,
            entered_quantity=Decimal("4"),
            entered_unit=kilogram,
            loss_rate=Decimal("0.25"),
        )
        version = _activate(
            complete_draft,
            author=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert card.lines[0].effective_quantity == Decimal("4.000000")
        assert card.total_material_cost == Decimal("6000.000")

    def test_no_float_reaches_any_result_field(
        self, valued_store: Warehouse, costable_version: RecipeVersion
    ) -> None:
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        numbers = [
            card.total_material_cost,
            card.cost_per_output_unit,
            card.food_total,
            card.packaging_total,
            card.accompaniment_total,
            *[line.effective_quantity for line in card.lines],
            *[line.unit_cost for line in card.lines],
            *[line.raw_extension for line in card.lines],
            *[line.allocated_extension for line in card.lines],
            *[serving.cost_per_serving for serving in card.servings],
        ]
        assert all(isinstance(value, Decimal) for value in numbers)

    def test_no_result_field_is_named_after_profit(
        self, valued_store: Warehouse, costable_version: RecipeVersion
    ) -> None:
        """Task 3.3 calculates direct material cost, not commercial profitability."""
        card = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        forbidden = {
            "profit",
            "net_profit",
            "gross_profit",
            "margin",
            "contribution_margin",
            "selling_price",
            "price",
        }
        assert not (set(vars(card)) & forbidden)
        assert not (set(vars(card.lines[0])) & forbidden)


# ---------------------------------------------------------------------------
# Preview, authority and the resolver
# ---------------------------------------------------------------------------


class TestPreviewAndAuthority:
    def test_a_draft_preview_is_marked_non_authoritative(
        self, valued_store: Warehouse, complete_draft: RecipeVersion
    ) -> None:
        card = preview_recipe_cost(
            version=complete_draft, warehouse=valued_store, as_of_date=_today()
        )
        assert card.is_authoritative is False
        assert card.total_material_cost > Decimal("0")

    def test_a_submitted_preview_is_marked_non_authoritative(
        self, valued_store: Warehouse, complete_draft: RecipeVersion, manager: User
    ) -> None:
        from apps.kitchen.lifecycle import submit_recipe_version

        submit_recipe_version(version=complete_draft, actor=manager)
        card = preview_recipe_cost(
            version=RecipeVersion.objects.get(pk=complete_draft.pk),
            warehouse=valued_store,
            as_of_date=_today(),
        )
        assert card.is_authoritative is False

    def test_a_draft_has_no_authoritative_cost(
        self, valued_store: Warehouse, complete_draft: RecipeVersion
    ) -> None:
        with pytest.raises(ValidationError) as refusal:
            cost_recipe_version(version=complete_draft, warehouse=valued_store, as_of_date=_today())
        assert "recipe_cost_version_not_authoritative" in codes_of(refusal.value)

    def test_a_rejected_version_has_no_authoritative_cost(
        self,
        valued_store: Warehouse,
        complete_draft: RecipeVersion,
        manager: User,
        approver: User,
    ) -> None:
        """A refusal somebody signed is not a costing record."""
        from apps.kitchen.lifecycle import reject_recipe_version, submit_recipe_version

        submit_recipe_version(version=complete_draft, actor=manager)
        reject_recipe_version(
            version=RecipeVersion.objects.get(pk=complete_draft.pk),
            actor=approver,
            reason="تجربة رفض",
        )
        with pytest.raises(ValidationError) as refusal:
            cost_recipe_version(
                version=RecipeVersion.objects.get(pk=complete_draft.pk),
                warehouse=valued_store,
                as_of_date=_today(),
            )
        assert "recipe_cost_version_not_authoritative" in codes_of(refusal.value)

    def test_an_approved_version_is_costed_authoritatively(
        self,
        valued_store: Warehouse,
        approved_version: RecipeVersion,
    ) -> None:
        card = cost_recipe_version(
            version=approved_version, warehouse=valued_store, as_of_date=_today()
        )
        assert card.is_authoritative is True
        assert card.calculation_version == CALCULATION_VERSION

    def test_an_approved_version_cannot_be_previewed(
        self, valued_store: Warehouse, approved_version: RecipeVersion
    ) -> None:
        """It has an authoritative answer and should be asked for it."""
        with pytest.raises(ValidationError) as refusal:
            preview_recipe_cost(
                version=approved_version, warehouse=valued_store, as_of_date=_today()
            )
        assert "recipe_cost_version_not_previewable" in codes_of(refusal.value)

    def test_a_foreign_warehouse_is_refused(
        self,
        costable_version: RecipeVersion,
        rival_store: Warehouse,
    ) -> None:
        with pytest.raises(ValidationError) as refusal:
            cost_recipe_version(
                version=costable_version, warehouse=rival_store, as_of_date=_today()
            )
        assert "recipe_cost_wrong_warehouse" in codes_of(refusal.value)

    def test_the_historical_read_resolves_the_version_first(
        self,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
        branch: Branch,
    ) -> None:
        card = cost_recipe_on_date(
            recipe=costable_version.recipe,
            branch=branch,
            warehouse=valued_store,
            on_date=_today(),
        )
        assert card.version.pk == costable_version.pk
        assert card.as_of_date == _today()

    def test_a_date_with_no_effective_version_returns_nothing(
        self,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
        branch: Branch,
    ) -> None:
        """The honest answer to "what did it cost before it existed"."""
        with pytest.raises(ValidationError) as refusal:
            cost_recipe_on_date(
                recipe=costable_version.recipe,
                branch=branch,
                warehouse=valued_store,
                on_date=datetime.date(2025, 1, 1),
            )
        assert "recipe_version_not_effective" in codes_of(refusal.value)

    def test_a_warehouse_outside_the_branch_is_refused(
        self,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
        second_branch: Branch,
    ) -> None:
        with pytest.raises(ValidationError) as refusal:
            cost_recipe_on_date(
                recipe=costable_version.recipe,
                branch=second_branch,
                warehouse=valued_store,
                on_date=_today(),
            )
        assert "recipe_cost_wrong_warehouse" in codes_of(refusal.value)

    def test_no_costing_entry_point_defaults_a_date(self) -> None:
        """
        Every signature is keyword-only with a required date.

        A read that quietly meant *today* would be right during development and
        wrong the first time somebody re-ran a July card in September.
        """
        import inspect

        for function in (preview_recipe_cost, cost_recipe_version):
            parameters = inspect.signature(function).parameters
            assert parameters["as_of_date"].default is inspect.Parameter.empty
        assert (
            inspect.signature(cost_recipe_on_date).parameters["on_date"].default
            is inspect.Parameter.empty
        )


# ---------------------------------------------------------------------------
# Missing valuation
# ---------------------------------------------------------------------------


class TestMissingValuation:
    def test_an_unvalued_leaf_is_reported_and_never_priced_at_zero(
        self,
        valued_store: Warehouse,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        cooked_rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        from .conftest import build_complete_draft

        draft = build_complete_draft(recipe=recipe, unit=kilogram, item=cooked_rice, author=manager)
        version = _activate(
            draft,
            author=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        card = cost_recipe_version(version=version, warehouse=valued_store, as_of_date=_today())
        assert not card.is_complete
        assert len(card.missing) == 1
        gap = card.missing[0]
        assert gap.item_code == cooked_rice.code
        assert gap.code == "recipe_cost_item_not_valued"
        assert gap.state is ValuationState.NO_POSITION
        # No fallback price was invented.
        assert card.lines[0].unit_cost == Decimal("0")
        assert card.total_material_cost == Decimal("0.000")

    def test_a_second_warehouse_answers_differently(
        self,
        valued_store: Warehouse,
        second_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        """
        Cost is warehouse-specific, and the second store holds nothing.

        The same version, the same date, and a different answer - which is why
        the warehouse is an input and never a default.
        """
        here = cost_recipe_version(
            version=costable_version, warehouse=valued_store, as_of_date=_today()
        )
        there = cost_recipe_version(
            version=costable_version, warehouse=second_store, as_of_date=_today()
        )
        assert here.is_complete
        assert not there.is_complete
        assert there.total_material_cost == Decimal("0.000")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _activate(
    draft: RecipeVersion,
    *,
    author: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> RecipeVersion:
    from apps.kitchen.lifecycle import activate_recipe_version

    from .conftest import carry_to_approved

    approved = carry_to_approved(
        draft,
        submitter=author,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
    )
    activate_recipe_version(version=approved, actor=approver, effective_from=TODAY)
    return RecipeVersion.objects.get(pk=approved.pk)


def _three_line_version(
    *,
    recipe: Recipe,
    kilogram: UnitOfMeasure,
    litre: UnitOfMeasure,
    piece: UnitOfMeasure,
    rice: InventoryItem,
    oil: InventoryItem,
    box: InventoryItem,
    author: User,
    cook: User,
    keeper: User,
    accountant: User,
    approver: User,
) -> RecipeVersion:
    """Food, food and packaging — the split the workbook's summary needs."""
    from apps.kitchen.services import create_draft_recipe_version

    draft = create_draft_recipe_version(
        recipe=recipe,
        batch_size=Decimal("1"),
        expected_output_quantity=Decimal("10"),
        output_unit=kilogram,
        instructions="نظرة عامة.",
        created_by=author,
    )
    add_recipe_line(
        version=draft,
        item=rice,
        entered_quantity=Decimal("4"),
        entered_unit=kilogram,
        cost_class=RecipeLineCostClass.FOOD,
    )
    add_recipe_line(
        version=draft,
        item=oil,
        entered_quantity=Decimal("0.5"),
        entered_unit=litre,
        cost_class=RecipeLineCostClass.FOOD,
    )
    add_recipe_line(
        version=draft,
        item=box,
        entered_quantity=Decimal("10"),
        entered_unit=piece,
        cost_class=RecipeLineCostClass.PACKAGING,
    )
    from apps.kitchen.services import add_recipe_step

    add_recipe_step(version=draft, instruction_ar="خطوة.")
    add_recipe_serving(
        version=draft,
        code="ONE",
        name_ar="حصة",
        serving_quantity=Decimal("1"),
        serving_unit=kilogram,
        is_primary=True,
    )
    return _activate(
        RecipeVersion.objects.get(pk=draft.pk),
        author=author,
        cook=cook,
        keeper=keeper,
        accountant=accountant,
        approver=approver,
    )
