"""
The posting surface: the API, the screens, and who may reach them.

Three claims run through this file and none of them is about rendering.

**Authority is a warehouse question, asked three times.** Reading, drafting and
posting are separate grants and reversing is a fourth. A hidden button is
presentation; the control is the permission check that runs on POST whether or
not the button was ever drawn, and each one below is exercised by a hand-made
request from somebody who never saw the screen.

**Money is omitted, never blanked.** A production reader without
`view_recipe_cost` receives no value key at all — not a `null`. The posted
values live on their own endpoint behind that permission, so redaction is
structural rather than a conditional somebody can forget. The raw bytes are
searched, because a key that is absent from a serializer and present in the
response is exactly the bug this catches.

**Out of scope is 404.** A 403 about another branch's batch confirms the batch
exists, and ids are sequential.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.inventory.models import Warehouse
from apps.kitchen.models import ProductionBatch, ProductionBatchStatus
from apps.kitchen.production import record_production_output
from apps.kitchen.production_posting import post_production_batch
from apps.users.models import User

pytestmark = pytest.mark.django_db

API = "/api/v1/kitchen"

#: Every word a money column would use, in the language the operator reads.
MONEY_LABELS = ("قيمة المستهلك", "قيمة الناتج", "كلفة الوحدة", "إجمالي الكلفة")
#: Every key a serializer would use.
MONEY_KEYS = ("input_value", "output_value", "consumed_value", "unit_cost", "inventory_value")


def _arabic(client: Client) -> Client:
    """The test settings force English, so the cookie is the only way to see Arabic."""
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
    return client


@pytest.fixture
def ready_batch(
    posting_store: Warehouse, production_draft: ProductionBatch, manager: User
) -> ProductionBatch:
    """A draft standing in a stocked warehouse, with its output entered."""
    item = production_draft.recipe.output_item
    assert item is not None
    record_production_output(
        batch=production_draft,
        entered_quantity=Decimal("40"),
        entered_unit=item.base_unit,
        actor=manager,
    )
    return ProductionBatch.objects.get(pk=production_draft.pk)


@pytest.fixture
def posted_batch(ready_batch: ProductionBatch, manager: User) -> ProductionBatch:
    return post_production_batch(batch=ready_batch, idempotency_key="SURFACE-POST", actor=manager)


class TestThePostingApi:
    def test_a_batch_posts_over_the_wire(
        self, manager_client: Client, ready_batch: ProductionBatch
    ) -> None:
        response = manager_client.post(
            f"{API}/production-batches/{ready_batch.pk}/post",
            data=json.dumps({"idempotency_key": "API-1", "reason": "ترحيل"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == ProductionBatchStatus.POSTED
        assert payload["number"].startswith("PRD-")

    def test_the_posting_response_carries_no_money(
        self, manager_client: Client, ready_batch: ProductionBatch
    ) -> None:
        """The command answers what happened, never what it was worth."""
        response = manager_client.post(
            f"{API}/production-batches/{ready_batch.pk}/post",
            data=json.dumps({"idempotency_key": "API-2"}),
            content_type="application/json",
        )
        body = response.content.decode()

        assert response.status_code == 200
        for key in MONEY_KEYS:
            assert key not in body, key

    def test_a_retry_with_the_same_key_returns_the_same_posting(
        self, manager_client: Client, ready_batch: ProductionBatch
    ) -> None:
        first = manager_client.post(
            f"{API}/production-batches/{ready_batch.pk}/post",
            data=json.dumps({"idempotency_key": "API-3"}),
            content_type="application/json",
        )
        again = manager_client.post(
            f"{API}/production-batches/{ready_batch.pk}/post",
            data=json.dumps({"idempotency_key": "API-3"}),
            content_type="application/json",
        )

        assert first.status_code == again.status_code == 200
        assert first.json()["number"] == again.json()["number"]

    def test_a_second_key_on_a_posted_batch_is_refused(
        self, manager_client: Client, ready_batch: ProductionBatch
    ) -> None:
        manager_client.post(
            f"{API}/production-batches/{ready_batch.pk}/post",
            data=json.dumps({"idempotency_key": "API-4"}),
            content_type="application/json",
        )
        again = manager_client.post(
            f"{API}/production-batches/{ready_batch.pk}/post",
            data=json.dumps({"idempotency_key": "API-4-OTHER"}),
            content_type="application/json",
        )

        assert again.status_code == 422
        assert "production_batch_already_posted" in again.content.decode()

    def test_a_reversal_runs_over_the_wire_and_needs_a_reason(
        self, manager_client: Client, posted_batch: ProductionBatch
    ) -> None:
        blank = manager_client.post(
            f"{API}/production-batches/{posted_batch.pk}/reverse",
            data=json.dumps({"idempotency_key": "API-5", "reason": "  "}),
            content_type="application/json",
        )
        assert blank.status_code == 422

        done = manager_client.post(
            f"{API}/production-batches/{posted_batch.pk}/reverse",
            data=json.dumps({"idempotency_key": "API-5", "reason": "خطأ"}),
            content_type="application/json",
        )
        assert done.status_code == 200
        assert done.json()["status"] == ProductionBatchStatus.REVERSED

    def test_a_second_reversal_is_refused(
        self, manager_client: Client, posted_batch: ProductionBatch
    ) -> None:
        manager_client.post(
            f"{API}/production-batches/{posted_batch.pk}/reverse",
            data=json.dumps({"idempotency_key": "API-6", "reason": "خطأ"}),
            content_type="application/json",
        )
        again = manager_client.post(
            f"{API}/production-batches/{posted_batch.pk}/reverse",
            data=json.dumps({"idempotency_key": "API-6-B", "reason": "مرة أخرى"}),
            content_type="application/json",
        )

        assert again.status_code == 422
        assert "production_batch_already_reversed" in again.content.decode()

    def test_an_unready_draft_is_refused_and_moves_nothing(
        self, manager_client: Client, posting_store: Warehouse, production_draft: ProductionBatch
    ) -> None:
        from apps.inventory.models import StockMovement

        before = StockMovement.objects.count()
        response = manager_client.post(
            f"{API}/production-batches/{production_draft.pk}/post",
            data=json.dumps({"idempotency_key": "API-7"}),
            content_type="application/json",
        )

        assert response.status_code == 422
        assert StockMovement.objects.count() == before
        assert ProductionBatch.objects.get(pk=production_draft.pk).number == ""


class TestAllocationsOverTheWire:
    def test_an_allocation_set_is_replaced_not_appended(
        self, manager_client: Client, ready_batch: ProductionBatch
    ) -> None:
        line = ready_batch.lines.first()
        assert line is not None
        actual = line.actuals.first()
        assert actual is not None

        response = manager_client.post(
            f"{API}/production-actual-lines/{actual.pk}/allocations",
            data=json.dumps({"rows": [{"base_quantity": str(actual.base_quantity)}]}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert len(response.json()) == 1

        # The same call again is the same answer, not a second row.
        again = manager_client.post(
            f"{API}/production-actual-lines/{actual.pk}/allocations",
            data=json.dumps({"rows": [{"base_quantity": str(actual.base_quantity)}]}),
            content_type="application/json",
        )
        assert len(again.json()) == 1

    def test_a_partial_allocation_is_refused(
        self, manager_client: Client, ready_batch: ProductionBatch
    ) -> None:
        """Summing to less than the consumption is a partial completion by another name."""
        line = ready_batch.lines.first()
        assert line is not None
        actual = line.actuals.first()
        assert actual is not None

        response = manager_client.post(
            f"{API}/production-actual-lines/{actual.pk}/allocations",
            data=json.dumps({"rows": [{"base_quantity": "0.001"}]}),
            content_type="application/json",
        )
        assert response.status_code == 422
        assert "production_allocation_total_mismatch" in response.content.decode()

    def test_a_foreign_lot_cannot_be_named(
        self, manager_client: Client, ready_batch: ProductionBatch, rival_item: object
    ) -> None:
        """A submitted id selects from what the caller reaches; it never widens it."""
        line = ready_batch.lines.first()
        assert line is not None
        actual = line.actuals.first()
        assert actual is not None

        response = manager_client.post(
            f"{API}/production-actual-lines/{actual.pk}/allocations",
            data=json.dumps(
                {"rows": [{"base_quantity": str(actual.base_quantity), "lot_id": 999999}]}
            ),
            content_type="application/json",
        )
        assert response.status_code == 404


class TestScopeAndAuthority:
    def test_a_foreign_batch_is_404_and_never_403(
        self, rival_client: Client, ready_batch: ProductionBatch
    ) -> None:
        response = rival_client.post(
            f"{API}/production-batches/{ready_batch.pk}/post",
            data=json.dumps({"idempotency_key": "X-1"}),
            content_type="application/json",
        )
        assert response.status_code == 404

    def test_a_reader_without_post_authority_is_403(
        self, cost_reader_client: Client, ready_batch: ProductionBatch
    ) -> None:
        """An accountant reads production and posts none of it."""
        response = cost_reader_client.post(
            f"{API}/production-batches/{ready_batch.pk}/post",
            data=json.dumps({"idempotency_key": "X-2"}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_a_storekeeper_posts_and_may_not_reverse(
        self, keeper_client: Client, ready_batch: ProductionBatch
    ) -> None:
        """
        The separation that makes posting controllable.

        The storekeeper is at the scale and commits the movement; undoing a
        posted economic event is supervisory, and a post that could both make
        and unmake its own movements has no control over it at all.
        """
        posted = keeper_client.post(
            f"{API}/production-batches/{ready_batch.pk}/post",
            data=json.dumps({"idempotency_key": "X-3"}),
            content_type="application/json",
        )
        assert posted.status_code == 200

        refused = keeper_client.post(
            f"{API}/production-batches/{ready_batch.pk}/reverse",
            data=json.dumps({"idempotency_key": "X-4", "reason": "خطأ"}),
            content_type="application/json",
        )
        assert refused.status_code == 403

    def test_a_hidden_button_is_not_authorization(
        self, cost_reader_client: Client, ready_batch: ProductionBatch
    ) -> None:
        """A hand-made POST from somebody who never saw the screen."""
        response = cost_reader_client.post(
            reverse("kitchen:production_post", args=[ready_batch.pk]), {"reason": ""}
        )
        assert response.status_code == 403
        assert ProductionBatch.objects.get(pk=ready_batch.pk).is_draft

    def test_htmx_and_the_full_page_refuse_identically(
        self, cost_reader_client: Client, ready_batch: ProductionBatch
    ) -> None:
        url = reverse("kitchen:production_post", args=[ready_batch.pk])
        full = cost_reader_client.get(url)
        fragment = cost_reader_client.get(url, headers={"HX-Request": "true"})
        assert full.status_code == fragment.status_code == 403


class TestCostRedaction:
    def test_the_valued_endpoint_needs_the_cost_permission(
        self, keeper_client: Client, posted_batch: ProductionBatch
    ) -> None:
        """A storekeeper posts the batch and never learns what it was worth."""
        response = keeper_client.get(f"{API}/production-batches/{posted_batch.pk}/posting")
        assert response.status_code == 403

    def test_the_valued_endpoint_answers_a_cost_reader(
        self, cost_reader_client: Client, posted_batch: ProductionBatch
    ) -> None:
        response = cost_reader_client.get(f"{API}/production-batches/{posted_batch.pk}/posting")
        payload = response.json()

        assert response.status_code == 200
        assert payload["value_is_conserved"] is True
        assert payload["input_value"] == payload["output_value"]
        # Exact strings, both directions. A JSON number is a binary float
        # before any Python code sees it.
        assert isinstance(payload["output_value"], str)
        assert payload["source_document_type"] == "KITCHEN_PRODUCTION_BATCH"

    def test_the_no_journal_case_says_so_in_words(
        self, cost_reader_client: Client, posted_batch: ProductionBatch
    ) -> None:
        payload = cost_reader_client.get(
            f"{API}/production-batches/{posted_batch.pk}/posting"
        ).json()

        assert payload["journal_entry_number"] is None
        assert payload["no_journal_reason"]

    def test_the_batch_screen_omits_money_without_the_permission(
        self, keeper_client: Client, posted_batch: ProductionBatch
    ) -> None:
        """
        Omitted, not blanked. A blanked column tells the reader a number exists
        and that they are not trusted with it, which is a different statement
        from the one intended — so the raw bytes are searched.
        """
        body = (
            _arabic(keeper_client)
            .get(reverse("kitchen:production_movements", args=[posted_batch.pk]))
            .content.decode()
        )
        for label in MONEY_LABELS:
            assert label not in body, label

    def test_the_batch_screen_shows_money_to_a_cost_reader(
        self, cost_reader_client: Client, posted_batch: ProductionBatch
    ) -> None:
        body = (
            _arabic(cost_reader_client)
            .get(reverse("kitchen:production_movements", args=[posted_batch.pk]))
            .content.decode()
        )
        assert "قيمة الناتج" in body


class TestScreens:
    def test_the_posting_confirmation_renders_in_arabic(
        self, manager_client: Client, ready_batch: ProductionBatch
    ) -> None:
        body = (
            _arabic(manager_client)
            .get(reverse("kitchen:production_post", args=[ready_batch.pk]))
            .content.decode()
        )
        assert 'dir="rtl"' in body
        assert "تأكيد الترحيل" in body

    def test_the_reversal_screen_does_not_claim_nothing_is_reversed(
        self, manager_client: Client, posted_batch: ProductionBatch
    ) -> None:
        """
        The discard confirmation says in words that nothing is being reversed.
        True for a draft, and exactly wrong here — so reversal has its own
        template, and this asserts the two never merge again.
        """
        body = (
            _arabic(manager_client)
            .get(reverse("kitchen:production_reverse", args=[posted_batch.pk]))
            .content.decode()
        )
        assert "لا يوجد ترحيل ولا حركة مخزنية" not in body
        assert "تأكيد العكس" in body

    def test_the_movement_panel_answers_htmx_and_a_full_page(
        self, cost_reader_client: Client, posted_batch: ProductionBatch
    ) -> None:
        url = reverse("kitchen:production_movements", args=[posted_batch.pk])
        full = cost_reader_client.get(url)
        fragment = cost_reader_client.get(url, headers={"HX-Request": "true"})

        assert full.status_code == fragment.status_code == 200
        assert len(fragment.content) < len(full.content)

    def test_the_allocation_screen_renders(
        self, manager_client: Client, ready_batch: ProductionBatch
    ) -> None:
        line = ready_batch.lines.first()
        assert line is not None
        actual = line.actuals.first()
        assert actual is not None
        body = (
            _arabic(manager_client)
            .get(reverse("kitchen:production_allocate", args=[actual.pk]))
            .content.decode()
        )
        assert "تخصيص اللوطات والمواقع" in body
