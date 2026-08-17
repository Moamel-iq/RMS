"""
The lifecycle's operator surface: the screens, the API, and who may reach them.

The security tests here matter more than the rendering ones. A hidden button is
presentation; the control is the permission check that runs on POST whether or
not the button was ever drawn, and each one is exercised by a hand-made request
from somebody who never saw the screen.
"""

from __future__ import annotations

import datetime
import json

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.inventory.models import InventoryItem
from apps.kitchen.models import (
    ApprovalEvidenceKind,
    Recipe,
    RecipeReviewDecision,
    RecipeReviewType,
    RecipeVersion,
    RecipeVersionStatus,
)
from apps.organizations.models import Branch
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

JULY = datetime.date(2026, 7, 1)
REFERENCE = "KM-RCP-004/2026/07"


def _arabic(client: Client) -> Client:
    """
    Ask for the Arabic rendering explicitly.

    The test settings force `LANGUAGE_CODE = "en"` and
    `ExplicitLocaleMiddleware` deliberately ignores `Accept-Language`, so the
    cookie is the only way to see what an operator actually sees.
    """
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
    return client


class TestScreensRender:
    def test_the_version_list_renders(
        self, manager_client: Client, active_version: RecipeVersion
    ) -> None:
        response = manager_client.get(reverse("kitchen:version_list"))

        assert response.status_code == 200
        assert active_version.recipe.code in response.content.decode()

    def test_the_version_list_filters_by_status(
        self, manager_client: Client, active_version: RecipeVersion
    ) -> None:
        matching = manager_client.get(
            reverse("kitchen:version_list"), {"status": RecipeVersionStatus.ACTIVE}
        )
        other = manager_client.get(
            reverse("kitchen:version_list"), {"status": RecipeVersionStatus.REJECTED}
        )

        assert active_version.recipe.code in matching.content.decode()
        assert active_version.recipe.code not in other.content.decode()

    def test_the_version_list_answers_htmx_with_the_fragment(
        self, manager_client: Client, active_version: RecipeVersion
    ) -> None:
        full = manager_client.get(reverse("kitchen:version_list"))
        fragment = manager_client.get(
            reverse("kitchen:version_list"), headers={"HX-Request": "true"}
        )

        assert fragment.status_code == 200
        assert len(fragment.content) < len(full.content)

    def test_the_version_detail_renders_in_arabic_rtl(
        self, manager_client: Client, active_version: RecipeVersion
    ) -> None:
        response = _arabic(manager_client).get(
            reverse("kitchen:version_detail", args=[active_version.pk])
        )
        body = response.content.decode()

        assert response.status_code == 200
        assert 'dir="rtl"' in body
        assert "سارية" in body

    def test_the_detail_screen_shows_the_signatures(
        self, manager_client: Client, active_version: RecipeVersion, cook: User
    ) -> None:
        response = _arabic(manager_client).get(
            reverse("kitchen:version_detail", args=[active_version.pk])
        )
        body = response.content.decode()

        assert str(cook) in body
        assert REFERENCE in body

    def test_the_timeline_fragment_and_the_full_page_carry_the_same_evidence(
        self, manager_client: Client, active_version: RecipeVersion
    ) -> None:
        url = reverse("kitchen:version_timeline", args=[active_version.pk])
        full = manager_client.get(url)
        fragment = manager_client.get(url, headers={"HX-Request": "true"})

        assert full.status_code == fragment.status_code == 200
        assert REFERENCE in fragment.content.decode()
        assert len(fragment.content) < len(full.content)

    def test_the_comparison_screen_renders(
        self,
        manager_client: Client,
        active_version: RecipeVersion,
        recipe: Recipe,
        kilogram: UnitOfMeasure,
        rice: InventoryItem,
        manager: User,
    ) -> None:
        from .conftest import build_complete_draft

        build_complete_draft(recipe=recipe, unit=kilogram, item=rice, author=manager)
        response = _arabic(manager_client).get(
            reverse("kitchen:version_compare", args=[active_version.pk])
        )

        assert response.status_code == 200
        assert "مقارنة" in response.content.decode()

    def test_the_resolver_preview_answers_a_dated_question(
        self, manager_client: Client, active_version: RecipeVersion, branch: Branch
    ) -> None:
        response = _arabic(manager_client).get(
            reverse("kitchen:version_resolve", args=[active_version.recipe_id]),
            {"branch": str(branch.pk), "on_date": JULY.isoformat()},
        )
        body = response.content.decode()

        assert response.status_code == 200
        assert f"v{active_version.version_number}" in body

    def test_the_resolver_preview_says_so_when_nothing_applies(
        self, manager_client: Client, approved_version: RecipeVersion, branch: Branch
    ) -> None:
        response = _arabic(manager_client).get(
            reverse("kitchen:version_resolve", args=[approved_version.recipe_id]),
            {"branch": str(branch.pk), "on_date": JULY.isoformat()},
        )

        assert response.status_code == 200
        assert "لا توجد نسخة سارية" in response.content.decode()

    def test_the_recipe_history_screen_renders(
        self, manager_client: Client, active_version: RecipeVersion
    ) -> None:
        response = _arabic(manager_client).get(
            reverse("kitchen:recipe_versions", args=[active_version.recipe_id])
        )

        assert response.status_code == 200
        assert "سجل النسخ" in response.content.decode()

    def test_no_screen_shows_a_cost_column(
        self, manager_client: Client, active_version: RecipeVersion
    ) -> None:
        """Task 3.3 owns costing; a blanked money column is worse than none."""
        for url in (
            reverse("kitchen:version_list"),
            reverse("kitchen:version_detail", args=[active_version.pk]),
            reverse("kitchen:recipe_versions", args=[active_version.recipe_id]),
        ):
            body = _arabic(manager_client).get(url).content.decode()
            for word in ("كلفة الوحدة", "إجمالي الكلفة", "سعر البيع", "الهامش"):
                assert word not in body


class TestButtonsAreNotAuthorization:
    """
    A hidden button is presentation. The refusal is the permission check that
    runs on POST regardless, and every one of the five is tested by somebody
    who never saw the screen.
    """

    def test_a_cashier_cannot_reach_the_version_list(
        self, cashier_client: Client, active_version: RecipeVersion
    ) -> None:
        assert cashier_client.get(reverse("kitchen:version_list")).status_code == 403

    def test_a_storekeeper_sees_no_approve_button(
        self, keeper_client: Client, complete_draft: RecipeVersion, manager: User
    ) -> None:
        from apps.kitchen.lifecycle import submit_recipe_version

        submit_recipe_version(version=complete_draft, actor=manager)
        body = (
            _arabic(keeper_client)
            .get(reverse("kitchen:version_detail", args=[complete_draft.pk]))
            .content.decode()
        )

        assert reverse("kitchen:version_approve", args=[complete_draft.pk]) not in body

    def test_a_storekeeper_posting_the_approval_anyway_is_refused(
        self, keeper_client: Client, complete_draft: RecipeVersion, manager: User
    ) -> None:
        from apps.kitchen.lifecycle import submit_recipe_version

        submit_recipe_version(version=complete_draft, actor=manager)

        response = keeper_client.post(
            reverse("kitchen:version_approve", args=[complete_draft.pk]),
            {
                "approval_reference": REFERENCE,
                "approval_evidence_kind": ApprovalEvidenceKind.SIGNED_FORM,
            },
        )

        assert response.status_code == 403
        complete_draft.refresh_from_db()
        assert complete_draft.status == RecipeVersionStatus.SUBMITTED

    def test_a_storekeeper_cannot_submit(
        self, keeper_client: Client, complete_draft: RecipeVersion
    ) -> None:
        response = keeper_client.post(reverse("kitchen:version_submit", args=[complete_draft.pk]))

        assert response.status_code == 403
        complete_draft.refresh_from_db()
        assert complete_draft.status == RecipeVersionStatus.DRAFT

    def test_an_accountant_cannot_activate(
        self, accountant: User, approved_version: RecipeVersion
    ) -> None:
        client = Client()
        client.force_login(accountant)

        response = client.post(
            reverse("kitchen:version_activate", args=[approved_version.pk]),
            {"effective_from": JULY.isoformat()},
        )

        assert response.status_code == 403
        approved_version.refresh_from_db()
        assert approved_version.status == RecipeVersionStatus.APPROVED

    def test_a_purchasing_role_cannot_review(
        self, complete_draft: RecipeVersion, manager: User, branch: Branch
    ) -> None:
        from apps.kitchen.lifecycle import submit_recipe_version
        from apps.organizations.models import Role
        from apps.organizations.services import grant_branch_access

        from .conftest import PASSWORD

        submit_recipe_version(version=complete_draft, actor=manager)
        buyer = User.objects.create_user(username="buyer", password=PASSWORD)
        grant_branch_access(user=buyer, branch=branch, role=Role.PURCHASING)
        client = Client()
        client.force_login(User.objects.get(pk=buyer.pk))

        response = client.post(
            reverse("kitchen:version_review", args=[complete_draft.pk]),
            {"review_type": RecipeReviewType.KITCHEN, "decision": RecipeReviewDecision.APPROVED},
        )

        assert response.status_code == 403
        assert not complete_draft.reviews.exists()

    def test_another_organization_gets_404_not_403(
        self, rival_client: Client, active_version: RecipeVersion
    ) -> None:
        """
        A 403 would confirm the version exists, which turns an id-guessing loop
        into a census of somebody else's menu.
        """
        for url in (
            reverse("kitchen:version_detail", args=[active_version.pk]),
            reverse("kitchen:version_compare", args=[active_version.pk]),
            reverse("kitchen:version_timeline", args=[active_version.pk]),
        ):
            assert rival_client.get(url).status_code == 404

    def test_a_foreign_version_cannot_be_acted_on(
        self, rival_client: Client, approved_version: RecipeVersion
    ) -> None:
        response = rival_client.post(
            reverse("kitchen:version_activate", args=[approved_version.pk]),
            {"effective_from": JULY.isoformat()},
        )

        assert response.status_code == 404
        approved_version.refresh_from_db()
        assert approved_version.status == RecipeVersionStatus.APPROVED

    def test_the_htmx_and_full_page_paths_are_permission_equivalent(
        self, cashier_client: Client, active_version: RecipeVersion
    ) -> None:
        url = reverse("kitchen:version_timeline", args=[active_version.pk])

        assert cashier_client.get(url).status_code == 403
        assert cashier_client.get(url, headers={"HX-Request": "true"}).status_code == 403


class TestTheScreensDriveTheLifecycle:
    def test_a_manager_can_submit_from_the_screen(
        self, manager_client: Client, complete_draft: RecipeVersion
    ) -> None:
        response = manager_client.post(reverse("kitchen:version_submit", args=[complete_draft.pk]))

        assert response.status_code == 302
        complete_draft.refresh_from_db()
        assert complete_draft.status == RecipeVersionStatus.SUBMITTED

    def test_an_incomplete_draft_is_refused_with_its_reasons(
        self, manager_client: Client, draft: RecipeVersion
    ) -> None:
        response = _arabic(manager_client).post(
            reverse("kitchen:version_submit", args=[draft.pk]), follow=True
        )

        assert "النسخة بلا مكوّنات" in response.content.decode()
        draft.refresh_from_db()
        assert draft.status == RecipeVersionStatus.DRAFT

    def test_a_rejection_needs_its_reason_on_the_screen_too(
        self, manager_client: Client, complete_draft: RecipeVersion, manager: User
    ) -> None:
        from apps.kitchen.lifecycle import submit_recipe_version

        submit_recipe_version(version=complete_draft, actor=manager)

        response = manager_client.post(
            reverse("kitchen:version_reject", args=[complete_draft.pk]), {"reason": ""}
        )

        assert response.status_code == 200
        complete_draft.refresh_from_db()
        assert complete_draft.status == RecipeVersionStatus.SUBMITTED

    def test_activation_from_the_screen_claims_the_range(
        self, approver: User, approved_version: RecipeVersion, branch: Branch
    ) -> None:
        client = Client()
        client.force_login(approver)

        response = client.post(
            reverse("kitchen:version_activate", args=[approved_version.pk]),
            {"effective_from": JULY.isoformat(), "effective_to": "", "reason": ""},
        )

        assert response.status_code == 302
        approved_version.refresh_from_db()
        assert approved_version.status == RecipeVersionStatus.ACTIVE
        assert approved_version.effective_from == JULY
        assert approved_version.branch_scopes.filter(branch=branch).exists()


class TestTheApi:
    def test_the_list_endpoint_returns_the_lifecycle_fields(
        self, manager_client: Client, active_version: RecipeVersion
    ) -> None:
        response = manager_client.get("/api/v1/kitchen/recipe-versions")
        payload = response.json()

        assert response.status_code == 200
        row = next(row for row in payload if row["id"] == active_version.pk)
        assert row["status"] == RecipeVersionStatus.ACTIVE
        assert row["effective_from"] == JULY.isoformat()
        assert row["approval_reference"] == REFERENCE
        assert len(row["reviews"]) == 4
        assert len(row["branch_scopes"]) == 1

    def test_quantities_cross_the_wire_as_exact_strings(
        self, manager_client: Client, active_version: RecipeVersion
    ) -> None:
        raw = manager_client.get(
            f"/api/v1/kitchen/recipe-versions/{active_version.pk}"
        ).content.decode()

        assert '"expected_output_quantity": "10.000000"' in raw

    def test_the_resolver_endpoint_requires_a_date(
        self, manager_client: Client, active_version: RecipeVersion, branch: Branch
    ) -> None:
        response = manager_client.get(
            f"/api/v1/kitchen/recipes/{active_version.recipe_id}/effective-version",
            {"branch_id": str(branch.pk)},
        )

        assert response.status_code == 422

    def test_the_resolver_endpoint_answers_for_a_date(
        self, manager_client: Client, active_version: RecipeVersion, branch: Branch
    ) -> None:
        response = manager_client.get(
            f"/api/v1/kitchen/recipes/{active_version.recipe_id}/effective-version",
            {"branch_id": str(branch.pk), "on_date": JULY.isoformat()},
        )

        assert response.status_code == 200
        assert response.json()["id"] == active_version.pk

    def test_the_whole_lifecycle_runs_through_the_api(
        self,
        complete_draft: RecipeVersion,
        manager: User,
        cook: User,
        keeper: User,
        accountant: User,
        approver: User,
        branch: Branch,
    ) -> None:
        base = f"/api/v1/kitchen/recipe-versions/{complete_draft.pk}"

        def client_for(user: User) -> Client:
            client = Client()
            client.force_login(user)
            return client

        assert client_for(manager).post(f"{base}/submit").status_code == 200
        for reviewer, review_type, extra in (
            (cook, RecipeReviewType.KITCHEN, {}),
            (keeper, RecipeReviewType.STOREKEEPER, {}),
            (
                accountant,
                RecipeReviewType.ACCOUNTING,
                {
                    "evidence_reference": REFERENCE,
                    "evidence_kind": ApprovalEvidenceKind.SIGNED_FORM,
                },
            ),
        ):
            response = client_for(reviewer).post(
                f"{base}/review",
                data=json.dumps(
                    {
                        "review_type": review_type,
                        "decision": RecipeReviewDecision.APPROVED,
                        **extra,
                    }
                ),
                content_type="application/json",
            )
            assert response.status_code == 201, response.content

        approving = client_for(approver)
        assert (
            approving.post(
                f"{base}/approve",
                data=json.dumps(
                    {
                        "approval_reference": REFERENCE,
                        "approval_evidence_kind": ApprovalEvidenceKind.SIGNED_FORM,
                    }
                ),
                content_type="application/json",
            ).status_code
            == 200
        )
        activated = approving.post(
            f"{base}/activate",
            data=json.dumps({"effective_from": JULY.isoformat()}),
            content_type="application/json",
        )

        assert activated.status_code == 200
        assert activated.json()["status"] == RecipeVersionStatus.ACTIVE
        assert activated.json()["branch_scopes"][0]["branch_code"] == branch.code

    def test_the_api_refuses_an_approval_by_the_author(
        self, complete_draft: RecipeVersion, manager: User, manager_client: Client
    ) -> None:
        from apps.kitchen.lifecycle import submit_recipe_version

        submit_recipe_version(version=complete_draft, actor=manager)

        response = manager_client.post(
            f"/api/v1/kitchen/recipe-versions/{complete_draft.pk}/approve",
            data=json.dumps(
                {
                    "approval_reference": REFERENCE,
                    "approval_evidence_kind": ApprovalEvidenceKind.SIGNED_FORM,
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 422

    def test_a_cashier_reaches_no_lifecycle_endpoint(
        self, cashier_client: Client, active_version: RecipeVersion
    ) -> None:
        assert cashier_client.get("/api/v1/kitchen/recipe-versions").status_code == 403

    def test_a_foreign_version_is_404_on_the_api(
        self, rival_client: Client, active_version: RecipeVersion
    ) -> None:
        response = rival_client.get(f"/api/v1/kitchen/recipe-versions/{active_version.pk}")

        assert response.status_code == 404

    def test_the_costing_routes_arrived_and_the_production_ones_did_not(self) -> None:
        """
        **Task 3.3 brought the cost routes in**, so that half of the original
        claim is now false and this test was rewritten rather than deleted.

        Production and flattening are Tasks 3.4 and 3.5, and neither route may
        appear before its task.
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
        for path in kitchen:
            for forbidden in ("production", "batch", "flatten"):
                assert forbidden not in path.lower(), path

    def test_no_endpoint_exposes_a_money_field(
        self, manager_client: Client, active_version: RecipeVersion
    ) -> None:
        payload = manager_client.get(f"/api/v1/kitchen/recipe-versions/{active_version.pk}").json()

        for key in payload:
            assert not any(word in key for word in ("cost", "price", "margin", "amount"))


class TestAdminStaysReadOnly:
    def test_the_lifecycle_models_are_registered_read_only(self) -> None:
        from django.contrib import admin

        from apps.kitchen.models import RecipeVersionBranchScope, RecipeVersionReview

        for model in (RecipeVersionReview, RecipeVersionBranchScope):
            registered = admin.site._registry[model]
            assert registered.has_add_permission(None) is False  # type: ignore[arg-type]
            assert registered.has_change_permission(None) is False  # type: ignore[arg-type]
            assert registered.has_delete_permission(None) is False  # type: ignore[arg-type]
