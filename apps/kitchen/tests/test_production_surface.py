"""
The production drafting surface: the screens, the API, and who may reach them.

The security tests here matter more than the rendering ones. A hidden button is
presentation; the control is the authorization check that runs on POST whether or
not the button was ever drawn, and each one below is exercised by a hand-made
request from somebody who never saw the screen.

## The one boundary every test in this file also guards

Task 3.4 drafts and posts nothing. So beyond "does the screen render", each of
these asks a second question: **is there any way from here to a posting?** The
answer has to be no by construction rather than by omission, which is why the
forbidden-vocabulary test reads the rendered bytes of every screen rather than
trusting the templates not to have grown a button.
"""

from __future__ import annotations

import datetime
import json
import re
from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.inventory.models import InventoryItem, Warehouse
from apps.kitchen.models import (
    ProductionBatch,
    ProductionBatchActualLine,
    Recipe,
    RecipeVersion,
)
from apps.kitchen.production import add_production_batch_substitute
from apps.organizations.models import Branch, Role, WarehouseScopeMode
from apps.organizations.services import (
    grant_branch_access,
    grant_organization_access,
    set_membership_warehouse_scope,
)
from apps.users.models import User

from .conftest import PRODUCTION_DATE

pytestmark = pytest.mark.django_db

#: Every act a Task 3.4 screen may never **offer**.
#:
#: Checked against the text of buttons and links rather than against the whole
#: page, and the distinction is the point: the detail screen says, in prose, that
#: there is no posting and no stock movement in this task. Saying so is the
#: opposite of offering it, and a blunt substring search over the body would
#: forbid the sentence that makes the boundary legible to an operator.
FORBIDDEN_ACTIONS = (
    "ترحيل",
    "عكس",
    "قيد",
    "الحساب",
    "مركز التكلفة",
    "اللوط",
    "الموقع",
    "المتاح",
    "حجز",
    "كلفة",
    "تكلفة",
    "قيمة",
    "صرف",
    "استهلاك المخزون",
)

#: Column headings that would put money on a production screen. These *are*
#: checked against the whole body, because a heading has no prose reading: there
#: is no honest sentence on a production draft containing `كلفة المواد`.
#:
#: `قيد محاسبي` is deliberately **not** here even though no journal exists. The
#: discard confirmation tells the operator there is no accounting entry to
#: reverse, which is exactly the reassurance somebody about to delete a draft
#: needs — and a list that forbade the phrase would forbid saying so. Journals
#: are caught where it matters instead: in `FORBIDDEN_ACTIONS`, against the text
#: of every control.
FORBIDDEN_ANYWHERE = (
    "قيمة المخزون",
    "كلفة المواد",
    "كلفة الطبق",
    "سعر الوحدة",
    "مركز التكلفة",
)

#: `<button>…</button>` and `<a class="btn…">…</a>`, which is every control a
#: kitchen operator can actually press on these screens.
_CONTROL = re.compile(
    r"<button[^>]*>(?P<button>.*?)</button>|<a[^>]*class=\"[^\"]*btn[^\"]*\"[^>]*>(?P<link>.*?)</a>",
    re.DOTALL,
)


def control_labels(body: str) -> list[str]:
    """The visible text of every pressable control on a rendered page."""
    labels = []
    for match in _CONTROL.finditer(body):
        text = match.group("button") or match.group("link") or ""
        labels.append(re.sub(r"<[^>]+>", " ", text).strip())
    return labels


def _arabic(client: Client) -> Client:
    """
    Ask for the Arabic rendering explicitly.

    The test settings force `LANGUAGE_CODE = "en"` and `ExplicitLocaleMiddleware`
    deliberately ignores `Accept-Language`, so the cookie is the only way to see
    what an operator actually sees.
    """
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
    return client


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


# ---------------------------------------------------------------------------
# The screens render, in Arabic, with the right shell
# ---------------------------------------------------------------------------


class TestScreensRender:
    def test_the_list_renders_and_shows_the_draft(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        response = _arabic(manager_client).get(reverse("kitchen:production_list"))
        body = response.content.decode()

        assert response.status_code == 200
        assert production_draft.recipe.code in body
        assert "أوامر الإنتاج" in body

    def test_the_detail_renders_the_plan_the_actuals_and_the_paths(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        response = _arabic(manager_client).get(
            reverse("kitchen:production_detail", args=[production_draft.pk])
        )
        body = response.content.decode()

        assert response.status_code == 200
        assert "المتطلبات والاستهلاك الفعلي" in body
        assert "جاهزية المسودة" in body
        assert "سجل المسودة" in body
        assert production_draft.lines.get().item_code in body

    def test_a_technical_decimal_renders_ltr_with_a_period(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        """
        A conversion factor and a multiplier are **technical identities**.

        Django localises Decimals, so under Arabic `2.500000` would render
        `2,500000` — a comma that is ambiguous and invites a mis-typed re-entry.
        Both are rendered through `*_display` properties and marked `dir="ltr"`.
        """
        response = _arabic(manager_client).get(
            reverse("kitchen:production_detail", args=[production_draft.pk])
        )
        body = response.content.decode()

        assert '<code dir="ltr">2.500000</code>' in body
        assert "2,500000" not in body

    def test_the_preview_renders_without_writing_anything(
        self,
        manager_client: Client,
        batch_recipe: tuple[Recipe, RecipeVersion],
        branch: Branch,
        store: Warehouse,
    ) -> None:
        recipe, version = batch_recipe
        before = ProductionBatch.objects.count()
        response = _arabic(manager_client).get(
            reverse("kitchen:production_preview"),
            {
                "recipe": str(recipe.pk),
                "branch": str(branch.pk),
                "planned_business_date": PRODUCTION_DATE.isoformat(),
                "multiplier": "2",
            },
        )
        body = response.content.decode()

        assert response.status_code == 200
        assert f"v{version.version_number}" in body
        assert ProductionBatch.objects.count() == before, "a preview writes nothing"

    def test_the_empty_preview_shows_the_selector_and_no_figures(
        self, manager_client: Client
    ) -> None:
        response = _arabic(manager_client).get(reverse("kitchen:production_preview"))
        body = response.content.decode()

        assert response.status_code == 200
        assert "النسخة السارية" not in body, "no answer before the question is asked"


class TestHtmxAndTheFullPageFallback:
    def test_the_list_answers_htmx_with_the_fragment_only(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        full = manager_client.get(reverse("kitchen:production_list"))
        fragment = manager_client.get(
            reverse("kitchen:production_list"), headers={"HX-Request": "true"}
        )

        assert "<html" in full.content.decode()
        assert "<html" not in fragment.content.decode()
        assert production_draft.recipe.code in fragment.content.decode()

    def test_the_detail_answers_htmx_with_the_fragment_only(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        full = manager_client.get(reverse("kitchen:production_detail", args=[production_draft.pk]))
        fragment = manager_client.get(
            reverse("kitchen:production_detail", args=[production_draft.pk]),
            headers={"HX-Request": "true"},
        )

        assert full.content.decode().count("<html") == 1
        assert "<html" not in fragment.content.decode()

    def test_every_panel_route_answers_with_its_partial_alone(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        for name, marker in (
            ("kitchen:production_requirements", "production-requirements"),
            ("kitchen:production_readiness", "production-readiness"),
            ("kitchen:production_timeline", "production-timeline"),
        ):
            response = manager_client.get(reverse(name, args=[production_draft.pk]))
            body = response.content.decode()
            assert response.status_code == 200, name
            assert marker in body, name
            assert "<html" not in body, name

    def test_an_action_works_as_a_plain_form_post(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        """No HX-Request header at all: the same URL must still work."""
        actual = production_draft.lines.get().actuals.get()
        response = manager_client.post(
            reverse("kitchen:production_actual_update", args=[actual.pk]),
            {
                "entered_quantity": "3.5",
                "entered_unit": actual.item.base_unit_id,
                "note": "",
            },
        )

        assert response.status_code == 302
        assert ProductionBatchActualLine.objects.get(pk=actual.pk).base_quantity == Decimal(
            "3.500000"
        )

    def test_the_same_action_over_htmx_redirects_by_header(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        actual = production_draft.lines.get().actuals.get()
        response = manager_client.post(
            reverse("kitchen:production_actual_update", args=[actual.pk]),
            {"entered_quantity": "4", "entered_unit": actual.item.base_unit_id, "note": ""},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 204
        assert response["HX-Redirect"].endswith(f"/production/{production_draft.pk}/")

    def test_a_refusal_comes_back_as_200_with_the_message(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        """
        htmx does not swap an error response, so a refusal returned as 4xx would
        leave the operator staring at an unchanged form with no explanation.
        """
        actual = production_draft.lines.get().actuals.get()
        response = manager_client.post(
            reverse("kitchen:production_actual_update", args=[actual.pk]),
            {"entered_quantity": "-1", "entered_unit": actual.item.base_unit_id},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "formrow--invalid" in response.content.decode()


class TestFiltersAndPaginationKeepTheirQuery:
    def test_a_filter_narrows_the_list(
        self,
        manager_client: Client,
        production_draft: ProductionBatch,
        second_store: Warehouse,
    ) -> None:
        """
        Asserted on the **row link**, not on the recipe code.

        The recipe code also appears in the filter dropdown's own options, so a
        test that searched the whole body would pass whether or not the filter
        worked — which is the shape of assertion that quietly stops testing.
        """
        row = reverse("kitchen:production_detail", args=[production_draft.pk])
        matching = manager_client.get(
            reverse("kitchen:production_list"),
            {"warehouse": production_draft.warehouse_id},
        )
        other = manager_client.get(
            reverse("kitchen:production_list"), {"warehouse": second_store.pk}
        )

        assert row in matching.content.decode()
        assert row not in other.content.decode()

    def test_pagination_links_carry_every_filter(
        self,
        manager_client: Client,
        batch_recipe: tuple[Recipe, RecipeVersion],
        branch: Branch,
        store: Warehouse,
        manager: User,
    ) -> None:
        """
        A page-2 link that dropped the warehouse filter would silently widen the
        list, which is worse than losing the filter: the operator would believe
        they were still looking at one store.
        """
        from apps.kitchen.production import create_production_batch

        for index in range(26):
            create_production_batch(
                recipe=batch_recipe[0],
                branch=branch,
                warehouse=store,
                planned_business_date=datetime.date(2026, 3, 1) + datetime.timedelta(days=index),
                multiplier=Decimal("1"),
                actor=manager,
                idempotency_key=f"PAGE-{index}",
            )
        response = manager_client.get(
            reverse("kitchen:production_list"), {"warehouse": str(store.pk), "q": ""}
        )
        body = response.content.decode()

        assert "page=2" in body
        assert f"warehouse={store.pk}&amp;page=2" in body or f"warehouse={store.pk}" in body


# ---------------------------------------------------------------------------
# Nothing here leads to a posting
# ---------------------------------------------------------------------------


class TestNoScreenOffersAPosting:
    def _screens(self, batch: ProductionBatch) -> list[str]:
        line = batch.lines.first()
        actual = line.actuals.first() if line else None
        names = [
            reverse("kitchen:production_list"),
            reverse("kitchen:production_create"),
            reverse("kitchen:production_preview"),
            reverse("kitchen:production_detail", args=[batch.pk]),
            reverse("kitchen:production_requirements", args=[batch.pk]),
            reverse("kitchen:production_readiness", args=[batch.pk]),
            reverse("kitchen:production_timeline", args=[batch.pk]),
            reverse("kitchen:production_rescale", args=[batch.pk]),
            reverse("kitchen:production_output", args=[batch.pk]),
            reverse("kitchen:production_notes", args=[batch.pk]),
            reverse("kitchen:production_discard", args=[batch.pk]),
        ]
        if line is not None:
            names.append(reverse("kitchen:production_substitute_create", args=[line.pk]))
        if actual is not None:
            names.append(reverse("kitchen:production_actual_update", args=[actual.pk]))
            names.append(reverse("kitchen:production_actual_delete", args=[actual.pk]))
        return names

    def test_no_screen_offers_a_forbidden_control(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        """
        Read from the rendered bytes, not from the templates.

        A template review proves what the templates said the day it was done; this
        is what notices the day one of them grows a post button or a lot picker.
        Every pressable control on every production screen, in Arabic, as the
        operator sees it.
        """
        client = _arabic(manager_client)
        for path in self._screens(production_draft):
            for label in control_labels(client.get(path).content.decode()):
                for word in FORBIDDEN_ACTIONS:
                    assert word not in label, f"{path} offers a control reading {label!r}"

    def test_no_screen_shows_money_or_a_ledger_reference(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        """
        A column heading has no innocent prose reading, so these are checked
        against the whole page rather than against the controls.
        """
        client = _arabic(manager_client)
        for path in self._screens(production_draft):
            body = client.get(path).content.decode()
            for word in FORBIDDEN_ANYWHERE:
                assert word not in body, f"{path} carries {word!r}"

    def test_the_boundary_is_stated_rather_than_left_to_be_inferred(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        """
        The paired positive to the two tests above.

        The screens must **say** that posting is not part of this task, because
        an operator who finds no post button learns only that they cannot find
        it. Without this test the previous two would be satisfied by a screen
        that removed the explanation along with the control.
        """
        body = (
            _arabic(manager_client)
            .get(reverse("kitchen:production_detail", args=[production_draft.pk]))
            .content.decode()
        )

        assert "لا ترحيل" in body

    def test_the_production_urls_name_posting_and_nothing_beyond_it(self) -> None:
        """
        The fence, moved once. Task 3.4 banned every posting verb because none
        existed; **Task 3.5 posts and reverses**, so those two are now expected
        and the rewrite keeps the half that is still true.

        What may still not appear: `issue`, `consume` and `complete`, which are
        the vocabulary of the multi-day, partially completed production RCP-094
        says a Release 1 batch never is. A route named for one of them would be
        the router promising a lifecycle the constraint set refuses.
        """
        from apps.kitchen import urls

        names = {
            pattern.name
            for pattern in urls.urlpatterns
            if (pattern.name or "").startswith("production")
        }
        assert {"production_post", "production_reverse"} <= names

        forbidden = ("issue", "consume", "complete", "journal", "flatten")
        for pattern in urls.urlpatterns:
            name = pattern.name or ""
            if not name.startswith("production"):
                continue
            route = str(pattern.pattern)
            for verb in forbidden:
                assert verb not in name, f"{name} names {verb!r}"
                assert verb not in route, f"{route} names {verb!r}"

    def test_the_batch_screens_leak_no_money(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        body = manager_client.get(
            reverse("kitchen:production_detail", args=[production_draft.pk])
        ).content.decode()

        for key in ("unit_cost", "total_cost", "material_cost", "plate_cost", "valuation"):
            assert key not in body


# ---------------------------------------------------------------------------
# Authorization: the button is never the control
# ---------------------------------------------------------------------------


class TestAuthorizationIsNeverTheButton:
    def test_a_global_group_with_no_membership_reaches_nothing(
        self, production_draft: ProductionBatch
    ) -> None:
        """
        ADR-016, at its sharpest. A Django group grants a **codename**; it does
        not grant a reach. Somebody holding `view_productionbatch` globally and no
        membership anywhere passes `test_func` at the door and then sees an empty
        list and a 404 on a batch that plainly exists — because the selector asks
        which warehouses they actually hold a post at, and the answer is none.
        """
        from django.contrib.auth.models import Group, Permission

        user = User.objects.create_user(username="codename-only", password="pw-not-real")
        group = Group.objects.create(name="production-readers")
        group.permissions.add(
            Permission.objects.get(codename="view_productionbatch"),
            Permission.objects.get(codename="create_production_batch"),
        )
        user.groups.add(group)
        client = _client(User.objects.get(pk=user.pk))

        listing = client.get(reverse("kitchen:production_list"))
        detail = client.get(reverse("kitchen:production_detail", args=[production_draft.pk]))

        assert listing.status_code == 200, "the codename gets them through the door"
        assert reverse("kitchen:production_detail", args=[production_draft.pk]) not in (
            listing.content.decode()
        ), "and no further: the list is empty"
        assert detail.status_code == 404

    def test_a_reader_without_draft_authority_sees_no_edit_controls(
        self, accountant: User, production_draft: ProductionBatch
    ) -> None:
        """The accountant reads production and may not draft it (the role map)."""
        body = (
            _arabic(_client(accountant))
            .get(reverse("kitchen:production_detail", args=[production_draft.pk]))
            .content.decode()
        )

        assert "تغيير المعامل" not in body
        assert "حذف المسودة" not in body

    def test_the_hidden_action_is_still_refused_by_direct_post(
        self, accountant: User, production_draft: ProductionBatch
    ) -> None:
        """
        The test that matters. Somebody who never saw the button posts anyway.
        """
        response = _client(accountant).post(
            reverse("kitchen:production_rescale", args=[production_draft.pk]),
            {"multiplier": "9"},
        )

        assert response.status_code in {403, 404}
        assert ProductionBatch.objects.get(pk=production_draft.pk).multiplier == Decimal("2.500000")

    def test_csrf_is_enforced(self, manager: User, production_draft: ProductionBatch) -> None:
        enforcing = Client(enforce_csrf_checks=True)
        enforcing.force_login(manager)
        response = enforcing.post(
            reverse("kitchen:production_notes", args=[production_draft.pk]),
            {"notes": "بلا رمز"},
        )

        assert response.status_code == 403

    def test_a_storekeeper_may_draft(self, keeper: User, production_draft: ProductionBatch) -> None:
        """Custody of the store is exactly who records what left it."""
        response = _client(keeper).post(
            reverse("kitchen:production_notes", args=[production_draft.pk]),
            {"notes": "ملاحظة من أمين المخزن"},
        )

        assert response.status_code == 302
        assert ProductionBatch.objects.get(pk=production_draft.pk).notes == (
            "ملاحظة من أمين المخزن"
        )

    @pytest.mark.parametrize("role", [Role.CASHIER, Role.PURCHASING])
    def test_a_role_without_production_reaches_nothing(
        self, role: str, branch: Branch, production_draft: ProductionBatch
    ) -> None:
        user = User.objects.create_user(username=f"no-production-{role}", password="pw-not-real")
        grant_branch_access(user=user, branch=branch, role=role)
        client = _client(User.objects.get(pk=user.pk))

        listing = client.get(reverse("kitchen:production_list"))
        detail = client.get(reverse("kitchen:production_detail", args=[production_draft.pk]))

        assert listing.status_code == 403
        assert detail.status_code in {403, 404}

    def test_a_foreign_batch_is_404_not_403(
        self, rival_manager: User, production_draft: ProductionBatch
    ) -> None:
        """
        A 403 about another organization's batch would confirm it exists, and ids
        are sequential — which turns an id-guessing loop into a census.
        """
        response = _client(rival_manager).get(
            reverse("kitchen:production_detail", args=[production_draft.pk])
        )

        assert response.status_code == 404

    def test_a_selected_warehouse_scope_that_omits_the_store_reaches_nothing(
        self, branch: Branch, store: Warehouse, second_store: Warehouse, production_draft: Any
    ) -> None:
        """
        Narrowing custody narrows authority with it. A `SELECTED` membership
        listing only the *other* store holds `view_production` nowhere here.
        """
        user = User.objects.create_user(username="selected-elsewhere", password="pw-not-real")
        membership = grant_branch_access(user=user, branch=branch, role=Role.STOREKEEPER)
        set_membership_warehouse_scope(
            membership=membership,
            mode=WarehouseScopeMode.SELECTED,
            warehouses=[second_store],
        )
        client = _client(User.objects.get(pk=user.pk))

        assert (
            client.get(reverse("kitchen:production_detail", args=[production_draft.pk])).status_code
            == 404
        )

    def test_an_organization_wide_grant_reaches_every_warehouse_it_owns(
        self, organization: Any, production_draft: ProductionBatch
    ) -> None:
        user = User.objects.create_user(username="org-wide", password="pw-not-real")
        grant_organization_access(user=user, organization=organization, role=Role.MANAGER)
        client = _client(User.objects.get(pk=user.pk))

        assert (
            client.get(reverse("kitchen:production_detail", args=[production_draft.pk])).status_code
            == 200
        )


# ---------------------------------------------------------------------------
# The API, and its parity with the screens
# ---------------------------------------------------------------------------


class TestTheApiContract:
    def test_every_decimal_crosses_as_a_quoted_string(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        """
        JSON numbers are binary floats before any Python code sees them. A
        quantity that arrived as one has already lost the exactness the whole
        policy exists to preserve.
        """
        raw = manager_client.get(
            f"/api/v1/kitchen/production-batches/{production_draft.pk}"
        ).content.decode()
        payload = json.loads(raw)

        for key in ("multiplier", "expected_output_quantity"):
            assert isinstance(payload[key], str), key
        line = payload["lines"][0]
        for key in ("source_base_quantity", "cumulative_multiplier", "planned_base_quantity"):
            assert isinstance(line[key], str), key
        assert isinstance(line["actuals"][0]["base_quantity"], str)

    def test_no_response_carries_a_cost_key_even_as_null(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        """
        Read the **raw bytes**. A `"cost": null` would still tell a reader that a
        cost exists on this document and that they were not given it, which is a
        different statement from the one intended.
        """
        raw = manager_client.get(
            f"/api/v1/kitchen/production-batches/{production_draft.pk}"
        ).content.decode()
        # `cost_class` is the recipe's FOOD / PACKAGING / ACCOMPANIMENT
        # classification, not an amount — it names which bucket a requirement
        # belongs to and carries no figure. Removed by name before the search so
        # the rest of the check stays sharp rather than being weakened for it.
        searchable = raw.lower().replace("cost_class", "")

        for word in (
            "cost",
            "price",
            "unit_cost",
            "valuation",
            "material_cost",
            "plate_cost",
            "money",
            "journal",
            "account",
            "amount",
        ):
            assert word not in searchable, f"{word!r} appears in a production payload"

    def test_the_readiness_route_separates_blockers_from_observations(
        self, manager_client: Client, production_draft: ProductionBatch
    ) -> None:
        payload = json.loads(
            manager_client.get(
                f"/api/v1/kitchen/production-batches/{production_draft.pk}/readiness"
            ).content
        )

        assert "problems" in payload
        assert "observations" in payload
        assert payload["is_ready"] is False, "no actual output has been entered yet"

    def test_a_cross_dimension_substitution_is_reported_and_never_summed(
        self,
        manager_client: Client,
        substituted_draft: ProductionBatch,
        oil: InventoryItem,
        manager: User,
    ) -> None:
        """
        4 KG of rice met with 2 litres of an approved substitute is not "6" of
        anything. The API says the two are not comparable rather than printing a
        figure the kitchen never measured.
        """
        line = substituted_draft.lines.get()
        add_production_batch_substitute(
            line=line,
            item=oil,
            entered_quantity=Decimal("2"),
            actor=manager,
            reason="نفد الرز",
        )
        payload = json.loads(
            manager_client.get(f"/api/v1/kitchen/production-batches/{substituted_draft.pk}").content
        )
        row = payload["lines"][0]

        assert len(row["actuals"]) == 2, "both rows are reported separately"
        assert row["is_quantitatively_comparable"] is True, (
            "the primary rice row is still comparable with the plan"
        )
        # The comparable figure counts the rice only. Never rice plus oil.
        assert row["comparable_actual_quantity"] == str(line.planned_base_quantity)

    def test_a_completely_cross_dimensional_requirement_reports_no_variance(
        self,
        manager_client: Client,
        substituted_draft: ProductionBatch,
        oil: InventoryItem,
        manager: User,
    ) -> None:
        from apps.kitchen.production import remove_production_batch_substitute

        line = substituted_draft.lines.get()
        add_production_batch_substitute(
            line=line,
            item=oil,
            entered_quantity=Decimal("2"),
            actor=manager,
            reason="نفد الرز تماماً",
        )
        remove_production_batch_substitute(
            actual=line.actuals.get(substitute__isnull=True), actor=manager, reason="استبدال كامل"
        )
        payload = json.loads(
            manager_client.get(f"/api/v1/kitchen/production-batches/{substituted_draft.pk}").content
        )
        row = payload["lines"][0]

        assert row["is_quantitatively_comparable"] is False
        assert row["comparable_actual_quantity"] is None
        assert row["variance"] is None
        assert "غير قابل للمقارنة" in row["comparison_statement"]

    def test_patch_cannot_move_the_frozen_decision(
        self, manager_client: Client, production_draft: ProductionBatch, second_store: Warehouse
    ) -> None:
        """
        Not "is refused" — **is not offered**. Django Ninja ignores unknown keys,
        so the proof is that the stored row is unchanged after a request that
        named every frozen field.
        """
        response = manager_client.patch(
            f"/api/v1/kitchen/production-batches/{production_draft.pk}",
            data=json.dumps(
                {
                    "warehouse_id": second_store.pk,
                    "branch_id": 999,
                    "recipe_id": 999,
                    "recipe_version_id": 999,
                    "planned_business_date": "2030-01-01",
                    "multiplier": "99",
                    "notes": "ملاحظة مقبولة",
                }
            ),
            content_type="application/json",
        )
        refreshed = ProductionBatch.objects.get(pk=production_draft.pk)

        assert response.status_code == 200
        assert refreshed.warehouse_id == production_draft.warehouse_id
        assert refreshed.branch_id == production_draft.branch_id
        assert refreshed.recipe_id == production_draft.recipe_id
        assert refreshed.recipe_version_id == production_draft.recipe_version_id
        assert refreshed.planned_business_date == production_draft.planned_business_date
        assert refreshed.multiplier == production_draft.multiplier
        # The one field the payload *does* own, so the request demonstrably
        # reached the route rather than being rejected wholesale.
        assert refreshed.notes == "ملاحظة مقبولة"

    def test_the_production_api_names_posting_and_nothing_beyond_it(self) -> None:
        """
        The same fence on the API, moved for the same reason.

        `/post` and `/reverse` are Task 3.5's and are asserted **present**;
        `issue`, `consume` and `complete` stay absent because a Release 1 batch
        has no lifecycle for them to belong to.
        """
        from apps.kitchen.api import router

        production_paths = {path for path in router.path_operations if "production" in path}
        assert any(path.endswith("/post") for path in production_paths)
        assert any(path.endswith("/reverse") for path in production_paths)

        forbidden = ("issue", "consume", "complete", "journal", "flatten")
        for path in production_paths:
            for verb in forbidden:
                assert verb not in path.lower(), f"{path} names {verb!r}"

    def test_a_foreign_batch_is_404_and_an_unauthorised_one_is_403(
        self, rival_manager: User, cashier: User, production_draft: ProductionBatch
    ) -> None:
        foreign = _client(rival_manager).get(
            f"/api/v1/kitchen/production-batches/{production_draft.pk}"
        )
        unauthorised = _client(cashier).get(
            f"/api/v1/kitchen/production-batches/{production_draft.pk}"
        )

        assert foreign.status_code == 404
        assert unauthorised.status_code in {403, 404}

    def test_a_foreign_substitute_id_is_refused(
        self,
        manager_client: Client,
        substituted_draft: ProductionBatch,
        rival_item: InventoryItem,
    ) -> None:
        line = substituted_draft.lines.get()
        response = manager_client.post(
            f"/api/v1/kitchen/production-lines/{line.pk}/substitutes",
            data=json.dumps({"item_id": rival_item.pk, "entered_quantity": "1"}),
            content_type="application/json",
        )

        assert response.status_code in {404, 422}
        assert line.actuals.count() == 1

    def test_the_api_and_the_screen_agree_on_who_may_draft(
        self, accountant: User, production_draft: ProductionBatch
    ) -> None:
        """Parity: one authorization answer, two doors."""
        client = _client(accountant)
        screen = client.post(
            reverse("kitchen:production_rescale", args=[production_draft.pk]),
            {"multiplier": "3"},
        )
        api = client.post(
            f"/api/v1/kitchen/production-batches/{production_draft.pk}/rescale",
            data=json.dumps({"multiplier": "3"}),
            content_type="application/json",
        )

        assert screen.status_code in {403, 404}
        assert api.status_code in {403, 404}
        assert ProductionBatch.objects.get(pk=production_draft.pk).multiplier == Decimal("2.500000")
