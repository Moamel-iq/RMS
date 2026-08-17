"""
The one interpretation of a recipe graph, shared by costing and production.

Task 3.3 walked this graph to price a version. Task 3.4 walks it to draft a
production batch. Those are the same walk, and letting each task keep its own
copy would be the beginning of two systems that disagree about what a recipe
contains — not immediately, but the first time somebody fixes a path-ordering
bug in one of them. So the walk lives here, both callers use it, and a test
asserts that neither reimplements it.

## What it returns and what it refuses to know

`ExpandedLeaf` is a **fact about the recipe** and nothing else: which exact
version, which exact line, by what path, scaled by what cumulative multiplier.

It carries no money, reads no inventory valuation, resolves no date, touches no
warehouse and writes no row. That is not minimalism for its own sake — it is
what lets costing multiply the leaves by a warehouse average and production
multiply the same leaves by a batch multiplier, without either one inheriting
the other's assumptions.

## The rules the walk keeps

* **Follows `RecipeComponent.component_version` exactly.** Never
  `resolve_recipe_version`, never the newest child, never the currently active
  one. A parent frozen in July still expands the blend it named in July after
  that blend is superseded in September (RCP-072, RCP-081).
* **A stocked sub-recipe is one leaf.** Its `output_item` is an inventory item
  on a `RecipeLine`; its ingredient tree is never re-expanded, because its book
  value already contains those ingredients (RCP-071). Nothing here has to
  remember that — RCP-070 makes the other shape unrepresentable at the database.
* **The same item on two paths stays two leaves.** Collapsing them would hide
  which level to fix, and both a cost card and a requirement list exist to be
  traced.
* **Multipliers compose at full precision** and never quantize on the way down
  (RCP-073, ADR-006). Where the product is finally rounded is the *caller's*
  storage boundary, and the two callers have different ones.
* **A corrupted graph is refused, loudly.** The graph is already acyclic and
  depth-bounded by Task 3.2B, so reaching either refusal means something got
  past three guards. A walk that silently stopped at the limit would return a
  short answer that still looks like an answer — too small a total for costing,
  too few requirements for production — and a walk that did not stop would hang
  the request thread. Both are worse than a named refusal.
* **Order is business order.** Component path, then leaf line order, then item
  code; never primary-key order. Two databases restored in a different order
  must produce the same card and the same requirement list in the same
  sequence, or no diff between them is readable.

## A note on the refusal codes

They read `recipe_expansion_*` rather than `recipe_cost_*`. Task 3.3 raised the
latter while it owned the walk; a production draft refused for a cycle is not a
costing failure, and the code a caller sees should say what actually happened.
The costing test that named the old code was rewritten rather than deleted —
the behaviour it guards is unchanged, and only the name moved with the walk.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.kitchen.models import (
    MAX_COMPONENT_DEPTH,
    Recipe,
    RecipeComponent,
    RecipeLine,
    RecipeVersion,
)

if TYPE_CHECKING:
    from django.utils.functional import _StrPromise

ONE = Decimal("1")


def _refuse(message: str | _StrPromise, code: str) -> ValidationError:
    """A domain refusal with a stable code — 422 at the API, never a 500."""
    return ValidationError(message, code=code)


class LeafKind(StrEnum):
    """Where an expanded leaf came from."""

    #: A `RecipeLine` on the version being expanded.
    DIRECT = "DIRECT"
    #: A `RecipeLine` on a nested non-stocked child, reached through components.
    COMPONENT = "COMPONENT"


@dataclass(frozen=True)
class ExpandedLeaf:
    """
    One economic path from a root version to one inventory item.

    Frozen, because both callers hand these to arithmetic and neither should be
    able to edit the recipe's own facts on the way. Everything derived from
    them — an extension, a planned quantity — belongs to the caller.
    """

    #: Component `line_order`s from the root. Empty for the root's own lines.
    path: tuple[int, ...]
    kind: LeafKind
    #: The **exact** version this line belongs to. For a `COMPONENT` leaf this
    #: is the frozen `component_version`, whatever its status is now.
    version: RecipeVersion
    recipe: Recipe
    line: RecipeLine
    #: The product of every multiplier from the root, at full precision.
    cumulative_multiplier: Decimal
    #: `DISH v2 <- BLEND v1`, for a screen and for a stored snapshot. Built
    #: during the walk because only the walk knows the versions on the way.
    label_path: str

    @property
    def path_display(self) -> str:
        """`2.1`, or an empty string for a line the root owns itself."""
        return ".".join(str(step) for step in self.path)

    @property
    def multiplier_display(self) -> str:
        """The cumulative factor as a technical identity: period, never comma."""
        return f"{self.cumulative_multiplier.normalize():f}"

    @property
    def item(self) -> object:
        return self.line.item

    @property
    def cost_class(self) -> str:
        return str(self.line.cost_class)

    @property
    def is_optional(self) -> bool:
        return bool(self.line.is_optional)

    @property
    def base_quantity(self) -> Decimal:
        """What one batch of the **leaf's own** recipe consumes."""
        return self.line.base_quantity


def expand_recipe_version(version: RecipeVersion) -> list[ExpandedLeaf]:
    """
    Every leaf `RecipeLine` under one exact version, in deterministic order.

    Pure: reads the recipe graph, returns facts, writes nothing and prices
    nothing. See the module docstring for the rules it keeps and why.

    Raises `recipe_expansion_graph_cycle` or `recipe_expansion_graph_too_deep`
    rather than truncating.
    """
    leaves: list[ExpandedLeaf] = []

    def label(current: RecipeVersion, recipe: Recipe) -> str:
        return f"{recipe.code} v{current.version_number}"

    def walk(
        current: RecipeVersion,
        recipe: Recipe,
        path: tuple[int, ...],
        cumulative: Decimal,
        on_path: tuple[int, ...],
        kind: LeafKind,
        trail: str,
    ) -> None:
        if current.pk in on_path:
            raise _refuse(
                _("رسم الوصفات الفرعية يحتوي على دورة ولا يمكن توسيعه."),
                "recipe_expansion_graph_cycle",
            )
        if len(path) > MAX_COMPONENT_DEPTH:
            raise _refuse(
                _("عمق الوصفات الفرعية يتجاوز الحد المسموح ولا يمكن توسيعه."),
                "recipe_expansion_graph_too_deep",
            )

        for line in RecipeLine.objects.filter(version=current).select_related(
            "item", "item__base_unit"
        ):
            leaves.append(
                ExpandedLeaf(
                    path=path,
                    kind=kind,
                    version=current,
                    recipe=recipe,
                    line=line,
                    cumulative_multiplier=cumulative,
                    label_path=trail,
                )
            )

        components = (
            RecipeComponent.objects.filter(version=current)
            .select_related("component_version", "component_recipe")
            .order_by("line_order")
        )
        for component in components:
            child = component.component_version
            child_recipe = component.component_recipe
            walk(
                child,
                child_recipe,
                (*path, component.line_order),
                # Full precision the whole way down. See the module docstring.
                cumulative * component.multiplier,
                (*on_path, current.pk),
                LeafKind.COMPONENT,
                f"{trail} ← {label(child, child_recipe)}",
            )

    walk(
        version,
        version.recipe,
        (),
        ONE,
        (),
        LeafKind.DIRECT,
        label(version, version.recipe),
    )
    return order_leaves(leaves)


def order_leaves(leaves: list[ExpandedLeaf]) -> list[ExpandedLeaf]:
    """
    Component path, then leaf line order, then item code.

    Exposed rather than inlined so a caller re-sorting stored rows can prove it
    uses the same key the walk used, instead of a second one that agrees today.
    The empty path sorts first, so a version's own lines lead and each
    component's subtree follows in its own `line_order`.
    """
    return sorted(leaves, key=lambda leaf: (leaf.path, leaf.line.line_order, leaf.line.item.code))
