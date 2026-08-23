"""
Contracts for the kitchen overview screen.

Two commitments hold this screen together. The pipeline numbers must come from
the caller's own scope — a rival organization's recipes count for nothing. And
the cost panel is gated by `view_recipe_cost` at the *screen* level: the
storekeeper reads the recipe card and its quantities every day, and must never
find a cost figure on the module's front page.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.kitchen.dashboard import kitchen_overview
from apps.kitchen.models import RecipeVersion, RecipeVersionStatus
from apps.users.models import User

pytestmark = pytest.mark.django_db


def test_the_pipeline_counts_the_callers_scope_only(
    draft: RecipeVersion, manager: User, rival_manager: User
) -> None:
    mine = kitchen_overview(manager, include_cost=True)
    theirs = kitchen_overview(rival_manager, include_cost=True)

    assert mine.recipe_count == 1
    assert mine.portion_count == 1
    assert mine.batch_count == 0
    assert mine.draft_version_count == 1
    assert [s.status for s in mine.statuses] == [RecipeVersionStatus.DRAFT]
    # Nothing approved, nothing sellable — the alert keys on this.
    assert mine.sellable is False

    assert theirs.recipe_count == 0
    assert theirs.statuses == []


def test_without_cost_rights_the_snapshot_list_is_gone(draft: RecipeVersion, manager: User) -> None:
    # The gate is the flag, not the emptiness of the table: a caller without
    # the permission gets no snapshot rows even on the day the table fills.
    redacted = kitchen_overview(manager, include_cost=False)
    assert redacted.snapshots == []
    # The structural counts survive: the pipeline is not money.
    assert redacted.recipe_count == 1


def test_the_storekeeper_front_page_carries_no_cost_panel(
    draft: RecipeVersion, manager_client: Client, keeper_client: Client
) -> None:
    url = reverse("kitchen:overview")

    manager_body = manager_client.get(url).content.decode()
    keeper_body = keeper_client.get(url).content.decode()

    # Both roles read the module; only one is entitled to cost.
    assert "نظرة عامة على المطبخ" in manager_body
    assert "نظرة عامة على المطبخ" in keeper_body
    assert "لقطات الكلفة" in manager_body
    assert "لقطات الكلفة" not in keeper_body
    # The un-sellable alert is not a cost and warns both.
    assert "لا نسخة وصفة فعّالة" in keeper_body
