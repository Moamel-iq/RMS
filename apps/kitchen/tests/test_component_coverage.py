"""
Effective coverage at activation, and what happens to an exact reference
afterwards.

Two questions that look alike and are not, which is the whole subject of this
file.

**Selecting a version** is a date question. `resolve_recipe_version` answers it
for a new, independent transaction: *which version governs this branch on this
day?*

**The validity of an already-frozen reference** is not a date question at all.
`RecipeComponent.component_version` is an immutable foreign key to a specific
frozen row. Once a parent is activated against it, that reference stays valid —
including after the child is superseded for new selection. A blend replaced in
September does not retroactively empty the July dish that named it.

So the gate is at **initial activation only**: for every applicable branch the
child must be effective on the parent's `effective_from`. The child's range is
**not** required to cover the parent's future, and an open-ended parent does
**not** require an open-ended child.

An earlier implementation required exactly that, and blocked child supersession
while any active parent still referenced the child. Both rules are gone. The
tests that asserted them were rewritten rather than deleted, and several now
assert the opposite — deliberately, because that is what changed.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.inventory.models import InventoryItem
from apps.kitchen.graph import component_tree, coverage_gaps, dependents_of, flatten_tree
from apps.kitchen.lifecycle import activate_recipe_version, resolve_recipe_version
from apps.kitchen.models import Recipe, RecipeComponent, RecipeVersion, RecipeVersionStatus
from apps.kitchen.services import create_recipe_component
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import build_complete_draft, carry_to_active, carry_to_approved, make_child_recipe
from .test_components import codes_of

pytestmark = pytest.mark.django_db

JANUARY = datetime.date(2026, 1, 1)
MARCH = datetime.date(2026, 3, 1)
JUNE_END = datetime.date(2026, 6, 30)
JULY = datetime.date(2026, 7, 1)
SEPTEMBER = datetime.date(2026, 9, 1)
DECEMBER_END = datetime.date(2026, 12, 31)


def _parent_with_child(
    *,
    recipe: Recipe,
    child: RecipeVersion,
    unit: UnitOfMeasure,
    item: InventoryItem,
    author: User,
    multiplier: Decimal = Decimal("0.5"),
) -> RecipeVersion:
    """A fresh draft of `recipe` that names `child` as its one component."""
    draft = build_complete_draft(recipe=recipe, unit=unit, item=item, author=author)
    create_recipe_component(version=draft, component_version=child, multiplier=multiplier)
    return draft


class TestCoverageAtActivation:
    """
    The gate at initial activation: the child must be effective **on the
    parent's start date**, at every applicable branch.

    Not across the parent's whole range — that rule existed, pinned children
    forever, and was removed.
    """

    def test_a_child_effective_earlier_and_open_ended_covers_the_parent(
        self,
        recipe: Recipe,
        blend_active: RecipeVersion,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """The ordinary case: blend from January, dish from July. Accepted."""
        parent = _parent_with_child(
            recipe=recipe, child=blend_active, unit=kilogram, item=rice, author=manager
        )
        activated = carry_to_active(
            parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JULY,
        )
        assert activated.status == RecipeVersionStatus.ACTIVE

    def test_exact_boundary_dates_are_covered_inclusively(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        Child `[1 Jan, 30 Jun]`, parent `[1 Jan, 30 Jun]`. Identical at both
        ends, and accepted — the range convention is inclusive, so the last day
        is covered rather than one short.
        """
        child_draft = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        child = carry_to_active(
            child_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JANUARY,
            effective_to=JUNE_END,
        )
        parent = _parent_with_child(
            recipe=recipe, child=child, unit=kilogram, item=rice, author=manager
        )
        activated = carry_to_active(
            parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JANUARY,
            effective_to=JUNE_END,
        )
        assert activated.effective_to == JUNE_END

    def test_a_child_that_starts_after_the_parent_is_refused(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        child_draft = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        child = carry_to_active(
            child_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JULY,
        )
        parent = _parent_with_child(
            recipe=recipe, child=child, unit=kilogram, item=rice, author=manager
        )
        carry_to_approved(
            parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        with pytest.raises(ValidationError) as caught:
            activate_recipe_version(version=parent, actor=approver, effective_from=MARCH)
        assert "recipe_component_not_effective" in codes_of(caught.value)

    def test_a_child_whose_range_ended_before_the_parent_starts_is_refused(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        The child ran January-June; the parent wants to start in July. The child
        is not effective on the parent's start date, so activation is refused.

        Contrast with the next test: a child that ends *after* the parent starts
        is fine, however far the parent then runs.
        """
        child_draft = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        child = carry_to_active(
            child_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JANUARY,
            effective_to=JUNE_END,
        )
        parent = _parent_with_child(
            recipe=recipe, child=child, unit=kilogram, item=rice, author=manager
        )
        carry_to_approved(
            parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        with pytest.raises(ValidationError) as caught:
            activate_recipe_version(version=parent, actor=approver, effective_from=JULY)
        assert "recipe_component_not_effective" in codes_of(caught.value)

    def test_a_bounded_child_does_not_limit_how_long_the_parent_runs(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        Child `[1 Jan, 30 Jun]`, parent `[1 Jan, 31 Dec]`. **Accepted.**

        This is the rule that was removed. The parent is not claiming that the
        child is selectable in December — it is claiming that it contains one
        exact frozen version, which it does, permanently.
        """
        child_draft = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        child = carry_to_active(
            child_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JANUARY,
            effective_to=JUNE_END,
        )
        parent = _parent_with_child(
            recipe=recipe, child=child, unit=kilogram, item=rice, author=manager
        )
        activated = carry_to_active(
            parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JANUARY,
            effective_to=DECEMBER_END,
        )
        assert activated.status == RecipeVersionStatus.ACTIVE
        assert activated.effective_to == DECEMBER_END

    def test_an_open_ended_parent_does_not_need_an_open_ended_child(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        **The rule this test used to assert has been removed**, and this asserts
        its removal rather than being deleted.

        Requiring an open-ended child pinned that child forever: any close date
        would leave the open-ended parent "uncovered", so the blend could never
        be replaced. That confused *selecting* a version by date with the
        *validity of a frozen reference*, which no date can invalidate.
        """
        child_draft = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        child = carry_to_active(
            child_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JANUARY,
            effective_to=DECEMBER_END,
        )
        parent = _parent_with_child(
            recipe=recipe, child=child, unit=kilogram, item=rice, author=manager
        )
        activated = carry_to_active(
            parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JANUARY,
        )
        assert activated.status == RecipeVersionStatus.ACTIVE
        assert activated.effective_to is None

    def test_a_child_covering_one_branch_but_not_the_other_is_refused(
        self,
        recipe: Recipe,
        blend: Recipe,
        branch: Branch,
        second_branch: Branch,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """Branch A is covered, Branch B is not. No partial activation."""
        child_draft = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        child = carry_to_active(
            child_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JANUARY,
            branches=[branch],
        )
        parent = _parent_with_child(
            recipe=recipe, child=child, unit=kilogram, item=rice, author=manager
        )
        carry_to_approved(
            parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        with pytest.raises(ValidationError) as caught:
            activate_recipe_version(
                version=parent,
                actor=approver,
                effective_from=JULY,
                branches=[branch, second_branch],
            )
        assert "recipe_component_branch_mismatch" in codes_of(caught.value)

    def test_an_approved_but_never_activated_child_cannot_be_taken_into_effect(
        self,
        recipe: Recipe,
        blend_approved: RecipeVersion,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        Agreement is not effectiveness. The draft was allowed to name it; the
        activation is not, because the child is in force nowhere.
        """
        parent = _parent_with_child(
            recipe=recipe, child=blend_approved, unit=kilogram, item=rice, author=manager
        )
        carry_to_approved(
            parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        with pytest.raises(ValidationError) as caught:
            activate_recipe_version(version=parent, actor=approver, effective_from=JULY)
        assert "recipe_component_not_effective" in codes_of(caught.value)

    def test_every_gap_is_reported_at_once_not_one_refusal_at_a_time(
        self,
        recipe: Recipe,
        blend: Recipe,
        branch: Branch,
        second_branch: Branch,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        child_draft = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        child = carry_to_active(
            child_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JULY,
            branches=[branch],
        )
        parent = _parent_with_child(
            recipe=recipe, child=child, unit=kilogram, item=rice, author=manager
        )
        gaps = coverage_gaps(
            parent_version=parent,
            branches=[branch.pk, second_branch.pk],
            effective_from=MARCH,
            effective_to=None,
        )
        # One branch starts too late, the other is not covered at all.
        assert {gap.code for gap in gaps} == {
            "recipe_component_not_effective",
            "recipe_component_branch_mismatch",
        }
        assert {gap.branch_code for gap in gaps} == {branch.code, second_branch.code}

    def test_a_parent_with_no_components_needs_no_coverage(
        self, active_version: RecipeVersion, branch: Branch
    ) -> None:
        assert (
            coverage_gaps(
                parent_version=active_version,
                branches=[branch.pk],
                effective_from=JULY,
                effective_to=None,
            )
            == []
        )


class TestSupersessionAfterActivation:
    """
    A child may be superseded freely once parents reference it.

    This class replaces one that asserted the opposite. The old rule refused the
    supersession outright while any `ACTIVE` parent named the child, which made
    an open-ended parent pin its child permanently and made ordinary corrections
    impossible. Nothing about the exact reference needed that protection: it is
    a frozen foreign key, not a lookup.
    """

    def _live_pair(
        self,
        *,
        recipe: Recipe,
        blend: Recipe,
        unit: UnitOfMeasure,
        item: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
        parent_effective_to: datetime.date | None = None,
    ) -> tuple[RecipeVersion, RecipeVersion]:
        """An ACTIVE child and an ACTIVE parent that names it."""
        child = carry_to_active(
            build_complete_draft(recipe=blend, unit=unit, item=item, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JANUARY,
        )
        parent = carry_to_active(
            _parent_with_child(recipe=recipe, child=child, unit=unit, item=item, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JULY,
            effective_to=parent_effective_to,
        )
        return child, parent

    def _replace_child(
        self,
        *,
        blend: Recipe,
        old_child: RecipeVersion,
        unit: UnitOfMeasure,
        item: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
        effective_from: datetime.date,
    ) -> RecipeVersion:
        replacement = carry_to_approved(
            build_complete_draft(recipe=blend, unit=unit, item=item, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(
            version=replacement,
            actor=approver,
            effective_from=effective_from,
            supersedes=RecipeVersion.objects.get(pk=old_child.pk),
        )
        return RecipeVersion.objects.get(pk=replacement.pk)

    def test_dependents_are_listed_per_branch(
        self,
        recipe: Recipe,
        blend: Recipe,
        branch: Branch,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        child, parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        dependencies = dependents_of(child)
        assert [dependency.parent_version.pk for dependency in dependencies] == [parent.pk]
        assert dependencies[0].branch_code == branch.code

    def test_an_open_ended_parent_does_not_block_child_supersession(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """The case the removed rule made impossible. It now simply works."""
        child, parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        assert RecipeVersion.objects.get(pk=parent.pk).effective_to is None

        replacement = self._replace_child(
            blend=blend,
            old_child=child,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=SEPTEMBER,
        )
        assert RecipeVersion.objects.get(pk=child.pk).status == RecipeVersionStatus.SUPERSEDED
        assert replacement.status == RecipeVersionStatus.ACTIVE

    def test_the_parent_still_references_the_exact_old_child(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        child, parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        self._replace_child(
            blend=blend,
            old_child=child,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=SEPTEMBER,
        )
        assert parent.components.get().component_version_id == child.pk

    def test_the_parents_component_tree_is_unchanged_by_the_supersession(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """Identical node for node, not merely "still has one row"."""
        child, parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )

        def shape() -> list[tuple[int, int, int, Decimal, Decimal]]:
            return [
                (
                    node.depth,
                    node.version.pk,
                    node.line_order,
                    node.multiplier,
                    node.cumulative_multiplier,
                )
                for node in flatten_tree(component_tree(RecipeVersion.objects.get(pk=parent.pk)))
            ]

        before = shape()
        self._replace_child(
            blend=blend,
            old_child=child,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=SEPTEMBER,
        )
        assert shape() == before

    def test_the_parent_remains_active_and_resolvable(
        self,
        recipe: Recipe,
        blend: Recipe,
        branch: Branch,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """Operationally valid, not merely present: the resolver still finds it."""
        child, parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        self._replace_child(
            blend=blend,
            old_child=child,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=SEPTEMBER,
        )
        refreshed = RecipeVersion.objects.get(pk=parent.pk)
        assert refreshed.status == RecipeVersionStatus.ACTIVE
        resolved = resolve_recipe_version(
            recipe=recipe, branch=branch, on_date=datetime.date(2026, 10, 1)
        )
        assert resolved.pk == parent.pk

    def test_the_supersession_writes_nothing_to_any_component(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """No re-pointing, and no touch at all: `updated_at` does not move."""
        child, parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        before = list(
            RecipeComponent.objects.order_by("pk").values(
                "pk", "component_version_id", "multiplier", "line_order", "updated_at"
            )
        )
        self._replace_child(
            blend=blend,
            old_child=child,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=SEPTEMBER,
        )
        after = list(
            RecipeComponent.objects.order_by("pk").values(
                "pk", "component_version_id", "multiplier", "line_order", "updated_at"
            )
        )
        assert after == before

    def test_a_new_parent_version_explicitly_adopts_the_new_child(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        child, parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        replacement = self._replace_child(
            blend=blend,
            old_child=child,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=SEPTEMBER,
        )
        new_parent = _parent_with_child(
            recipe=recipe, child=replacement, unit=kilogram, item=rice, author=manager
        )
        carry_to_approved(
            new_parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        # The old parent is still open-ended, so the replacement supersedes it -
        # a parent correction, entirely separate from the child correction above.
        activated = activate_recipe_version(
            version=new_parent,
            actor=approver,
            effective_from=datetime.date(2026, 10, 1),
            supersedes=RecipeVersion.objects.get(pk=parent.pk),
        )
        assert activated.components.get().component_version_id == replacement.pk
        # And the historical one is untouched.
        assert parent.components.get().component_version_id == child.pk

    def test_the_comparison_reports_the_child_version_change(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        from apps.kitchen.comparison import CHANGED, compare_recipe_versions

        child, parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        replacement = self._replace_child(
            blend=blend,
            old_child=child,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=SEPTEMBER,
        )
        new_parent = _parent_with_child(
            recipe=recipe, child=replacement, unit=kilogram, item=rice, author=manager
        )
        comparison = compare_recipe_versions(
            left=RecipeVersion.objects.get(pk=parent.pk), right=new_parent
        )
        section = next(part for part in comparison.sections if part.key == "components")
        row = next(entry for entry in section.rows if entry.key == blend.code)
        assert row.classification == CHANGED

    def test_the_superseded_child_cannot_be_deleted(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """`PROTECT` still holds. Superseding is not deleting."""
        from django.db.models import ProtectedError

        child, _parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        self._replace_child(
            blend=blend,
            old_child=child,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=SEPTEMBER,
        )
        with pytest.raises((ProtectedError, Exception)), transaction.atomic():
            RecipeVersion.objects.filter(pk=child.pk).delete()

    def test_the_superseded_childs_structure_is_still_frozen(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """Its lines cannot be edited, so a parent's expansion stays deterministic."""
        from apps.kitchen.models import RecipeLine

        child, _parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        self._replace_child(
            blend=blend,
            old_child=child,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=SEPTEMBER,
        )
        line = RecipeLine.objects.filter(version=child).first()
        assert line is not None
        with pytest.raises(Exception) as caught, transaction.atomic():
            RecipeLine.objects.filter(pk=line.pk).update(base_quantity=Decimal("99"))
        assert "frozen" in str(caught.value)

    def test_the_verifier_reports_it_as_an_advisory_and_stays_clean(
        self,
        organization: Organization,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        An advisory, not a finding. The exit code must not move for a state that
        is entirely correct.
        """
        from apps.kitchen.reconciliation import component_advisories, verify_organization

        child, parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        replacement = self._replace_child(
            blend=blend,
            old_child=child,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=SEPTEMBER,
        )

        assert verify_organization(organization) == []

        advisories = component_advisories(organization)
        assert [advisory.code for advisory in advisories] == ["active_parent_uses_superseded_child"]
        note = advisories[0]
        assert note.recipe_code == recipe.code
        assert note.version == f"v{parent.version_number}"
        assert blend.code in note.message
        assert f"v{child.version_number}" in note.message
        assert f"v{replacement.version_number}" in note.message

    def test_no_supersession_of_a_referenced_child_touches_stock_or_the_ledger(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        from apps.accounting.models import JournalEntry, JournalLine
        from apps.inventory.models import StockBalance, StockMovement

        child, _parent = self._live_pair(
            recipe=recipe,
            blend=blend,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        before = (
            StockMovement.objects.count(),
            StockBalance.objects.count(),
            JournalEntry.objects.count(),
            JournalLine.objects.count(),
        )
        self._replace_child(
            blend=blend,
            old_child=child,
            unit=kilogram,
            item=rice,
            manager=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=SEPTEMBER,
        )
        assert (
            StockMovement.objects.count(),
            StockBalance.objects.count(),
            JournalEntry.objects.count(),
            JournalLine.objects.count(),
        ) == before

    def test_no_service_re_points_a_component(self) -> None:
        """
        The lifecycle never assigns `component_version`, and the removed refusal
        is gone from the source rather than merely unreachable.

        Assignments are what re-pointing would look like, so that is what is
        searched for — prose mentioning the field is exactly what this module's
        comments are *supposed* to contain.
        """
        import inspect

        from apps.kitchen import lifecycle

        source = inspect.getsource(lifecycle)
        assert "component_version =" not in source
        assert "component_version_id =" not in source
        assert "recipe_component_dependency_blocks_supersession" not in source
        assert "supersession_blockers" not in source


class TestNoCascade:
    """Neither repointing nor cascade-superseding ever happens."""

    def test_a_superseded_child_leaves_its_parents_active(
        self,
        recipe: Recipe,
        blend: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        A child closed *within* its dependents' coverage is permitted, and the
        parents keep running on the exact version they named. Cascading would
        end recipes nobody decided to end.
        """
        child = carry_to_active(
            build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JANUARY,
        )
        parent = carry_to_active(
            _parent_with_child(
                recipe=recipe, child=child, unit=kilogram, item=rice, author=manager
            ),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=JULY,
            effective_to=datetime.date(2026, 8, 31),
        )
        replacement = carry_to_approved(
            build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(
            version=replacement,
            actor=approver,
            effective_from=datetime.date(2026, 9, 1),
            supersedes=RecipeVersion.objects.get(pk=child.pk),
        )
        assert RecipeVersion.objects.get(pk=child.pk).status == RecipeVersionStatus.SUPERSEDED
        refreshed_parent = RecipeVersion.objects.get(pk=parent.pk)
        assert refreshed_parent.status == RecipeVersionStatus.ACTIVE
        assert refreshed_parent.components.get().component_version_id == child.pk


class TestForeignScope:
    """No branch or organization ever leaks in through a component."""

    def test_a_foreign_branch_is_never_inferred_from_a_child(
        self,
        organization: Organization,
        other_organization: Organization,
        recipe: Recipe,
        blend_active: RecipeVersion,
        other_branch: Branch,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        parent = _parent_with_child(
            recipe=recipe, child=blend_active, unit=kilogram, item=rice, author=manager
        )
        carry_to_approved(
            parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        with pytest.raises(ValidationError) as caught:
            activate_recipe_version(
                version=parent,
                actor=approver,
                effective_from=JULY,
                branches=[other_branch],
            )
        assert "recipe_version_foreign_branch" in codes_of(caught.value)

    def test_a_foreign_child_recipe_cannot_be_named(
        self,
        complete_draft: RecipeVersion,
        other_organization: Organization,
        rival_manager: User,
        kilogram: UnitOfMeasure,
        rival_item: InventoryItem,
    ) -> None:
        foreign_recipe = make_child_recipe(
            organization=other_organization, code="THEIR-SPICE", author=rival_manager
        )
        foreign = build_complete_draft(
            recipe=foreign_recipe, unit=kilogram, item=rival_item, author=rival_manager
        )
        with pytest.raises(ValidationError) as caught:
            create_recipe_component(
                version=complete_draft, component_version=foreign, multiplier=Decimal("1")
            )
        assert "recipe_component_foreign_organization" in codes_of(caught.value)
