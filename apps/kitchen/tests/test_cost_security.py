"""
Who may see what a dish costs, and what the screens and payloads say to
everybody else.

`view_recipe` gets somebody the card. What the dish costs is a **separate
permission** with a separate answer (RCP-027), the same boundary inventory
draws between `view_stock` and `view_valuation` and procurement draws between
`view_supplier` and `view_supplier_cost`.

Two rules are tested rather than described, because both are the sort of thing
that looks right and is not:

* **Cost keys are omitted, never blanked.** A `null` in a JSON payload or an
  empty cell in a table tells the reader a number exists and that they are not
  trusted with it — a different statement from the one intended, and one that
  leaks the shape of the data. The assertions read **raw response bytes** for
  that reason: a test that deserialized first would pass on a payload that had
  the key with a null in it.
* **Hiding a button is presentation, never protection.** Every screen is also
  reached by a hand-made request from somebody who never saw the button.
"""

from __future__ import annotations

import datetime
import json

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse

from apps.inventory.models import Warehouse
from apps.kitchen.costing import cost_recipe_version
from apps.kitchen.models import RecipeCostSnapshot, RecipeVersion
from apps.kitchen.permissions import (
    ROLE_PERMISSIONS,
    VIEW_RECIPE,
    VIEW_RECIPE_COST,
)
from apps.kitchen.snapshots import create_recipe_cost_snapshot
from apps.organizations.models import Role
from apps.users.models import User

pytestmark = pytest.mark.django_db

#: Cost keys that must never appear in an unauthorized payload — as keys, not
#: as values. Searched for in the raw bytes.
COST_KEYS = (
    b"total_material_cost",
    b"cost_per_output_unit",
    b"food_total",
    b"packaging_total",
    b"unit_cost",
    b"allocated_extension",
    b"plate_cost",
    b"portions_per_batch",
)

#: The Arabic labels an unauthorized reader must not see on any page. Checked
#: as well as the JSON keys, because a screen leaks in a different language
#: from a payload.
COST_LABELS = (
    "كلفة الطبق",
    "إجمالي كلفة المواد",
    "كلفة وحدة الناتج",
    "عدد الأطباق في الدفعة",
)

#: Fields Task 3.3 must never expose anywhere, to anybody.
COMMERCIAL_KEYS = (
    b"selling_price",
    b"profit",
    b"margin",
    b"commission",
    b"cost_percentage",
)


def _today() -> datetime.date:
    from django.utils import timezone

    return timezone.localdate()


def _query(warehouse: Warehouse) -> dict[str, str]:
    """
    The screen's two required inputs, as strings.

    Typed rather than inlined because a mixed `{str: int | str}` literal infers
    as `dict[str, object]`, which the test client's signature rejects — and the
    fix that matters is naming the type, not silencing the checker.
    """
    return {"warehouse": str(warehouse.pk), "as_of_date": _today().isoformat()}


def _api_query(warehouse: Warehouse) -> dict[str, str]:
    """The same two inputs under the API's parameter names."""
    return {"warehouse_id": str(warehouse.pk), "as_of_date": _today().isoformat()}


@pytest.fixture
def snapshot(
    valued_store: Warehouse, costable_version: RecipeVersion, manager: User
) -> RecipeCostSnapshot:
    card = cost_recipe_version(
        version=costable_version, warehouse=valued_store, as_of_date=_today()
    )
    return create_recipe_cost_snapshot(
        card=card, actor=manager, idempotency_key="SEC-1", reference="R"
    )


class TestTheApprovedRoleMap:
    """
    The map is not widened by Task 3.3. It is *activated*.

    Every assertion below reads `ROLE_PERMISSIONS`, which Task 3.1 decided and
    this task does not touch — so a future change that quietly granted cost to
    a storekeeper fails here rather than in production.
    """

    def test_the_roles_that_read_cost_are_exactly_the_approved_four(self) -> None:
        holders = {
            role for role, granted in ROLE_PERMISSIONS.items() if VIEW_RECIPE_COST in granted
        }
        assert holders == {
            Role.OWNER.value,
            Role.MANAGER.value,
            Role.ACCOUNTING_MANAGER.value,
            Role.ACCOUNTANT.value,
        }

    def test_a_storekeeper_reads_the_card_and_not_the_cost(self) -> None:
        granted = ROLE_PERMISSIONS[Role.STOREKEEPER.value]
        assert VIEW_RECIPE in granted
        assert VIEW_RECIPE_COST not in granted

    def test_purchasing_and_a_viewer_read_no_cost(self) -> None:
        for role in (Role.PURCHASING.value, Role.VIEWER.value):
            assert VIEW_RECIPE_COST not in ROLE_PERMISSIONS[role]

    def test_the_accountant_reads_cost_and_cannot_edit_a_recipe(self) -> None:
        """The workbook assigns `كلفة الوحدة` to المحاسب; editing is the kitchen's."""
        from apps.kitchen.permissions import MANAGE_RECIPE

        granted = ROLE_PERMISSIONS[Role.ACCOUNTANT.value]
        assert VIEW_RECIPE_COST in granted
        assert MANAGE_RECIPE not in granted


class TestTheScreens:
    def test_a_cost_reader_sees_the_card(
        self,
        cost_reader_client: Client,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        response = cost_reader_client.get(
            reverse("kitchen:cost_card", args=[costable_version.pk]),
            _query(valued_store),
        )
        assert response.status_code == 200
        assert b"6,000" in response.content or b"6000" in response.content

    def test_a_storekeeper_is_refused_the_card_outright(
        self, keeper_client: Client, costable_version: RecipeVersion
    ) -> None:
        """403, and no cost key or plate-cost label anywhere in the body."""
        response = keeper_client.get(reverse("kitchen:cost_card", args=[costable_version.pk]))
        assert response.status_code == 403
        for key in COST_KEYS:
            assert key not in response.content
        body = response.content.decode()
        for label in COST_LABELS:
            assert label not in body

    def test_the_version_detail_page_omits_the_cost_link_without_the_permission(
        self, keeper_client: Client, costable_version: RecipeVersion
    ) -> None:
        """
        Omitted, not disabled. An inert control still announces that a costing
        screen exists for this recipe.
        """
        response = keeper_client.get(reverse("kitchen:version_detail", args=[costable_version.pk]))
        assert response.status_code == 200
        assert reverse("kitchen:cost_card", args=[costable_version.pk]).encode() not in (
            response.content
        )

    def test_the_version_detail_page_shows_the_cost_link_with_it(
        self, cost_reader_client: Client, costable_version: RecipeVersion
    ) -> None:
        response = cost_reader_client.get(
            reverse("kitchen:version_detail", args=[costable_version.pk])
        )
        assert response.status_code == 200
        assert reverse("kitchen:cost_card", args=[costable_version.pk]).encode() in (
            response.content
        )

    def test_a_foreign_recipe_is_404_and_not_403(
        self, rival_client: Client, costable_version: RecipeVersion
    ) -> None:
        """
        A 403 about another organization's recipe would confirm it exists, and
        ids are sequential.
        """
        response = rival_client.get(reverse("kitchen:cost_card", args=[costable_version.pk]))
        assert response.status_code == 404

    def test_the_snapshot_list_is_empty_for_a_storekeeper(
        self, keeper_client: Client, snapshot: RecipeCostSnapshot
    ) -> None:
        response = keeper_client.get(reverse("kitchen:cost_snapshot_list"))
        assert response.status_code == 403

    def test_a_hidden_snapshot_button_is_still_a_refused_post(
        self, keeper_client: Client, valued_store: Warehouse, costable_version: RecipeVersion
    ) -> None:
        response = keeper_client.post(
            reverse("kitchen:cost_snapshot_create", args=[costable_version.pk]),
            {
                "warehouse": valued_store.pk,
                "as_of_date": _today().isoformat(),
                "idempotency_key": "FORGED-1",
            },
        )
        assert response.status_code == 403
        assert RecipeCostSnapshot.objects.count() == 0

    def test_htmx_and_the_full_page_enforce_the_same_check(
        self, keeper_client: Client, costable_version: RecipeVersion
    ) -> None:
        """The fragment is a rendering choice, not a second door."""
        url = reverse("kitchen:cost_card", args=[costable_version.pk])
        assert keeper_client.get(url).status_code == 403
        assert keeper_client.get(url, headers={"HX-Request": "true"}).status_code == 403

    def test_htmx_returns_a_fragment_and_the_full_page_returns_a_shell(
        self,
        cost_reader_client: Client,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        url = reverse("kitchen:cost_card", args=[costable_version.pk])
        params = _query(valued_store)
        full = cost_reader_client.get(url, params)
        fragment = cost_reader_client.get(url, params, headers={"HX-Request": "true"})
        assert b"<html" in full.content
        assert b"<html" not in fragment.content
        assert b"cost-card-summary" in fragment.content

    def test_a_global_group_permission_does_not_widen_scope(
        self, cashier: User, costable_version: RecipeVersion
    ) -> None:
        """
        ADR-016: a permission says *what*, a membership says *where*.

        The cashier gets the codename from a Django group, and their branch
        membership carries no cost authority at all. The group alone therefore
        authorizes nothing: the view's second check refuses them.

        **403, not 404**, and the distinction is the rule rather than an
        accident. The recipe is inside their scope and they simply may not read
        its money — the honest answer, disclosing nothing they were not already
        entitled to. A *foreign* organization's recipe is the 404 case, and it
        is tested separately.
        """
        group = Group.objects.create(name="global-cost")
        group.permissions.add(
            Permission.objects.get(content_type__app_label="kitchen", codename="view_recipe_cost")
        )
        cashier.groups.add(group)
        client = Client()
        client.force_login(cashier)
        response = client.get(reverse("kitchen:cost_card", args=[costable_version.pk]))
        assert response.status_code == 403
        for key in COST_KEYS:
            assert key not in response.content


class TestTheApi:
    def test_the_cost_payload_carries_quoted_decimal_strings(
        self,
        cost_reader_client: Client,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        """
        JSON's only numeric type is binary floating point. A total that crossed
        as a number would arrive as a float, and a costing figure that has been
        through a float is no longer the figure that was approved.
        """
        response = cost_reader_client.get(
            f"/api/v1/kitchen/recipe-versions/{costable_version.pk}/cost",
            _api_query(valued_store),
        )
        assert response.status_code == 200
        assert b'"total_material_cost": "6000.000"' in response.content.replace(b'":"', b'": "')
        payload = json.loads(response.content)
        for key in ("total_material_cost", "cost_per_output_unit", "plate_cost"):
            assert isinstance(payload[key], str), key
        assert isinstance(payload["portions_per_batch"], str)
        assert isinstance(payload["lines"][0]["unit_cost"], str)
        serving = payload["servings"][0]
        for key in (
            "cost_per_serving",
            "allocated_total",
            "normal_cost_per_serving",
            "elevated_cost_per_serving",
            "remainder_cost",
        ):
            assert isinstance(serving[key], str), key

    def test_the_payload_has_no_commercial_field(
        self,
        cost_reader_client: Client,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        response = cost_reader_client.get(
            f"/api/v1/kitchen/recipe-versions/{costable_version.pk}/cost",
            _api_query(valued_store),
        )
        for key in COMMERCIAL_KEYS:
            assert key not in response.content

    def test_an_unauthorized_caller_gets_no_cost_key_at_all(
        self, keeper_client: Client, valued_store: Warehouse, costable_version: RecipeVersion
    ) -> None:
        """
        Raw bytes: omitted, not nulled — and `plate_cost` is in the list.

        A `"plate_cost": null` would tell the reader a plate cost exists and
        that they are not trusted with it, which is a different statement from
        the one intended.
        """
        response = keeper_client.get(
            f"/api/v1/kitchen/recipe-versions/{costable_version.pk}/cost",
            _api_query(valued_store),
        )
        assert response.status_code == 403
        for key in COST_KEYS:
            assert key not in response.content

    def test_the_existing_version_endpoint_stays_money_free(
        self, cost_reader_client: Client, costable_version: RecipeVersion
    ) -> None:
        """
        Costing arrived beside the recipe endpoints, not inside them.

        `unit_cost` is excluded from the search deliberately — it is a costing
        word and its absence here is the point.
        """
        response = cost_reader_client.get(f"/api/v1/kitchen/recipe-versions/{costable_version.pk}")
        assert response.status_code == 200
        for key in COST_KEYS:
            assert key not in response.content

    def test_a_foreign_version_is_404(
        self, rival_client: Client, valued_store: Warehouse, costable_version: RecipeVersion
    ) -> None:
        response = rival_client.get(
            f"/api/v1/kitchen/recipe-versions/{costable_version.pk}/cost",
            _api_query(valued_store),
        )
        assert response.status_code == 404

    def test_a_foreign_warehouse_is_404(
        self,
        cost_reader_client: Client,
        rival_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        response = cost_reader_client.get(
            f"/api/v1/kitchen/recipe-versions/{costable_version.pk}/cost",
            _api_query(rival_store),
        )
        assert response.status_code == 404

    def test_the_snapshot_list_is_scoped_by_the_cost_permission(
        self, keeper_client: Client, snapshot: RecipeCostSnapshot
    ) -> None:
        response = keeper_client.get("/api/v1/kitchen/recipe-cost-snapshots")
        assert response.status_code == 200
        assert json.loads(response.content) == []

    def test_a_snapshot_has_no_patch_and_no_delete(
        self, cost_reader_client: Client, snapshot: RecipeCostSnapshot
    ) -> None:
        """The rows refuse both verbs at the database; the router offers neither."""
        url = f"/api/v1/kitchen/recipe-cost-snapshots/{snapshot.pk}"
        assert cost_reader_client.patch(url).status_code in {404, 405}
        assert cost_reader_client.delete(url).status_code in {404, 405}
        assert RecipeCostSnapshot.objects.count() == 1

    def test_the_snapshot_command_is_refused_without_the_permission(
        self, keeper_client: Client, valued_store: Warehouse, costable_version: RecipeVersion
    ) -> None:
        response = keeper_client.post(
            f"/api/v1/kitchen/recipe-versions/{costable_version.pk}/cost-snapshots",
            data=json.dumps(
                {
                    "warehouse_id": valued_store.pk,
                    "as_of_date": _today().isoformat(),
                    "idempotency_key": "FORGED-2",
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 403
        assert RecipeCostSnapshot.objects.count() == 0

    def test_a_preview_route_marks_its_payload_non_authoritative(
        self, cost_reader_client: Client, valued_store: Warehouse, complete_draft: RecipeVersion
    ) -> None:
        response = cost_reader_client.get(
            f"/api/v1/kitchen/recipe-versions/{complete_draft.pk}/cost-preview",
            _api_query(valued_store),
        )
        assert response.status_code == 200
        assert json.loads(response.content)["is_authoritative"] is False


class TestTheArabicSurface:
    def test_recipe_cost_report_uses_frozen_snapshots_and_htmx(
        self, cost_reader_client: Client, snapshot: RecipeCostSnapshot
    ) -> None:
        cost_reader_client.cookies["django_language"] = "ar"
        url = reverse("kitchen:report_recipe_cost")
        full = cost_reader_client.get(url)
        filters: dict[str, str] = {
            "q": snapshot.recipe_code,
            "branch_id": str(snapshot.branch_id),
        }
        fragment = cost_reader_client.get(
            url,
            filters,
            headers={"HX-Request": "true"},
        )
        assert full.status_code == 200
        body = full.content.decode()
        assert "كلفة الوصفات" in body
        assert snapshot.recipe_code in body
        assert f"v{snapshot.version_number}" in body
        assert snapshot.as_of_date.isoformat() in body
        assert snapshot.warehouse_code in body
        assert "كلفة الفاقد المعتمد" in body
        assert "د.ع" in body
        assert reverse("kitchen:report_recipe_cost_detail", args=[snapshot.pk]) in body
        assert fragment.status_code == 200
        assert "<html" not in fragment.content.decode().lower()
        assert snapshot.recipe_code in fragment.content.decode()

    def test_recipe_cost_report_detail_shows_nested_evidence_and_warnings(
        self, cost_reader_client: Client, snapshot: RecipeCostSnapshot
    ) -> None:
        cost_reader_client.cookies["django_language"] = "ar"
        response = cost_reader_client.get(
            reverse("kitchen:report_recipe_cost_detail", args=[snapshot.pk])
        )
        body = response.content.decode()
        assert response.status_code == 200
        assert "تقرير كلفة الوصفات" in body
        assert "فترة نفاذ النسخة" in body
        assert "كلفة الفاقد المعتمد (ضمن الكميات)" in body
        assert "الكمية الفعّالة" in body
        assert "نقطة قطع الدفتر" in body
        assert snapshot.recipe_code in body

    def test_recipe_cost_report_export_is_the_same_scoped_query(
        self, cost_reader_client: Client, snapshot: RecipeCostSnapshot
    ) -> None:
        response = cost_reader_client.get(
            reverse("kitchen:report_recipe_cost"),
            {"q": snapshot.recipe_code, "export": "csv"},
        )
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/csv")
        assert response.content.startswith(b"\xef\xbb\xbf")
        decoded = response.content.decode("utf-8-sig")
        assert snapshot.recipe_code in decoded
        assert "كلفة الدفعة" in decoded
        assert "كلفة الفاقد المعتمد" in decoded

    def test_recipe_cost_report_refuses_non_cost_readers_and_hides_foreign_detail(
        self,
        keeper_client: Client,
        rival_client: Client,
        snapshot: RecipeCostSnapshot,
    ) -> None:
        assert keeper_client.get(reverse("kitchen:report_recipe_cost")).status_code == 403
        assert (
            rival_client.get(
                reverse("kitchen:report_recipe_cost_detail", args=[snapshot.pk])
            ).status_code
            == 404
        )

    def test_the_authorized_card_shows_the_plate_cost(
        self,
        cost_reader_client: Client,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        """The other half of "omitted, not blanked": present for a holder."""
        cost_reader_client.cookies["django_language"] = "ar"
        response = cost_reader_client.get(
            reverse("kitchen:cost_card", args=[costable_version.pk]),
            _query(valued_store),
        )
        body = response.content.decode()
        assert "كلفة الطبق" in body
        assert "عدد الأطباق في الدفعة" in body
        assert "الحصة الأساسية" in body

    def test_the_card_is_labelled_direct_material_cost(
        self,
        cost_reader_client: Client,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        """
        Not "cost", not "plate cost": what the number is.

        The test settings force English, so the screen is asked for Arabic the
        way a real reader would get it.
        """
        cost_reader_client.cookies["django_language"] = "ar"
        response = cost_reader_client.get(
            reverse("kitchen:cost_card", args=[costable_version.pk]),
            _query(valued_store),
        )
        body = response.content.decode()
        assert "كلفة المواد المباشرة" in body
        assert "كلفة الغذاء" in body
        assert "كلفة التغليف" in body

    def test_the_preview_banner_says_it_is_not_approved(
        self, cost_reader_client: Client, valued_store: Warehouse, complete_draft: RecipeVersion
    ) -> None:
        cost_reader_client.cookies["django_language"] = "ar"
        response = cost_reader_client.get(
            reverse("kitchen:cost_card", args=[complete_draft.pk]),
            _query(valued_store),
        )
        assert "معاينة — غير معتمدة" in response.content.decode()

    def test_the_cost_ratio_renders_as_a_dash_with_its_reason(
        self,
        cost_reader_client: Client,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        """
        RCP-093: the ratio is a Phase 4 read, because Phase 3 has no price.

        Rendered as a dash with the reason, never as zero — a zero ratio means
        "not calculated", not "free".
        """
        cost_reader_client.cookies["django_language"] = "ar"
        response = cost_reader_client.get(
            reverse("kitchen:cost_card", args=[costable_version.pk]),
            _query(valued_store),
        )
        body = response.content.decode()
        assert "نسبة الكلفة" in body
        assert "لا يوجد سعر بيع في هذه المرحلة" in body

    def test_technical_factors_render_left_to_right_with_a_period(
        self,
        cost_reader_client: Client,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        """
        Django localises Decimals, so under Arabic a factor would render
        `0,25`. A comma in a re-enterable technical value is ambiguous.
        """
        cost_reader_client.cookies["django_language"] = "ar"
        response = cost_reader_client.get(
            reverse("kitchen:cost_card", args=[costable_version.pk]),
            _query(valued_store),
        )
        body = response.content.decode()
        assert '<code dir="ltr">1500.000000</code>' in body
        assert "1500,000000" not in body

    def test_the_snapshot_detail_shows_its_evidence(
        self, cost_reader_client: Client, snapshot: RecipeCostSnapshot
    ) -> None:
        cost_reader_client.cookies["django_language"] = "ar"
        response = cost_reader_client.get(
            reverse("kitchen:cost_snapshot_detail", args=[snapshot.pk])
        )
        body = response.content.decode()
        assert "نقطة قطع الدفتر" in body
        assert str(snapshot.ledger_cutoff_sequence) in body
        assert snapshot.calculation_version in body

    def test_the_snapshot_command_requires_a_csrf_token(
        self,
        cost_reader: User,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        """
        The one write Task 3.3 offers, and it is a form POST like any other.

        A costing record created by a cross-site request would be a decision
        nobody made, carrying the signed-in user's name.
        """
        enforcing = Client(enforce_csrf_checks=True)
        enforcing.force_login(cost_reader)
        response = enforcing.post(
            reverse("kitchen:cost_snapshot_create", args=[costable_version.pk]),
            {
                "warehouse": valued_store.pk,
                "as_of_date": _today().isoformat(),
                "idempotency_key": "NO-TOKEN-1",
            },
        )
        assert response.status_code == 403
        assert RecipeCostSnapshot.objects.count() == 0

    def test_no_screen_shows_a_selling_price_or_a_margin(
        self,
        cost_reader_client: Client,
        valued_store: Warehouse,
        costable_version: RecipeVersion,
    ) -> None:
        cost_reader_client.cookies["django_language"] = "ar"
        response = cost_reader_client.get(
            reverse("kitchen:cost_card", args=[costable_version.pk]),
            _query(valued_store),
        )
        for word in ("سعر البيع", "هامش الربح", "صافي الربح", "العمولة"):
            assert word not in response.content.decode()
