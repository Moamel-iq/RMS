"""
The component surface: screens, HTMX fragments, the API, and who may reach them.

The security tests here matter more than the rendering ones. `can_manage`
decides whether a button is *drawn*; the control is the permission check that
runs on POST whether or not the button was ever drawn, and each of the four
mutations is exercised below by a hand-made request from somebody who never saw
the screen.

Two boundaries are asserted repeatedly and on purpose: a foreign parent or child
is **404** (a 403 would confirm the row exists, and ids are sequential), and
reviewing a recipe never becomes a way to edit one.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.inventory.models import InventoryItem
from apps.kitchen.models import Recipe, RecipeComponent, RecipeVersion
from apps.kitchen.services import create_recipe_component
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User

from .conftest import build_complete_draft, carry_to_active, make_child_recipe

pytestmark = pytest.mark.django_db

API = "/api/v1/kitchen"


def _arabic(client: Client) -> Client:
    """
    Ask for the Arabic rendering explicitly.

    The test settings force `LANGUAGE_CODE = "en"` and
    `ExplicitLocaleMiddleware` deliberately ignores `Accept-Language`, so the
    cookie is the only way to see what an operator actually sees.
    """
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
    return client


@pytest.fixture
def component(complete_draft: RecipeVersion, blend_active: RecipeVersion) -> RecipeComponent:
    return create_recipe_component(
        version=complete_draft, component_version=blend_active, multiplier=Decimal("0.25")
    )


class TestScreensRender:
    def test_the_component_editor_renders_in_arabic(
        self, manager_client: Client, component: RecipeComponent
    ) -> None:
        response = _arabic(manager_client).get(
            reverse("kitchen:component_editor", args=[component.version_id])
        )
        body = response.content.decode()

        assert response.status_code == 200
        assert "الوصفات الفرعية" in body
        assert component.component_recipe.code in body

    def test_the_editor_shows_the_factor_with_a_period(
        self, manager_client: Client, component: RecipeComponent
    ) -> None:
        """A conversion factor is a technical identity, even under Arabic."""
        response = _arabic(manager_client).get(
            reverse("kitchen:component_editor", args=[component.version_id])
        )
        assert "0.250000000000" in response.content.decode()

    def test_the_candidate_filter_answers_htmx_and_a_full_page(
        self, manager_client: Client, component: RecipeComponent, blend_active: RecipeVersion
    ) -> None:
        url = reverse("kitchen:component_editor", args=[component.version_id])
        full = manager_client.get(url, {"q": "BLEND"})
        fragment = manager_client.get(url, {"q": "BLEND"}, headers={"HX-Request": "true"})

        assert full.status_code == 200
        assert fragment.status_code == 200
        # The fragment carries the panel and not the shell around it.
        assert "component-candidates" in fragment.content.decode()
        assert "<html" not in fragment.content.decode().lower()

    def test_the_tree_renders_both_ways(
        self, manager_client: Client, component: RecipeComponent
    ) -> None:
        url = reverse("kitchen:component_tree", args=[component.version_id])
        full = manager_client.get(url)
        fragment = manager_client.get(url, headers={"HX-Request": "true"})

        assert full.status_code == 200
        assert fragment.status_code == 200
        assert "<html" in full.content.decode().lower()
        assert "<html" not in fragment.content.decode().lower()

    def test_the_tree_shows_the_exact_child_version(
        self, manager_client: Client, component: RecipeComponent
    ) -> None:
        response = manager_client.get(
            reverse("kitchen:component_tree", args=[component.version_id])
        )
        body = response.content.decode()
        assert component.component_recipe.code in body
        assert f"v{component.component_version.version_number}" in body

    def test_the_dependency_panel_renders_both_ways(
        self, manager_client: Client, component: RecipeComponent
    ) -> None:
        url = reverse("kitchen:component_dependencies", args=[component.component_version_id])
        full = manager_client.get(url)
        fragment = manager_client.get(url, headers={"HX-Request": "true"})

        assert full.status_code == 200
        assert fragment.status_code == 200
        assert component.version.recipe.code in full.content.decode()

    def test_the_version_detail_links_to_the_component_workspace(
        self, manager_client: Client, component: RecipeComponent
    ) -> None:
        response = manager_client.get(
            reverse("kitchen:version_detail", args=[component.version_id])
        )
        body = response.content.decode()
        assert reverse("kitchen:component_editor", args=[component.version_id]) in body
        assert component.component_recipe.code in body

    def test_no_component_screen_shows_a_cost(
        self, manager_client: Client, component: RecipeComponent
    ) -> None:
        """
        Task 3.3 built costing **screens**, and these three are not among them.

        The claim was "no screen in this module shows money" and that half is
        now false: `kitchen:cost_card` exists and shows exactly that, behind
        `view_recipe_cost`. What is still true — and what this test was
        rewritten to hold — is that the *component* workspace stayed money-free.
        A tree that quietly grew a cost column would be a cost surface arriving
        by the back door, ungated, on a screen a cook reads.

        `cost` and `price` as bare substrings are gone from the search: the
        rendered page now legitimately contains a link whose URL says `cost`.
        The Arabic words a reader would actually see are what matter.
        """
        for name, argument in (
            ("kitchen:component_editor", component.version_id),
            ("kitchen:component_tree", component.version_id),
            ("kitchen:component_dependencies", component.component_version_id),
        ):
            body = _arabic(manager_client).get(reverse(name, args=[argument])).content.decode()
            for word in ("الكلفة", "السعر", "كلفة الوحدة", "إجمالي كلفة"):
                assert word not in body, f"{name} exposed {word}"

    def test_the_editor_says_a_frozen_parent_cannot_be_edited(
        self,
        manager_client: Client,
        component: RecipeComponent,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        from .conftest import carry_to_approved

        carry_to_approved(
            component.version,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        body = (
            _arabic(manager_client)
            .get(reverse("kitchen:component_editor", args=[component.version_id]))
            .content.decode()
        )
        assert "مجمّدة" in body


class TestScreenSecurity:
    """A hidden button is presentation. Direct POST is still refused."""

    def test_a_foreign_parent_version_is_404(
        self,
        rival_client: Client,
        component: RecipeComponent,
    ) -> None:
        for name in ("kitchen:component_editor", "kitchen:component_tree"):
            response = rival_client.get(reverse(name, args=[component.version_id]))
            assert response.status_code == 404, name

    def test_a_foreign_component_is_404(
        self, rival_client: Client, component: RecipeComponent
    ) -> None:
        response = rival_client.post(reverse("kitchen:component_delete", args=[component.pk]))
        assert response.status_code == 404

    def test_a_reviewer_without_manage_gets_403_on_every_mutation(
        self, keeper_client: Client, component: RecipeComponent, blend_active: RecipeVersion
    ) -> None:
        """
        The storekeeper holds `review_recipe_version` and reaches the
        organization. Reviewing a recipe must never become a way to edit one.
        """
        assert (
            keeper_client.post(
                reverse("kitchen:component_create", args=[component.version_id]),
                {"component_version": blend_active.pk, "multiplier": "1"},
            ).status_code
            == 403
        )
        assert (
            keeper_client.post(
                reverse("kitchen:component_update", args=[component.pk]),
                {"component_version": blend_active.pk, "multiplier": "2"},
            ).status_code
            == 403
        )
        assert (
            keeper_client.post(
                reverse("kitchen:component_reorder", args=[component.pk]), {"line_order": 1}
            ).status_code
            == 403
        )
        assert (
            keeper_client.post(reverse("kitchen:component_delete", args=[component.pk])).status_code
            == 403
        )

    def test_a_cashier_reaches_nothing_here(
        self, cashier_client: Client, component: RecipeComponent
    ) -> None:
        assert (
            cashier_client.get(
                reverse("kitchen:component_editor", args=[component.version_id])
            ).status_code
            == 403
        )
        assert (
            cashier_client.get(
                reverse("kitchen:component_editor", args=[component.version_id]),
                headers={"HX-Request": "true"},
            ).status_code
            == 403
        )

    def test_a_keeper_may_still_read_the_tree(
        self, keeper_client: Client, component: RecipeComponent
    ) -> None:
        """`view_recipe` reads the card, including what it is built on."""
        response = keeper_client.get(reverse("kitchen:component_tree", args=[component.version_id]))
        assert response.status_code == 200

    def test_the_htmx_fragment_does_not_leak_a_foreign_candidate(
        self,
        manager_client: Client,
        complete_draft: RecipeVersion,
        other_organization: Organization,
        rival_manager: User,
        kilogram: UnitOfMeasure,
        rival_item: InventoryItem,
    ) -> None:
        foreign_recipe = make_child_recipe(
            organization=other_organization, code="THEIR-BLEND", author=rival_manager
        )
        build_complete_draft(
            recipe=foreign_recipe, unit=kilogram, item=rival_item, author=rival_manager
        )
        response = manager_client.get(
            reverse("kitchen:component_editor", args=[complete_draft.pk]),
            {"q": "THEIR"},
            headers={"HX-Request": "true"},
        )
        assert "THEIR-BLEND" not in response.content.decode()

    def test_csrf_is_enforced_on_a_mutation(
        self, manager: User, component: RecipeComponent
    ) -> None:
        client = Client(enforce_csrf_checks=True)
        client.force_login(manager)
        response = client.post(reverse("kitchen:component_delete", args=[component.pk]))
        assert response.status_code == 403


class TestTheApi:
    def test_the_component_crud_runs_through_the_api(
        self,
        manager_client: Client,
        complete_draft: RecipeVersion,
        blend_active: RecipeVersion,
    ) -> None:
        created = manager_client.post(
            f"{API}/recipe-versions/{complete_draft.pk}/components",
            data=json.dumps(
                {"component_version_id": blend_active.pk, "multiplier": "0.250000000000"}
            ),
            content_type="application/json",
        )
        assert created.status_code == 201
        body = created.json()
        # Decimals cross the boundary as exact strings. A JSON number is a
        # binary float before any Python code sees it.
        assert body["multiplier"] == "0.250000000000"
        assert isinstance(body["multiplier"], str)
        assert body["component_version_number"] == blend_active.version_number

        listed = manager_client.get(f"{API}/recipe-versions/{complete_draft.pk}/components")
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        patched = manager_client.patch(
            f"{API}/recipe-components/{body['id']}",
            data=json.dumps({"multiplier": "0.500000000000"}),
            content_type="application/json",
        )
        assert patched.status_code == 200
        assert patched.json()["multiplier"] == "0.500000000000"

        reordered = manager_client.post(
            f"{API}/recipe-components/{body['id']}/reorder",
            data=json.dumps({"line_order": 1}),
            content_type="application/json",
        )
        assert reordered.status_code == 200
        assert reordered.json()[0]["line_order"] == 1

        removed = manager_client.delete(f"{API}/recipe-components/{body['id']}")
        assert removed.status_code == 204

    def test_the_tree_endpoint_carries_no_cost(
        self, manager_client: Client, component: RecipeComponent
    ) -> None:
        response = manager_client.get(
            f"{API}/recipe-versions/{component.version_id}/component-tree"
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload
        for node in payload:
            assert not [key for key in node if "cost" in key or "price" in key]
            assert "," not in node["cumulative_multiplier"]

    def test_a_stocked_child_is_refused_with_its_stable_code(
        self,
        manager_client: Client,
        complete_draft: RecipeVersion,
        stocked_active: RecipeVersion,
    ) -> None:
        """Domain refusals surface as 422 with the code intact."""
        response = manager_client.post(
            f"{API}/recipe-versions/{complete_draft.pk}/components",
            data=json.dumps({"component_version_id": stocked_active.pk, "multiplier": "1"}),
            content_type="application/json",
        )
        assert response.status_code == 422
        assert "recipe_component_child_is_stocked" in response.content.decode()

    def test_a_cycle_is_refused_with_its_stable_code(
        self, manager_client: Client, complete_draft: RecipeVersion
    ) -> None:
        response = manager_client.post(
            f"{API}/recipe-versions/{complete_draft.pk}/components",
            data=json.dumps({"component_version_id": complete_draft.pk, "multiplier": "1"}),
            content_type="application/json",
        )
        assert response.status_code == 422
        assert "recipe_component_cycle" in response.content.decode()

    def test_a_frozen_parent_refuses_a_component_through_the_api(
        self,
        manager_client: Client,
        component: RecipeComponent,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
        blend_active: RecipeVersion,
    ) -> None:
        from .conftest import carry_to_approved

        carry_to_approved(
            component.version,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        response = manager_client.patch(
            f"{API}/recipe-components/{component.pk}",
            data=json.dumps({"multiplier": "9"}),
            content_type="application/json",
        )
        assert response.status_code == 422

    def test_a_foreign_child_id_cannot_widen_scope(
        self,
        manager_client: Client,
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
        response = manager_client.post(
            f"{API}/recipe-versions/{complete_draft.pk}/components",
            data=json.dumps({"component_version_id": foreign.pk, "multiplier": "1"}),
            content_type="application/json",
        )
        # Resolved through the caller, so it is invisible rather than forbidden.
        assert response.status_code == 404

    def test_a_foreign_parent_version_is_404_through_the_api(
        self, rival_client: Client, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        response = rival_client.post(
            f"{API}/recipe-versions/{complete_draft.pk}/components",
            data=json.dumps({"component_version_id": blend_active.pk, "multiplier": "1"}),
            content_type="application/json",
        )
        assert response.status_code == 404

    def test_a_reviewer_gets_403_from_the_api(
        self, keeper_client: Client, complete_draft: RecipeVersion, blend_active: RecipeVersion
    ) -> None:
        response = keeper_client.post(
            f"{API}/recipe-versions/{complete_draft.pk}/components",
            data=json.dumps({"component_version_id": blend_active.pk, "multiplier": "1"}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_the_posting_routes_arrived_and_the_report_ones_did_not(self) -> None:
        """
        **Task 3.3 brought the cost routes in and Task 3.4 the drafting ones**,
        so the original claim is now false twice over and this test has been
        rewritten each time rather than deleted — the fence moved, it did not
        come down.

        What is left of it is the line that still matters: a production batch
        may be drafted, scaled and edited over the wire, and it may not be
        posted, reversed, issued, consumed, completed or journalled. Those are
        Task 3.5's, and a route is the first place an unfinished boundary
        leaks.
        """
        from config.api import api

        paths = {
            f"{prefix}{operation.path}"
            for prefix, router in api._routers
            for path_view in router.path_operations.values()
            for operation in path_view.operations
        }
        kitchen = {path for path in paths if "kitchen" in path}
        assert kitchen
        assert any("cost" in path for path in kitchen), "Task 3.3 owns the cost routes"
        assert any("production-batches" in path for path in kitchen), "Task 3.4 owns drafting"
        assert any(path.endswith("/post") for path in kitchen), "Task 3.5 owns posting"
        assert any(path.endswith("/reverse") for path in kitchen)
        for path in kitchen:
            for forbidden in ("flatten", "meal", "consumption", "variance", "theoretical"):
                assert forbidden not in path.lower(), path


class TestVersionComparison:
    """The components section of the diff (§P)."""

    def test_adopting_a_newer_child_reads_as_changed(
        self,
        recipe: Recipe,
        blend: Recipe,
        blend_active: RecipeVersion,
        complete_draft: RecipeVersion,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        from apps.kitchen.comparison import CHANGED, compare_recipe_versions

        from .conftest import carry_to_approved

        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("0.5")
        )
        carry_to_active(
            complete_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=datetime.date(2026, 7, 1),
        )
        blend_v2 = build_complete_draft(recipe=blend, unit=kilogram, item=rice, author=manager)
        carry_to_approved(
            blend_v2,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
        )
        parent_v2 = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        create_recipe_component(
            version=parent_v2,
            component_version=RecipeVersion.objects.get(pk=blend_v2.pk),
            multiplier=Decimal("0.5"),
        )

        comparison = compare_recipe_versions(
            left=RecipeVersion.objects.get(pk=complete_draft.pk), right=parent_v2
        )
        section = next(part for part in comparison.sections if part.key == "components")
        row = next(entry for entry in section.rows if entry.key == blend.code)
        assert row.classification == CHANGED
        labels = {difference.label for difference in row.differences}
        assert any("نسخة" in label for label in labels)

    def test_a_child_superseded_elsewhere_leaves_the_parent_unchanged(
        self,
        recipe: Recipe,
        blend_active: RecipeVersion,
        complete_draft: RecipeVersion,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        """
        The parent's own `component_version` did not move, so its diff row must
        not claim it did. That distinction is the whole of RCP-072.
        """
        from apps.kitchen.comparison import UNCHANGED, compare_recipe_versions

        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("0.5")
        )
        carry_to_active(
            complete_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=datetime.date(2026, 7, 1),
        )
        parent_v2 = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        create_recipe_component(
            version=parent_v2, component_version=blend_active, multiplier=Decimal("0.5")
        )

        comparison = compare_recipe_versions(
            left=RecipeVersion.objects.get(pk=complete_draft.pk), right=parent_v2
        )
        section = next(part for part in comparison.sections if part.key == "components")
        row = next(entry for entry in section.rows if entry.key == blend_active.recipe.code)
        assert row.classification == UNCHANGED

    def test_the_comparison_screen_shows_the_components_section(
        self,
        manager_client: Client,
        recipe: Recipe,
        complete_draft: RecipeVersion,
        blend_active: RecipeVersion,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
    ) -> None:
        create_recipe_component(
            version=complete_draft, component_version=blend_active, multiplier=Decimal("0.5")
        )
        carry_to_active(
            complete_draft,
            submitter=manager,
            cook=cook,
            keeper=keeper,
            accountant=accountant,
            approver=approver,
            effective_from=datetime.date(2026, 7, 1),
        )
        parent_v2 = build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        response = _arabic(manager_client).get(
            reverse("kitchen:version_compare", args=[parent_v2.pk])
        )
        assert response.status_code == 200
        assert "الوصفات الفرعية" in response.content.decode()
