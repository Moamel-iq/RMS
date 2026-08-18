"""
The four contracts Task 3.4 rests on, tested rather than reasoned about.

Each of these is a claim made somewhere in a docstring, and each is the kind of
claim that stays true right up until it silently does not:

* **One expansion.** Costing and production read the same engine. A second walk
  would not fail — it would agree, for a while.
* **The warehouse mirror.** Kitchen's bulk warehouse filter has to answer
  exactly what the certified single-warehouse resolver answers, for every role,
  membership shape and scope mode. Written locally because Task 3.4 may not
  modify `apps/organizations`, which makes an equivalence test the only thing
  standing between "mirrors" and "resembles".
* **Idempotency across a version race.** A retry must return the original batch
  even when a replacement version activated in between — because the resolved
  version is a consequence of the request, not part of it.
* **The frozen decision.** Not merely refused by a service, but refused by the
  database, because a bulk update, raw SQL, the admin and a psql prompt all
  reach these tables.
"""

from __future__ import annotations

import datetime
import itertools
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.inventory.models import InventoryItem, Warehouse, WarehouseType
from apps.kitchen.expansion import expand_recipe_version
from apps.kitchen.graph import component_tree, flatten_tree
from apps.kitchen.models import (
    ProductionBatch,
    ProductionBatchActualLine,
    ProductionBatchLine,
    Recipe,
    RecipeVersion,
)
from apps.kitchen.permissions import CREATE_PRODUCTION_BATCH, VIEW_PRODUCTION
from apps.kitchen.production import create_production_batch
from apps.kitchen.selectors import (
    _warehouses_with_permission,
    draftable_production_warehouses,
    readable_production_warehouses,
)
from apps.kitchen.services import create_recipe_component
from apps.organizations.authorization import has_warehouse_permission
from apps.organizations.models import Branch, Organization, Role, WarehouseScopeMode
from apps.organizations.services import (
    grant_branch_access,
    grant_organization_access,
    set_membership_warehouse_scope,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import (
    build_complete_draft,
    carry_to_active,
    carry_to_approved,
    codes_of,
    make_child_recipe,
)

pytestmark = pytest.mark.django_db

#: When a *replacement* version takes effect, well after the `batch_recipe`
#: fixture's own `PRODUCTION_EFFECTIVE_FROM`, so the version-race tests arrange a
#: supersession rather than beginning with one.
SECOND = datetime.date(2026, 6, 1)


# ---------------------------------------------------------------------------
# §1.A — one expansion, and the display tree agrees with it
# ---------------------------------------------------------------------------


class TestOneSharedExpansion:
    def test_costing_and_production_import_the_same_engine(self) -> None:
        """
        Neither module may own a recursive `RecipeComponent` walk of its own.

        Read from the source rather than by calling both and comparing results:
        two copies *would* agree on the day they were written, and this is the
        test that notices the day one of them is fixed alone.
        """
        import ast
        import pathlib

        for name in ("costing.py", "production.py"):
            source = pathlib.Path("apps/kitchen") / name
            tree = ast.parse(source.read_text(encoding="utf-8"))
            recursion = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and any(
                    isinstance(inner, ast.Name) and inner.id == node.name
                    for inner in ast.walk(node)
                )
            ]
            assert recursion == [], f"{name} defines a recursive walk of its own"
            assert "RecipeComponent.objects" not in source.read_text(encoding="utf-8"), name
            assert "expand_recipe_version" in source.read_text(encoding="utf-8"), name

    def test_the_display_tree_agrees_with_the_engine_on_every_multiplier(
        self,
        organization: Organization,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        `graph.component_tree` still walks the graph — for the **screen**.

        It computes `cumulative_multiplier` too, which makes it the one other
        place in the codebase that multiplies component factors together. It is
        not folded into the engine because the screen needs the nesting shape
        the engine deliberately flattens away. So the two are pinned against
        each other here instead: same version, same products, in the same order.
        """
        people = {
            "author": manager,
            "cook": cook,
            "keeper": keeper,
            "accountant": accountant,
            "approver": approver,
        }
        leaf_recipe = make_child_recipe(
            organization=organization, code="AGREE-SPICE", author=manager
        )
        leaf = carry_to_active(
            build_complete_draft(recipe=leaf_recipe, unit=kilogram, item=rice, author=manager),
            submitter=people["author"],
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        middle_recipe = make_child_recipe(
            organization=organization, code="AGREE-BLEND", author=manager
        )
        middle_draft = build_complete_draft(
            recipe=middle_recipe, unit=kilogram, item=rice, author=manager
        )
        create_recipe_component(
            version=middle_draft, component_version=leaf, multiplier=Decimal("0.25")
        )
        middle = carry_to_active(
            middle_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        parent_recipe = make_child_recipe(
            organization=organization, code="AGREE-DISH", author=manager
        )
        parent_draft = build_complete_draft(
            recipe=parent_recipe, unit=kilogram, item=rice, author=manager
        )
        create_recipe_component(
            version=parent_draft, component_version=middle, multiplier=Decimal("0.5")
        )
        parent = carry_to_active(
            parent_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )

        # The engine's view: one entry per leaf line, with its path product.
        engine = {
            leaf_row.path: leaf_row.cumulative_multiplier
            for leaf_row in expand_recipe_version(parent)
        }
        # The screen's view: one node per component edge, with the same product.
        display = {
            tuple(int(step) for step in _path_of(node)): node.cumulative_multiplier
            for node in flatten_tree(component_tree(parent))
        }
        for path, product in display.items():
            assert engine[path] == product, path
        assert set(display) <= set(engine)


def _path_of(node: Any) -> list[str]:
    """
    The display node's path, rebuilt from its own depth and line order.

    `TreeNode` records `line_order` and `depth` rather than a full path, so the
    comparison above reconstructs it the only way the screen itself could.
    """
    return [str(node.line_order)] if node.depth == 1 else _walk_path(node)


def _walk_path(node: Any) -> list[str]:
    # `flatten_tree` yields parents before children, so a deeper node's path is
    # its parent's plus its own order. The demo graph here is a single chain, so
    # depth alone identifies it.
    return ["1"] * (node.depth - 1) + [str(node.line_order)]


# ---------------------------------------------------------------------------
# §1.E — the warehouse mirror
# ---------------------------------------------------------------------------


class TestTheWarehouseAuthorizationMirror:
    """
    `_warehouses_with_permission` must answer exactly what
    `has_warehouse_permission` answers, for every combination.

    This is the test that makes "mirrors" a fact. Kitchen wrote its own bulk
    filter because `apps.organizations` offers only the single-object question
    and Task 3.4 may not modify that module; the cost of that decision is
    exactly this equivalence, so it is paid here in full rather than asserted in
    a docstring.
    """

    @pytest.fixture
    def two_warehouses(self, branch: Branch) -> tuple[Warehouse, Warehouse]:
        first = Warehouse.objects.create(
            branch=branch,
            code="MIRROR-1",
            name_ar="مخزن أول",
            warehouse_type=WarehouseType.PHYSICAL,
        )
        second = Warehouse.objects.create(
            branch=branch,
            code="MIRROR-2",
            name_ar="مخزن ثانٍ",
            warehouse_type=WarehouseType.PHYSICAL,
        )
        return first, second

    @pytest.mark.parametrize("role", [role.value for role in Role])
    @pytest.mark.parametrize("permission", [VIEW_PRODUCTION, CREATE_PRODUCTION_BATCH])
    def test_a_branch_membership_in_all_mode_mirrors(
        self,
        two_warehouses: tuple[Warehouse, Warehouse],
        branch: Branch,
        role: str,
        permission: str,
    ) -> None:
        user = User.objects.create_user(username=f"all-{role}-{permission[-6:]}")
        grant_branch_access(user=user, branch=branch, role=role)
        user = User.objects.get(pk=user.pk)
        for warehouse in two_warehouses:
            assert _warehouses_with_permission(user, permission).filter(
                pk=warehouse.pk
            ).exists() is has_warehouse_permission(user, permission, warehouse), (
                role,
                permission,
                warehouse.code,
            )

    @pytest.mark.parametrize("role", [role.value for role in Role])
    def test_a_selected_membership_mirrors_warehouse_by_warehouse(
        self,
        two_warehouses: tuple[Warehouse, Warehouse],
        branch: Branch,
        role: str,
    ) -> None:
        """
        The case a bulk filter gets wrong: narrowing custody must narrow
        authority with it, so the *listed* warehouse mirrors and the unlisted
        one mirrors too — as a refusal.
        """
        listed, unlisted = two_warehouses
        user = User.objects.create_user(username=f"selected-{role}")
        membership = grant_branch_access(user=user, branch=branch, role=role)
        set_membership_warehouse_scope(
            membership=membership,
            mode=WarehouseScopeMode.SELECTED,
            warehouses=[listed],
        )
        user = User.objects.get(pk=user.pk)
        for permission, warehouse in itertools.product(
            (VIEW_PRODUCTION, CREATE_PRODUCTION_BATCH), (listed, unlisted)
        ):
            bulk = _warehouses_with_permission(user, permission).filter(pk=warehouse.pk).exists()
            single = has_warehouse_permission(user, permission, warehouse)
            assert bulk is single, (role, permission, warehouse.code)

    @pytest.mark.parametrize("role", [role.value for role in Role])
    def test_organization_authority_mirrors(
        self,
        two_warehouses: tuple[Warehouse, Warehouse],
        organization: Organization,
        role: str,
    ) -> None:
        """Organization-wide authority reaches every warehouse it owns, or none."""
        user = User.objects.create_user(username=f"org-{role}")
        grant_organization_access(user=user, organization=organization, role=role)
        user = User.objects.get(pk=user.pk)
        for permission, warehouse in itertools.product(
            (VIEW_PRODUCTION, CREATE_PRODUCTION_BATCH), two_warehouses
        ):
            bulk = _warehouses_with_permission(user, permission).filter(pk=warehouse.pk).exists()
            assert bulk is has_warehouse_permission(user, permission, warehouse), (
                role,
                permission,
            )

    def test_no_membership_mirrors_as_nothing(
        self, two_warehouses: tuple[Warehouse, Warehouse]
    ) -> None:
        user = User.objects.create_user(username="nobody-at-all")
        for warehouse in two_warehouses:
            assert not has_warehouse_permission(user, VIEW_PRODUCTION, warehouse)
            assert not readable_production_warehouses(user).filter(pk=warehouse.pk).exists()
            assert not draftable_production_warehouses(user).filter(pk=warehouse.pk).exists()

    def test_a_superuser_mirrors_as_everything(
        self, two_warehouses: tuple[Warehouse, Warehouse]
    ) -> None:
        """Made explicit so it is testable rather than incidental."""
        root = User.objects.create_superuser(username="root-mirror", password="x")
        for warehouse in two_warehouses:
            assert has_warehouse_permission(root, VIEW_PRODUCTION, warehouse)
            assert readable_production_warehouses(root).filter(pk=warehouse.pk).exists()


# ---------------------------------------------------------------------------
# §1.D — idempotency, and the version race
# ---------------------------------------------------------------------------


class TestIdempotencyAndVersionRaces:
    def test_the_same_key_and_request_returns_the_original(
        self,
        batch_recipe: tuple[Recipe, RecipeVersion],
        branch: Branch,
        store: Warehouse,
        manager: User,
    ) -> None:
        recipe, _version = batch_recipe
        first = create_production_batch(
            recipe=recipe,
            branch=branch,
            warehouse=store,
            planned_business_date=datetime.date(2026, 3, 1),
            multiplier=Decimal("2"),
            actor=manager,
            idempotency_key="IDEM-1",
        )
        again = create_production_batch(
            recipe=recipe,
            branch=branch,
            warehouse=store,
            planned_business_date=datetime.date(2026, 3, 1),
            multiplier=Decimal("2"),
            actor=manager,
            idempotency_key="IDEM-1",
        )
        assert again.pk == first.pk
        assert ProductionBatch.objects.count() == 1
        assert ProductionBatchLine.objects.filter(batch=first).count() == 1
        assert ProductionBatchActualLine.objects.filter(line__batch=first).count() == 1

    def test_a_retry_does_not_repoint_after_a_replacement_activates(
        self,
        batch_recipe: tuple[Recipe, RecipeVersion],
        branch: Branch,
        store: Warehouse,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        The case the fingerprint deliberately excludes the version for.

        A retry after an activation must return the **original** batch, still
        naming the version that was in force when it was drafted. Including the
        version in the fingerprint would have made this a conflict instead,
        which is the opposite of what idempotency is for.
        """
        from apps.kitchen.lifecycle import activate_recipe_version

        recipe, version = batch_recipe
        original = create_production_batch(
            recipe=recipe,
            branch=branch,
            warehouse=store,
            planned_business_date=datetime.date(2026, 3, 1),
            multiplier=Decimal("2"),
            actor=manager,
            idempotency_key="IDEM-RACE",
        )
        assert original.recipe_version_id == version.pk

        replacement = carry_to_approved(
            build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager),
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        activate_recipe_version(
            version=replacement,
            actor=approver,
            effective_from=SECOND,
            supersedes=RecipeVersion.objects.get(pk=version.pk),
        )

        again = create_production_batch(
            recipe=recipe,
            branch=branch,
            warehouse=store,
            planned_business_date=datetime.date(2026, 3, 1),
            multiplier=Decimal("2"),
            actor=manager,
            idempotency_key="IDEM-RACE",
        )
        assert again.pk == original.pk
        assert again.recipe_version_id == version.pk
        assert ProductionBatch.objects.count() == 1

    def test_the_same_key_with_a_changed_request_conflicts(
        self,
        batch_recipe: tuple[Recipe, RecipeVersion],
        branch: Branch,
        store: Warehouse,
        manager: User,
    ) -> None:
        recipe, _version = batch_recipe
        create_production_batch(
            recipe=recipe,
            branch=branch,
            warehouse=store,
            planned_business_date=datetime.date(2026, 3, 1),
            multiplier=Decimal("2"),
            actor=manager,
            idempotency_key="IDEM-CONFLICT",
        )
        with pytest.raises(ValidationError) as refusal:
            create_production_batch(
                recipe=recipe,
                branch=branch,
                warehouse=store,
                planned_business_date=datetime.date(2026, 3, 1),
                multiplier=Decimal("3"),
                actor=manager,
                idempotency_key="IDEM-CONFLICT",
            )
        assert "idempotency_key_conflict" in codes_of(refusal.value)
        assert ProductionBatch.objects.count() == 1

    def test_the_batch_version_is_permanent(
        self,
        batch_recipe: tuple[Recipe, RecipeVersion],
        branch: Branch,
        store: Warehouse,
        manager: User,
    ) -> None:
        """
        No service re-resolves it, and a test reads the source to say so.

        `resolve_recipe_version` appears exactly once in `production.py` outside
        the preview — inside `create_production_batch`. A second call anywhere
        else would be the silent re-pointing the whole design forbids.
        """
        import pathlib

        source = pathlib.Path("apps/kitchen/production.py").read_text(encoding="utf-8")
        # Exactly two call sites: `create_production_batch`, and the read-only
        # preview that has to agree with it. A third would be the silent
        # re-pointing the whole design forbids.
        assert source.count("resolve_recipe_version(") == 2, (
            "one create, one preview — no third resolution"
        )
        assert "def create_production_batch" in source
        assert "def preview_production_batch" in source


# ---------------------------------------------------------------------------
# §1.F — the frozen decision
# ---------------------------------------------------------------------------


@pytest.fixture
def drafted(
    batch_recipe: tuple[Recipe, RecipeVersion],
    branch: Branch,
    store: Warehouse,
    manager: User,
) -> ProductionBatch:
    recipe, _version = batch_recipe
    return create_production_batch(
        recipe=recipe,
        branch=branch,
        warehouse=store,
        planned_business_date=datetime.date(2026, 3, 1),
        multiplier=Decimal("2.5"),
        actor=manager,
        idempotency_key="FROZEN-1",
    )


class TestTheDecisionIsFrozen:
    """
    Raw SQL, deliberately. A service having no setter proves nothing about a
    psql prompt, and the psql prompt is what the trigger is for.
    """

    #: §1.F's frozen list, exactly. `multiplier` and `expected_output_quantity`
    #: are deliberately absent: how much of a recipe to make is a decision an
    #: operator may revise while the batch is a draft, and
    #: `rescale_production_batch` is the approved way to do it. What may not
    #: change is *which* recipe at *which* version — see the paired test below.
    @pytest.mark.parametrize(
        "column",
        [
            "organization_id",
            "branch_id",
            "warehouse_id",
            "recipe_id",
            "recipe_version_id",
            "planned_business_date",
        ],
    )
    def test_a_raw_update_of_the_decision_is_refused(
        self, drafted: ProductionBatch, column: str
    ) -> None:
        value = "'2027-01-01'" if column == "planned_business_date" else "999"
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                # The column name comes from this file's own parametrize list, not
                # from any request. Interpolated because a column cannot be bound.
                f"UPDATE kitchen_productionbatch SET {column} = {value} WHERE id = %s",  # noqa: S608
                [drafted.pk],
            )

    @pytest.mark.parametrize(
        "column",
        [
            "source_version_id",
            "source_line_id",
            "component_path",
            "item_id",
            "source_base_quantity",
            "cumulative_multiplier",
            "cost_class",
        ],
    )
    def test_a_raw_update_of_a_requirement_source_is_refused(
        self, drafted: ProductionBatch, column: str
    ) -> None:
        line = drafted.lines.first()
        assert line is not None
        value = "'999'" if column in {"component_path", "cost_class"} else "999"
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                # Same: a parametrized column name, never request input.
                f"UPDATE kitchen_productionbatchline SET {column} = {value} WHERE id = %s",  # noqa: S608
                [line.pk],
            )

    def test_a_raw_update_to_posted_is_refused(self, drafted: ProductionBatch) -> None:
        """The Task 3.4 / 3.5 boundary, as a property of the schema."""
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_productionbatch SET status = 'POSTED' WHERE id = %s",
                [drafted.pk],
            )

    def test_the_scale_is_not_part_of_the_frozen_decision(self, drafted: ProductionBatch) -> None:
        """
        The paired positive: a **coherent** raw rescale is permitted.

        Asserted rather than left implicit, because the 0011 allowlist is the only
        place that says the scale may move, and a future tidying that removed
        those two lines would break `rescale_production_batch` with no test to
        notice.

        Coherent, not merely raw: 0015 checks the header and every requirement
        against each other at COMMIT, so this updates both — which is exactly
        what makes it the positive case. The incomplete versions are refused, and
        `test_production_scale.py` is where each of them is proved refused.
        """
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE kitchen_productionbatch b SET multiplier = 3, "
                "expected_output_quantity = round(v.expected_output_quantity * 3, 6) "
                "FROM kitchen_recipeversion v "
                "WHERE v.id = b.recipe_version_id AND b.id = %s",
                [drafted.pk],
            )
            cursor.execute(
                "UPDATE kitchen_productionbatchline SET planned_base_quantity = "
                "round(source_base_quantity * cumulative_multiplier * 3, 6) "
                "WHERE batch_id = %s",
                [drafted.pk],
            )
        refreshed = ProductionBatch.objects.get(pk=drafted.pk)
        assert refreshed.multiplier == Decimal("3.000000")

    def test_the_planned_quantity_may_be_rescaled(self, drafted: ProductionBatch) -> None:
        """
        The one column on a requirement a rescale rewrites — from the frozen
        source basis, which is why the source figures themselves stay frozen.
        """
        from apps.kitchen.production import rescale_production_batch

        rescaled = rescale_production_batch(batch=drafted, multiplier=Decimal("4"))
        assert rescaled.multiplier == Decimal("4.000000")
        line = rescaled.lines.get()
        assert line.planned_base_quantity == (
            line.source_base_quantity * line.cumulative_multiplier * Decimal("4")
        )

    def test_the_actual_facts_remain_editable(
        self, drafted: ProductionBatch, manager: User
    ) -> None:
        from apps.kitchen.production import update_production_batch_actuals

        row = ProductionBatchActualLine.objects.get(line__batch=drafted)
        updated = update_production_batch_actuals(
            actual=row, entered_quantity=Decimal("1.5"), actor=manager
        )
        assert updated.base_quantity == Decimal("1.500000")
        assert updated.conversion_factor == Decimal("1.000000000000")
