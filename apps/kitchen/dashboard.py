"""
The kitchen overview, as one scoped read.

What this screen deliberately does **not** show is a live plate cost. A cost
is a function of one exact version, one warehouse and one as-of date — the
costing module refuses a defaulted date by design, because "today, probably
MAIN" produces a number that looks authoritative and is not (RCP-026). A
dashboard that quietly picked both would be the one place that rule is easiest
to violate and hardest to notice. So the cost panel here reads
`RecipeCostSnapshot` — the append-only record a person explicitly created,
carrying its own date, warehouse and authority flag — and the live calculation
stays on the cost card screen where the caller names both.

Cost figures sit behind `kitchen.view_recipe_cost` and are omitted rather than
zeroed, matching the report screens: a cook without the permission gets a
screen with no cost panel, not a menu that appears to cost nothing.

Everything here is a read. Nothing writes, posts, or caches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Count

from apps.kitchen.models import (
    RecipeCostSnapshot,
    RecipeType,
    RecipeVersionStatus,
)
from apps.kitchen.selectors import (
    reachable_organization_ids,
    visible_recipes,
    visible_versions,
)
from apps.users.models import User

#: Snapshots listed on the overview. The full history has its own screen.
TOP_SNAPSHOTS = 8


@dataclass(frozen=True)
class StatusSlice:
    """One version status and how many versions hold it."""

    status: str
    label: str
    count: int


@dataclass(frozen=True)
class SnapshotRow:
    """One stored cost record, exactly as it was frozen."""

    recipe_code: str
    recipe_name: str
    as_of_date: object
    warehouse_code: str
    is_authoritative: bool
    total_material_cost: Decimal
    plate_cost: Decimal | None


@dataclass(frozen=True)
class KitchenOverview:
    """
    Everything the overview screen renders, already scoped and redacted.

    `snapshots` is empty for a caller without cost rights — and also for one
    *with* them before anybody has frozen a card, which is why the template
    keys the panel on `show_cost` rather than on the list being non-empty.
    """

    recipe_count: int
    portion_count: int
    batch_count: int
    active_version_count: int
    draft_version_count: int
    statuses: list[StatusSlice] = field(default_factory=list)
    snapshots: list[SnapshotRow] = field(default_factory=list)

    @property
    def sellable(self) -> bool:
        """A menu can only sell against an ACTIVE version somewhere."""
        return self.active_version_count > 0


def kitchen_overview(user: User, *, include_cost: bool) -> KitchenOverview:
    """
    Build the overview for everything `user` can read.

    `include_cost` is the caller's decision, not this function's: the view
    holds the request and therefore the permission, and passing it in keeps
    the redaction testable without a request object.
    """
    recipes = visible_recipes(user).filter(is_active=True)
    versions = visible_versions(user)

    type_counts = dict(recipes.values_list("recipe_type").annotate(total=Count("id")).order_by())
    status_counts = dict(versions.values_list("status").annotate(total=Count("id")).order_by())
    # Rendered in lifecycle order rather than by count: the pipeline is the
    # story, and a bar chart sorted by size would shuffle it every week.
    statuses = [
        StatusSlice(status=value, label=str(label), count=status_counts.get(value, 0))
        for value, label in RecipeVersionStatus.choices
        if status_counts.get(value, 0)
    ]

    overview = KitchenOverview(
        recipe_count=recipes.count(),
        portion_count=type_counts.get(RecipeType.PORTION, 0),
        batch_count=type_counts.get(RecipeType.BATCH, 0),
        active_version_count=status_counts.get(RecipeVersionStatus.ACTIVE, 0),
        draft_version_count=status_counts.get(RecipeVersionStatus.DRAFT, 0),
        statuses=statuses,
    )
    if not include_cost:
        return overview

    snapshots = [
        SnapshotRow(
            recipe_code=snapshot.recipe_code,
            recipe_name=snapshot.recipe_name,
            as_of_date=snapshot.as_of_date,
            warehouse_code=snapshot.warehouse_code,
            is_authoritative=snapshot.is_authoritative,
            total_material_cost=snapshot.total_material_cost,
            plate_cost=snapshot.plate_cost,
        )
        for snapshot in RecipeCostSnapshot.objects.filter(
            organization_id__in=reachable_organization_ids(user)
        ).order_by("-created_at")[:TOP_SNAPSHOTS]
    ]
    return KitchenOverview(
        recipe_count=overview.recipe_count,
        portion_count=overview.portion_count,
        batch_count=overview.batch_count,
        active_version_count=overview.active_version_count,
        draft_version_count=overview.draft_version_count,
        statuses=overview.statuses,
        snapshots=snapshots,
    )
