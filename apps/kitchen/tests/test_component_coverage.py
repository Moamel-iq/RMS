"""
Effective branch and date coverage, and the dependency guard that keeps it true
afterwards.

Two rules, and they are the same rule at two moments.

**RCP-074, at activation.** A parent may not take effect over a range or a
branch its child does not cover. A parent effective in March whose blend expired
in February is a recipe that claims to contain something that did not exist —
and nothing downstream would notice until a costing gap appeared months later,
by which time nobody remembers what changed.

**§L, afterwards.** The parent named one *exact* child version and keeps naming
it forever, so closing that child's range under a live parent would create the
same gap from the other direction. Superseding a child something depends on is
refused, and the refusal names the dependent parent. The correction is
versioning, never repointing (RCP-081): approve a replacement child, create a
new **parent** version that adopts it, put that parent into effect, and only
then close the old child.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.inventory.models import InventoryItem
from apps.kitchen.graph import coverage_gaps, dependents_of, supersession_blockers
from apps.kitchen.lifecycle import activate_recipe_version
from apps.kitchen.models import Recipe, RecipeVersion, RecipeVersionStatus
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
    """RCP-074: the child must cover the whole of what the parent claims."""

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

    def test_a_child_that_ends_before_the_parent_is_refused(
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
            activate_recipe_version(
                version=parent,
                actor=approver,
                effective_from=JANUARY,
                effective_to=DECEMBER_END,
            )
        assert "recipe_component_not_effective" in codes_of(caught.value)

    def test_an_open_ended_parent_needs_an_open_ended_child(
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
        A child that ends at all leaves a tail the parent claims and nothing
        covers — and the tail has no last day to name, which is exactly why an
        open-ended range cannot be checked by comparing two dates.
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
        carry_to_approved(
            parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        with pytest.raises(ValidationError) as caught:
            activate_recipe_version(version=parent, actor=approver, effective_from=JANUARY)
        assert "recipe_component_not_effective" in codes_of(caught.value)

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


class TestTheDependencyGuard:
    """§L: an exact child link stays valid after the parent activates."""

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
        )
        return child, parent

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

    def test_superseding_a_depended_on_child_is_refused(
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
        replacement = carry_to_approved(
            build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        with pytest.raises(ValidationError) as caught:
            activate_recipe_version(
                version=replacement,
                actor=approver,
                effective_from=datetime.date(2026, 9, 1),
                supersedes=RecipeVersion.objects.get(pk=child.pk),
            )
        assert "recipe_component_dependency_blocks_supersession" in codes_of(caught.value)
        assert recipe.code in str(caught.value)

    def test_the_refusal_leaves_both_versions_untouched(
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
        replacement = carry_to_approved(
            build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        with pytest.raises(ValidationError):
            activate_recipe_version(
                version=replacement,
                actor=approver,
                effective_from=datetime.date(2026, 9, 1),
                supersedes=RecipeVersion.objects.get(pk=child.pk),
            )
        assert RecipeVersion.objects.get(pk=child.pk).status == RecipeVersionStatus.ACTIVE
        assert RecipeVersion.objects.get(pk=child.pk).effective_to is None
        assert RecipeVersion.objects.get(pk=replacement.pk).status == RecipeVersionStatus.APPROVED

    def test_an_open_ended_parent_pins_its_child_open_ended(
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
        A structural consequence, asserted rather than discovered later.

        A dish in force **indefinitely** that says it contains blend v1 pins
        blend v1 in force indefinitely: any close date leaves the dish claiming
        an ingredient that stopped existing. There is no ordering that escapes
        it, and that is the honest answer rather than a defect — to change the
        blend, the dish has to be given an end date, which is the next test.
        """
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
        for close_at in (
            datetime.date(2026, 8, 31),
            datetime.date(2027, 12, 31),
            datetime.date(2099, 1, 1),
        ):
            assert supersession_blockers(
                child_version=RecipeVersion.objects.get(pk=child.pk), close_at=close_at
            )

    def test_the_child_may_be_closed_once_no_active_parent_needs_it(
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
        RCP-081's correction order, end to end, with a **bounded** parent.

        The dish runs July–August and says so. Once its own range ends on 31
        August, closing the blend on the same day strands nothing: every day the
        dish claims is a day the blend covers. Then the replacement blend takes
        effect on 1 September, and a new dish version adopts it explicitly.

        Nothing is repointed and nothing cascades: the historical dish still
        names the historical blend afterwards, which is the assertion at the end.
        """
        august_end = datetime.date(2026, 8, 31)
        september = datetime.date(2026, 9, 1)

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
            effective_to=august_end,
        )

        # Nothing active needs the blend past 31 August any more.
        assert (
            supersession_blockers(
                child_version=RecipeVersion.objects.get(pk=child.pk), close_at=august_end
            )
            == []
        )

        new_child = carry_to_approved(
            build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(
            version=new_child,
            actor=approver,
            effective_from=september,
            supersedes=RecipeVersion.objects.get(pk=child.pk),
        )
        assert RecipeVersion.objects.get(pk=child.pk).status == RecipeVersionStatus.SUPERSEDED
        assert RecipeVersion.objects.get(pk=child.pk).effective_to == august_end

        # A new dish version adopts the new blend explicitly.
        new_parent = _parent_with_child(
            recipe=recipe,
            child=RecipeVersion.objects.get(pk=new_child.pk),
            unit=kilogram,
            item=rice,
            author=manager,
        )
        activated = carry_to_active(
            new_parent,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=september,
        )
        assert activated.components.get().component_version_id == new_child.pk
        # The historical dish still names the historical blend. Nothing moved.
        assert parent.components.get().component_version_id == child.pk

    def test_both_supersession_paths_share_one_guard(self) -> None:
        """
        `activate_recipe_version(..., supersedes=...)` and
        `supersede_recipe_version` both close a predecessor through
        `_supersede_locked`, so the dependency guard is written once and cannot
        be true of one path and false of the other.

        The standalone command's blocker branch is not separately reachable
        through legitimate state, and that is worth saying rather than faking:
        a blocker needs an open-ended child, and a replacement cannot be
        activated over an open-ended predecessor without superseding it — which
        is the activation path the tests above exercise.
        """
        import inspect

        from apps.kitchen import lifecycle

        for command in (lifecycle.activate_recipe_version, lifecycle.supersede_recipe_version):
            assert "_supersede_locked" in inspect.getsource(command)
        assert "supersession_blockers" in inspect.getsource(lifecycle._supersede_locked)


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
