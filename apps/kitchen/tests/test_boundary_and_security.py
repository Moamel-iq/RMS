"""
The Task 3.1 boundary, and who is allowed through it.

Two proofs live here. The first is **zero effect**: recipes are intentions, so
every service in this module can run and the stock ledger and the general ledger
must be exactly as they were. That is asserted by counting rows before and
after rather than by reading the code, because the code is what would be wrong.

The second is **scope**. A recipe in another organization is a 404 and not a
403, because a 403 confirms the record is real and turns an id-guessing loop
into a census of somebody else's menu.
"""

from __future__ import annotations

import ast
import pathlib
from decimal import Decimal

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.accounting.models import JournalEntry
from apps.inventory.models import InventoryItem, StockBalance, StockLedgerEntry, StockMovement
from apps.kitchen.models import Recipe, RecipeType, RecipeVersion
from apps.kitchen.permissions import (
    ACTIVATE_RECIPE_VERSION,
    ALL_PERMISSIONS,
    APPROVE_RECIPE_VERSION,
    CREATE_PRODUCTION_BATCH,
    MANAGE_RECIPE,
    POST_PRODUCTION_BATCH,
    REJECT_RECIPE_VERSION,
    REVERSE_PRODUCTION_BATCH,
    REVIEW_RECIPE_VERSION,
    SUBMIT_RECIPE_VERSION,
    VIEW_PRODUCTION,
    VIEW_RECIPE,
    VIEW_RECIPE_COST,
    permissions_for_role,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_serving,
    add_recipe_step,
    archive_recipe,
    create_draft_recipe_version,
    create_recipe,
    delete_draft_recipe_version,
)
from apps.organizations.models import Organization, Role
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db


class TestZeroLedgerEffect:
    """
    Task 3.1 must create 0 stock movements and 0 journal entries.

    Stated as a requirement in the brief and proved here by counting, because
    "this code does not post" is exactly the claim that stops being true when
    somebody adds a convenience later.
    """

    def test_the_whole_lifecycle_moves_no_stock_and_writes_no_journal(
        self,
        organization: Organization,
        rice: InventoryItem,
        kilogram: UnitOfMeasure,
        manager: User,
    ) -> None:
        before = (
            StockMovement.objects.count(),
            StockLedgerEntry.objects.count(),
            StockBalance.objects.count(),
            JournalEntry.objects.count(),
        )

        recipe = create_recipe(
            organization=organization,
            code="ZERO",
            name_ar="بلا أثر",
            recipe_type=RecipeType.PORTION,
            created_by=manager,
        )
        draft = create_draft_recipe_version(
            recipe=recipe,
            expected_output_quantity=Decimal("10"),
            output_unit=kilogram,
            created_by=manager,
        )
        add_recipe_line(
            version=draft, item=rice, entered_quantity=Decimal("2"), entered_unit=kilogram
        )
        add_recipe_step(version=draft, instruction_ar="خطوة")
        add_recipe_serving(
            version=draft,
            code="ONE",
            name_ar="حصة",
            serving_quantity=Decimal("1"),
            serving_unit=kilogram,
            is_primary=True,
        )
        archive_recipe(recipe=recipe)
        delete_draft_recipe_version(version=draft)

        after = (
            StockMovement.objects.count(),
            StockLedgerEntry.objects.count(),
            StockBalance.objects.count(),
            JournalEntry.objects.count(),
        )
        assert before == after

    def test_only_the_posting_module_declares_a_source_document_type(self) -> None:
        """
        A source document type is what a posting carries, so exactly one module
        may declare one.

        Task 3.1 declared none and this test said so; **Task 3.5 declares one**,
        and the rewrite narrows the claim rather than dropping it. `models` and
        `services` still post nothing and still name nothing; the constant lives
        beside the command that writes it, and its value is checked here because
        a source identity that drifts by one character stops matching the
        journals and stock entries already carrying it.
        """
        from apps.kitchen import models, production_posting, services

        for module in (models, services):
            assert not [
                name
                for name in dir(module)
                if name.isupper() and "SOURCE" in name and "DOCUMENT" in name
            ]
        assert production_posting.SOURCE_DOCUMENT_TYPE == "KITCHEN_PRODUCTION_BATCH"


class TestDependencyDirection:
    """
    Kitchen imports inventory. Nothing imports kitchen.

    Checked by parsing the imports rather than by convention, because a
    circular dependency announces itself only at start-up and only sometimes.
    """

    def _imports(
        self, package: str, *, include_tests: bool = True, include_demo: bool = True
    ) -> set[str]:
        found: set[str] = set()
        for path in pathlib.Path(package.replace(".", "/")).rglob("*.py"):
            if "migrations" in path.parts:
                continue
            if not include_tests and "tests" in path.parts:
                continue
            if not include_demo and path.name == "demo.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found |= {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module)
        return found

    def test_inventory_does_not_import_kitchen(self) -> None:
        assert not {name for name in self._imports("apps/inventory") if "kitchen" in name}

    def test_accounting_does_not_import_kitchen(self) -> None:
        assert not {name for name in self._imports("apps/accounting") if "kitchen" in name}

    def test_procurement_does_not_import_kitchen(self) -> None:
        assert not {name for name in self._imports("apps/procurement") if "kitchen" in name}

    def test_kitchen_calls_no_inventory_posting_service(self) -> None:
        """
        Kitchen reads the item master, the conversions and — from Task 3.3 —
        the read-only valuation query. It must never reach a **posting** entry
        point, so those are checked by name.

        `include_tests=False`, and the exclusion is the point rather than a
        convenience. The rule is about what the module *does at runtime*; a
        costing fixture that posts stock through `apps.inventory.ledger` is
        exercising Inventory's own public API to build a world with a real
        moving average in it, which is exactly what the costing tests need and
        is not Kitchen reaching the ledger. `apps/inventory/valuation.py` is
        absent from the set below deliberately: it writes nothing, and a test
        asserts its source contains no write at all.

        `include_demo=False`, added by Task 3.5, is the same exclusion for the
        same reason. `demo.py` is DEBUG-only seeding, not domain logic: it
        builds an organization that has a chart of accounts and an item mapping
        so the posting screens have something on them, and it does so through
        `create_account` and `create_inventory_mapping` — the approved public
        services, used exactly as an operator would. A seeder constructing a
        world is not Kitchen reaching the accounting kernel to post, and the
        two would only be conflated by a rule that reads imports without
        reading what they are for.
        """
        forbidden = {
            "apps.inventory.ledger",
            "apps.inventory.operations",
            "apps.inventory.posting",
            "apps.inventory.commands",
            "apps.inventory.transfers",
            "apps.inventory.adjustments",
            "apps.inventory.counts",
            "apps.inventory.opening",
            "apps.inventory.locations",
        }
        assert not (
            self._imports("apps/kitchen", include_tests=False, include_demo=False) & forbidden
        )

    def test_kitchen_posts_stock_through_exactly_one_inventory_module(self) -> None:
        """
        Task 3.5 has to move stock, and it moves it through one door.

        `apps.inventory.production` is the narrow public interface the multi-
        module approval allowed: it takes quantities and a source identity, it
        knows nothing about recipes, and it is the **only** inventory module in
        the posting family that kitchen may import. The list above stays a list
        of refusals; this is the single, named exception, asserted so that a
        second door has to be added deliberately.
        """
        imported = self._imports("apps/kitchen", include_tests=False, include_demo=False)
        posting_family = {
            name
            for name in imported
            if name.startswith("apps.inventory.")
            # Read-only neighbours kitchen has used since Task 3.3, and the
            # demo seeder it composes with. None of them posts.
            and name.split(".")[-1]
            not in {
                "models",
                "valuation",
                "selectors",
                "reports",
                "demo",
                "seed_inventory_demo",
                "reason_codes",
                "permissions",
            }
        }
        assert posting_family == {"apps.inventory.production"}

    def test_kitchen_calls_no_accounting_kernel(self) -> None:
        """
        Same narrowing, same reason: the costing fixtures open a fiscal year so
        stock can be posted at all, and an open accounting period is a
        precondition of Inventory's own kernel rather than anything Kitchen
        knows about.
        """
        assert not {
            name
            for name in self._imports("apps/kitchen", include_tests=False, include_demo=False)
            if name.startswith("apps.accounting") and not name.endswith(".models")
        }


class TestPermissionMap:
    def test_the_module_declares_exactly_twelve_permissions(self) -> None:
        """
        Three from Task 3.1, five from the lifecycle, two Task 3.4 added for
        drafting, and two Task 3.5 added for posting. Task 3.1 asserted three
        here, 3.2A eight and 3.4 ten, for the reason this test keeps asserting
        a **closed** set: a permission that arrives before the workflow it
        guards is a grant nobody can audit.
        """
        assert set(ALL_PERMISSIONS) == {
            VIEW_RECIPE,
            MANAGE_RECIPE,
            VIEW_RECIPE_COST,
            SUBMIT_RECIPE_VERSION,
            REVIEW_RECIPE_VERSION,
            APPROVE_RECIPE_VERSION,
            REJECT_RECIPE_VERSION,
            ACTIVATE_RECIPE_VERSION,
            VIEW_PRODUCTION,
            CREATE_PRODUCTION_BATCH,
            POST_PRODUCTION_BATCH,
            REVERSE_PRODUCTION_BATCH,
        }

    def test_drafting_posting_and_reversing_are_three_separate_grants(self) -> None:
        """
        The control this separation exists for, checked on the role map.

        A storekeeper weighs the ingredients and commits the movement, and
        cannot undo it. Undoing a posted economic event is supervisory. If one
        post held all three, the person who made a wrong posting would be the
        only person able to make it disappear.
        """
        keeper = permissions_for_role(Role.STOREKEEPER)
        assert CREATE_PRODUCTION_BATCH in keeper
        assert POST_PRODUCTION_BATCH in keeper
        assert REVERSE_PRODUCTION_BATCH not in keeper

        assert REVERSE_PRODUCTION_BATCH in permissions_for_role(Role.MANAGER)
        for role in (Role.ACCOUNTANT, Role.ACCOUNTING_MANAGER, Role.VIEWER):
            held = permissions_for_role(role)
            assert VIEW_PRODUCTION in held
            assert not held & {POST_PRODUCTION_BATCH, REVERSE_PRODUCTION_BATCH}

    def test_no_meal_report_or_import_permission_is_registered_early(self) -> None:
        """Task 3.7 - 3.10's permissions arrive with Task 3.7 - 3.10's workflows."""
        forbidden = ("meal", "report", "import")
        assert not [name for name in ALL_PERMISSIONS if any(word in name for word in forbidden)]

    def test_the_four_lifecycle_authorities_are_separable(self) -> None:
        """
        Four different roles can between them hold the whole control, and no
        single non-owner role below manager holds two of the three that
        `KM-RCP-004` keeps apart.
        """
        assert REVIEW_RECIPE_VERSION in permissions_for_role(Role.STOREKEEPER)
        assert REVIEW_RECIPE_VERSION in permissions_for_role(Role.ACCOUNTANT)
        assert APPROVE_RECIPE_VERSION in permissions_for_role(Role.MANAGER)
        assert APPROVE_RECIPE_VERSION not in permissions_for_role(Role.ACCOUNTANT)
        assert APPROVE_RECIPE_VERSION not in permissions_for_role(Role.STOREKEEPER)
        assert ACTIVATE_RECIPE_VERSION not in permissions_for_role(Role.PURCHASING)

    def test_reviewing_never_confers_the_right_to_edit(self) -> None:
        """An accountant attests the evidence; they do not touch the quantities."""
        for role in (Role.ACCOUNTANT, Role.ACCOUNTING_MANAGER, Role.STOREKEEPER):
            held = permissions_for_role(role)
            assert REVIEW_RECIPE_VERSION in held
            assert MANAGE_RECIPE not in held

    def test_purchasing_reads_a_recipe_and_decides_nothing(self) -> None:
        held = permissions_for_role(Role.PURCHASING)
        assert VIEW_RECIPE in held
        assert not held & {
            REVIEW_RECIPE_VERSION,
            APPROVE_RECIPE_VERSION,
            REJECT_RECIPE_VERSION,
            ACTIVATE_RECIPE_VERSION,
        }

    def test_a_storekeeper_reads_the_card_and_never_the_cost(self) -> None:
        held = permissions_for_role(Role.STOREKEEPER)
        assert VIEW_RECIPE in held
        assert VIEW_RECIPE_COST not in held
        assert MANAGE_RECIPE not in held

    def test_a_cashier_holds_nothing_here(self) -> None:
        assert permissions_for_role(Role.CASHIER) == frozenset()

    def test_an_unknown_role_holds_nothing(self) -> None:
        assert permissions_for_role("NOT-A-ROLE") == frozenset()


class TestScopeAndAuthorization:
    def test_a_foreign_recipe_is_a_404_not_a_403(
        self, rival_client: Client, recipe: Recipe
    ) -> None:
        response = rival_client.get(reverse("kitchen:recipe_detail", args=[recipe.pk]))
        assert response.status_code == 404

    def test_a_foreign_recipe_cannot_be_archived_by_a_direct_post(
        self, rival_client: Client, recipe: Recipe
    ) -> None:
        response = rival_client.post(reverse("kitchen:recipe_archive", args=[recipe.pk]))
        assert response.status_code == 404
        recipe.refresh_from_db()
        assert recipe.is_active is True

    def test_a_reader_without_manage_is_refused_the_write_route(
        self, keeper_client: Client, recipe: Recipe
    ) -> None:
        """
        In scope, without authority: 403. The storekeeper can see this recipe
        and may not reshape it.
        """
        response = keeper_client.post(reverse("kitchen:recipe_archive", args=[recipe.pk]))
        assert response.status_code == 403
        recipe.refresh_from_db()
        assert recipe.is_active is True

    def test_a_reader_may_open_the_list(self, keeper_client: Client, recipe: Recipe) -> None:
        response = keeper_client.get(reverse("kitchen:recipe_list"))
        assert response.status_code == 200

    def test_someone_holding_no_kitchen_permission_is_refused(self, cashier_client: Client) -> None:
        response = cashier_client.get(reverse("kitchen:recipe_list"))
        assert response.status_code == 403

    def test_an_anonymous_caller_is_sent_to_the_login(self) -> None:
        response = Client().get(reverse("kitchen:recipe_list"))
        assert response.status_code in {302, 403}

    def test_a_foreign_recipe_is_absent_from_the_list(
        self, rival_client: Client, recipe: Recipe
    ) -> None:
        response = rival_client.get(reverse("kitchen:recipe_list"))
        assert response.status_code == 200
        assert recipe.code.encode() not in response.content


class TestScreens:
    def test_the_recipe_list_renders_right_to_left_under_arabic(
        self, manager_client: Client, recipe: Recipe
    ) -> None:
        """
        Direction comes from the active language, so the assertion has to name
        one. Arabic is the source language of this system; English is the
        translation target, and it is legitimately left-to-right.
        """
        # Test settings default to English for deterministic assertions, and
        # `ExplicitLocaleMiddleware` deliberately ignores Accept-Language so a
        # browser header cannot flip the interface. Arabic is therefore chosen
        # here the way a user chooses it.
        manager_client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
        response = manager_client.get(reverse("kitchen:recipe_list"))
        assert response.status_code == 200
        body = response.content.decode()
        assert 'dir="rtl"' in body
        assert "الوصفات" in body
        assert recipe.code in body

    def test_the_detail_screen_renders(self, manager_client: Client, recipe: Recipe) -> None:
        response = manager_client.get(reverse("kitchen:recipe_detail", args=[recipe.pk]))
        assert response.status_code == 200
        assert recipe.name_ar in response.content.decode()

    def test_the_detail_screen_says_the_version_is_a_draft(
        self, manager_client: Client, draft: RecipeVersion
    ) -> None:
        """No approve button, and the screen says why rather than staying silent."""
        response = manager_client.get(reverse("kitchen:recipe_detail", args=[draft.recipe_id]))
        body = response.content.decode()
        assert "مسودة" in body
        assert "kitchen:version_approve" not in body

    def test_an_htmx_request_returns_the_fragment_without_the_shell(
        self, manager_client: Client, recipe: Recipe
    ) -> None:
        full = manager_client.get(reverse("kitchen:recipe_list"))
        fragment = manager_client.get(
            reverse("kitchen:recipe_list"), headers={"HX-Request": "true"}
        )
        assert fragment.status_code == 200
        assert b"<html" in full.content
        assert b"<html" not in fragment.content

    def test_the_full_page_carries_exactly_one_shell(
        self, manager_client: Client, recipe: Recipe
    ) -> None:
        response = manager_client.get(reverse("kitchen:recipe_list"))
        assert response.content.count(b"<html") == 1

    def test_list_filters_and_search_survive_together(
        self, manager_client: Client, recipe: Recipe
    ) -> None:
        response = manager_client.get(
            reverse("kitchen:recipe_list"), {"q": recipe.code, "recipe_type": RecipeType.PORTION}
        )
        assert response.status_code == 200
        assert recipe.code.encode() in response.content

    def test_a_filter_that_excludes_the_row_hides_it(
        self, manager_client: Client, recipe: Recipe
    ) -> None:
        response = manager_client.get(
            reverse("kitchen:recipe_list"), {"recipe_type": RecipeType.BATCH}
        )
        assert recipe.code.encode() not in response.content

    def test_an_htmx_fragment_enforces_the_same_authorization(self, cashier_client: Client) -> None:
        response = cashier_client.get(
            reverse("kitchen:recipe_list"), headers={"HX-Request": "true"}
        )
        assert response.status_code == 403

    def test_no_cost_column_exists_anywhere_yet(
        self, manager_client: Client, draft: RecipeVersion
    ) -> None:
        """Task 3.3 owns costing; Task 3.1 ships no money on any screen."""
        response = manager_client.get(reverse("kitchen:recipe_detail", args=[draft.recipe_id]))
        body = response.content.decode()
        for money in ("الكلفة", "السعر", "الهامش"):
            assert money not in body


class TestAdminIsReadOnly:
    def test_admin_refuses_add_change_and_delete_even_for_a_superuser(self) -> None:
        from django.contrib import admin

        model_admin = admin.site._registry[Recipe]
        assert model_admin.has_add_permission(None) is False  # type: ignore[arg-type]
        assert model_admin.has_change_permission(None) is False  # type: ignore[arg-type]
        assert model_admin.has_delete_permission(None) is False  # type: ignore[arg-type]

    def test_every_kitchen_model_is_registered_read_only(self) -> None:
        """
        **Every** kitchen model, enumerated from the app registry rather than
        from a list somebody has to remember to extend.

        The list form was the previous shape of this test, and it would have
        passed unchanged when Task 3.4 added three tables that nothing
        registered. Asking the registry is what makes "every" mean every.
        """
        from django.apps import apps
        from django.contrib import admin

        from apps.kitchen.admin import ReadOnlyAdmin

        for model in apps.get_app_config("kitchen").get_models():
            if model.__name__.startswith("Historical"):
                # `simple_history` registers its own admin; it is append-only
                # by construction and is not this module's to configure.
                continue
            assert model in admin.site._registry, f"{model.__name__} is not registered"
            assert isinstance(admin.site._registry[model], ReadOnlyAdmin), (
                f"{model.__name__} is registered writable"
            )


class TestNoImportsFromTheProprietarySources:
    def test_the_application_never_opens_a_pdf(self) -> None:
        """
        RCP-122: the books are evidence for an import, not a data source the
        application reads. Nothing here parses one at request time.
        """
        offenders: list[str] = []
        for path in pathlib.Path("apps/kitchen").rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8").lower()
            for marker in (".pdf", "pypdf", "pdfplumber", "fitz", "openpyxl", ".xlsx"):
                if marker in text:
                    offenders.append(f"{path}:{marker}")
        assert offenders == []

    def test_no_pdf_or_workbook_is_tracked_by_git(self) -> None:
        import shutil
        import subprocess

        git = shutil.which("git")
        assert git is not None, "git is needed to prove nothing proprietary is tracked"
        tracked = subprocess.run(  # noqa: S603 - resolved absolute path, fixed arguments
            [git, "ls-files", "*.pdf", "*.xlsx", "*.xls"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert tracked.stdout.strip() == ""
