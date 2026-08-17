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
    MANAGE_RECIPE,
    REJECT_RECIPE_VERSION,
    REVIEW_RECIPE_VERSION,
    SUBMIT_RECIPE_VERSION,
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

    def test_the_module_declares_no_posting_source_document_type(self) -> None:
        """
        A source document type is what a posting carries. Task 3.1 posts
        nothing, so it declares none — the constant arrives with the
        production batch in Task 3.5.
        """
        from apps.kitchen import models, services

        for module in (models, services):
            assert not [
                name
                for name in dir(module)
                if name.isupper() and "SOURCE" in name and "DOCUMENT" in name
            ]


class TestDependencyDirection:
    """
    Kitchen imports inventory. Nothing imports kitchen.

    Checked by parsing the imports rather than by convention, because a
    circular dependency announces itself only at start-up and only sometimes.
    """

    def _imports(self, package: str, *, include_tests: bool = True) -> set[str]:
        found: set[str] = set()
        for path in pathlib.Path(package.replace(".", "/")).rglob("*.py"):
            if "migrations" in path.parts:
                continue
            if not include_tests and "tests" in path.parts:
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
        }
        assert not (self._imports("apps/kitchen", include_tests=False) & forbidden)

    def test_kitchen_calls_no_accounting_kernel(self) -> None:
        """
        Same narrowing, same reason: the costing fixtures open a fiscal year so
        stock can be posted at all, and an open accounting period is a
        precondition of Inventory's own kernel rather than anything Kitchen
        knows about.
        """
        assert not {
            name
            for name in self._imports("apps/kitchen", include_tests=False)
            if name.startswith("apps.accounting") and not name.endswith(".models")
        }


class TestPermissionMap:
    def test_the_module_declares_exactly_eight_permissions(self) -> None:
        """
        Three from Task 3.1 and five from the lifecycle. Task 3.1 asserted
        three here for the same reason: a permission that arrives before the
        workflow it guards is a grant nobody can audit.
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
        }

    def test_no_production_or_report_permission_is_registered_early(self) -> None:
        """Task 3.4 – 3.9's permissions arrive with Task 3.4 – 3.9's workflows."""
        forbidden = ("production", "batch", "meal", "report", "import")
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
        from django.contrib import admin

        from apps.kitchen.admin import ReadOnlyAdmin
        from apps.kitchen.models import (
            RecipeCategory,
            RecipeLine,
            RecipeLineSubstitute,
            RecipeServing,
            RecipeStep,
            RecipeStepIngredient,
        )

        for model in (
            Recipe,
            RecipeCategory,
            RecipeVersion,
            RecipeLine,
            RecipeLineSubstitute,
            RecipeStep,
            RecipeStepIngredient,
            RecipeServing,
        ):
            assert isinstance(admin.site._registry[model], ReadOnlyAdmin)


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
