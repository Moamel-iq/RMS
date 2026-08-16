"""
The nested-recipe graph: cycles, depth, effective coverage and the lock that
makes all three hold under concurrency.

Separated from `lifecycle.py` because it answers a different question. The
lifecycle decides when *one* version may move; this module decides whether the
**graph of versions** is coherent — and a graph is exactly the kind of thing
two transactions can each see as valid a moment before they jointly break it.

**The race is real, and it is worth being exact about which race it is.**

The textbook version — `T1` adds `A → B` while `T2` adds `B → A`, neither sees
the other, both commit a cycle — **cannot happen here**, and not by luck. An
edge may be written only on a `DRAFT` parent and may point only at a frozen
child, and traversal runs parent-to-child; to walk across a newly added edge you
must first arrive at its parent *as somebody's child*, and a draft is never
anybody's child. Two concurrent additions therefore cannot lie on one path.
`TestTwoEdgesCannotCloseACycle` pins that property so it fails the day the
draft-only rule is relaxed.

What the lock **is** for is reading a consistent graph. Cycle and depth are
properties of the whole edge set, and every check here re-reads that set: a walk
that observed the graph half-way through somebody else's multi-edge edit could
certify a version against a picture that never existed. Mutation and
certification therefore both take it, and taking it above every row lock is also
what stops opposite-order callers deadlocking.

What the lock is **not** for is keeping a child effective for a parent's whole
life. Coverage is a point-in-time gate at activation, and a supersession racing
it produces a state an ordinary sequential order produces too — activate the
parent on 1 July, then supersede the child so it closes on 30 June. That is
legitimate: once a parent is `ACTIVE` its `component_version` is a frozen
reference, and the child may be superseded freely afterwards. See
`parents_outliving_child`, which is advisory rather than a refusal.

Every operation that can change the graph, or that certifies it, takes one
**organization-scoped advisory lock** first:

    recipe-component-graph:<organization_id>

Deliberately **not** the account-mapping lock from `apps.core.locks`: that lock
protects "which account carries this role", is taken in shared mode by every
posting in the system, and hanging recipe graph mutation off it would make two
unrelated concerns contend and would let a kitchen edit block a posting. A
separate name costs nothing and says what it protects.

**The full order, and every command here obeys it:**

    1. the component graph lock      advisory, exclusive, per organization
    2. the Recipe rows               row locks, ascending id
    3. the RecipeVersion rows        row locks, ascending id
    4. the RecipeVersionBranchScope  row locks, ascending id

The advisory lock is taken **first and unconditionally**, which is what makes
the rest safe: two callers naming the same two versions in opposite order are
already serialised before they reach a single row lock, so the classic
lock-ordering deadlock cannot form. Ascending id within each level is belt and
braces for callers that hold the graph lock and still touch several rows.

**The persisted graph is authoritative.** Every check re-reads edges from the
database under the lock. Validating the payload a caller submitted would prove
something about the caller's intention and nothing about the graph, and a UI
candidate filter is a convenience, never an enforcement.

**Nothing here computes a cost.** `cumulative_multiplier` is a scaling
identity — how many child batches enter one parent batch, multiplied down the
path — and Task 3.3 is what may eventually multiply it by a price. Task 3.4
owns flattening into production lines. This module builds neither.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection
from django.utils.translation import gettext_lazy as _

from apps.core.quantity import FACTOR_PLACES
from apps.kitchen.models import (
    COMPONENT_ELIGIBLE_STATUSES,
    MAX_COMPONENT_DEPTH,
    RESOLVABLE_VERSION_STATUSES,
    Recipe,
    RecipeComponent,
    RecipeVersion,
    RecipeVersionBranchScope,
    RecipeVersionStatus,
)

#: A multiplier product starts at one, in the Decimal world the factors live in.
ONE = Decimal("1")


def _refuse(message: str, code: str, field_name: str | None = None) -> ValidationError:
    """A refusal that carries a stable code, as every kitchen service does."""
    if field_name is None:
        return ValidationError(message, code=code)
    return ValidationError({field_name: ValidationError(message, code=code)})


# ---------------------------------------------------------------------------
# The graph lock
# ---------------------------------------------------------------------------


def _graph_key(organization_id: int) -> str:
    return f"recipe-component-graph:{organization_id}"


def lock_component_graph(organization_id: int) -> None:
    """
    Hold one organization's component-graph lock exclusively for this
    transaction.

    Taken by every command that may change the graph — create, update, remove,
    reorder — and by every command that *certifies* it: submission, approval,
    activation, and the supersession of a version something else may depend on.
    Certification needs it as much as mutation does: cycle and depth are
    properties of the whole edge set, and a walk that read that set while
    somebody else was half-way through a multi-edge edit would certify a version
    against a graph that never existed.

    Exclusive rather than shared. Graph edits are rare — a recipe is written
    once and read forever — so there is nothing to gain from letting two of them
    interleave, and a shared mode would reintroduce the exact race this exists
    to close. Transaction-scoped, so commit or rollback releases it with no
    cleanup path to forget.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [_graph_key(organization_id)],
        )


# ---------------------------------------------------------------------------
# The persisted graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    """One persisted component link, flattened to what a graph walk needs."""

    parent_version_id: int
    child_version_id: int
    parent_recipe_id: int
    child_recipe_id: int
    parent_label: str
    child_label: str
    line_order: int
    multiplier: Decimal


@dataclass(frozen=True)
class Graph:
    """
    One organization's whole component graph, read once and walked many times.

    Read whole rather than followed lazily row by row: a walk that queries per
    node issues one round trip per edge and — worse — could observe two
    different states of the graph within one validation if anything committed
    between two of its reads. One read under the lock is one consistent picture.
    """

    #: parent version id -> its edges, in `line_order`.
    children: dict[int, tuple[Edge, ...]]
    #: child version id -> the edges that name it.
    parents: dict[int, tuple[Edge, ...]]

    def edges_below(self, version_id: int) -> tuple[Edge, ...]:
        return self.children.get(version_id, ())

    def edges_above(self, version_id: int) -> tuple[Edge, ...]:
        return self.parents.get(version_id, ())


def read_graph(organization_id: int) -> Graph:
    """
    Every component edge in one organization, as an in-memory graph.

    Organization-wide rather than reachable-from-one-version, because the depth
    a new edge produces depends on what sits *above* the parent as well as below
    the child, and "above" is only discoverable by looking at rows the parent
    itself does not reference.
    """
    rows = (
        RecipeComponent.objects.filter(recipe__organization_id=organization_id)
        .select_related("recipe", "component_recipe", "component_version", "version")
        .order_by("version_id", "line_order")
    )
    children: dict[int, list[Edge]] = {}
    parents: dict[int, list[Edge]] = {}
    for row in rows:
        edge = Edge(
            parent_version_id=row.version_id,
            child_version_id=row.component_version_id,
            parent_recipe_id=row.recipe_id,
            child_recipe_id=row.component_recipe_id,
            # Labels are resolved here, once, so no graph walk issues a query.
            parent_label=f"{row.recipe.code} v{row.version.version_number}",
            child_label=f"{row.component_recipe.code} v{row.component_version.version_number}",
            line_order=row.line_order,
            multiplier=row.multiplier,
        )
        children.setdefault(row.version_id, []).append(edge)
        parents.setdefault(row.component_version_id, []).append(edge)
    return Graph(
        children={key: tuple(value) for key, value in children.items()},
        parents={key: tuple(value) for key, value in parents.items()},
    )


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------


def cycle_path(
    graph: Graph,
    *,
    parent_recipe_id: int,
    parent_version_id: int,
    child_version_id: int,
    child_recipe_id: int,
) -> list[str] | None:
    """
    The path a proposed edge would close into a cycle, or `None` if it is safe.

    **Recipe identity, not version identity** (RCP-076). `A v2 → B v1 → A v1` is
    a cycle even though no version repeats: the dish would contain an older
    edition of itself, its expansion would never terminate, and its cost would
    be defined in terms of its own cost. Walking version ids alone would accept
    it, which is why the walk compares `recipe_id` and the database carries a
    matching `CheckConstraint` for the one-edge case.

    Returns the offending path as recipe codes so the refusal can *show* the
    loop. "A cycle exists" sends a chef looking; "خلطة → مرق → خلطة" is the
    answer.
    """
    if child_recipe_id == parent_recipe_id or child_version_id == parent_version_id:
        return [label_of(graph, child_version_id)]

    # Depth-first from the proposed child, looking for the parent's recipe.
    # `seen` bounds the walk over a graph that is already corrupt: a cycle that
    # somehow reached the database must make this fail fast, not recurse until
    # the stack gives out (RCP-076's last sentence).
    stack: list[tuple[int, list[str]]] = [(child_version_id, [label_of(graph, child_version_id)])]
    seen: set[int] = set()
    while stack:
        version_id, trail = stack.pop()
        if version_id in seen:
            continue
        seen.add(version_id)
        for edge in graph.edges_below(version_id):
            if (
                edge.child_recipe_id == parent_recipe_id
                or edge.child_version_id == parent_version_id
            ):
                return [*trail, edge.child_label]
            stack.append((edge.child_version_id, [*trail, edge.child_label]))
    return None


def label_of(graph: Graph, version_id: int) -> str:
    """
    A readable name for a node, from the graph where possible.

    Falls back to one query only for a version the graph has no edge for, which
    is exactly the proposed child of a first component — never inside a walk.
    """
    for edge in graph.edges_above(version_id):
        return edge.child_label
    for edge in graph.edges_below(version_id):
        return edge.parent_label
    version = RecipeVersion.objects.filter(pk=version_id).select_related("recipe").first()
    if version is not None:
        return f"{version.recipe.code} v{version.version_number}"
    return f"#{version_id}"


# ---------------------------------------------------------------------------
# Depth
# ---------------------------------------------------------------------------


def depth_below(graph: Graph, version_id: int) -> tuple[int, list[str]]:
    """
    The longest downward path from a version, in **edges**, with its labels.

    A leaf is 0. `seen` on the current path only — a diamond (two parents using
    one blend) is perfectly legal and must not be mistaken for a loop.
    """
    return _longest(graph, version_id, set())


def _longest(graph: Graph, version_id: int, on_path: set[int]) -> tuple[int, list[str]]:
    if version_id in on_path:
        # A corrupt graph. Stop rather than recurse; the cycle check reports it.
        return 0, []
    best_depth = 0
    best_trail: list[str] = []
    for edge in graph.edges_below(version_id):
        depth, trail = _longest(graph, edge.child_version_id, on_path | {version_id})
        if depth + 1 > best_depth:
            best_depth = depth + 1
            best_trail = [edge.child_label, *trail]
    return best_depth, best_trail


def depth_above(graph: Graph, version_id: int) -> tuple[int, list[str]]:
    """
    The longest upward path to any root, in edges, with its labels nearest-first.

    Needed because a new edge does not only deepen the graph beneath the parent:
    if the parent is itself a component of something, the whole chain above it
    grows by the same amount. Checking only downward would accept a fourth level
    whenever it was added from the middle.
    """
    return _longest_up(graph, version_id, set())


def _longest_up(graph: Graph, version_id: int, on_path: set[int]) -> tuple[int, list[str]]:
    if version_id in on_path:
        return 0, []
    best_depth = 0
    best_trail: list[str] = []
    for edge in graph.edges_above(version_id):
        depth, trail = _longest_up(graph, edge.parent_version_id, on_path | {version_id})
        if depth + 1 > best_depth:
            best_depth = depth + 1
            best_trail = [edge.parent_label, *trail]
    return best_depth, best_trail


def depth_with_edge(
    graph: Graph, *, parent_version_id: int, child_version_id: int
) -> tuple[int, list[str]]:
    """
    How deep the graph becomes if this edge is added, and the path that proves it.

    `above(parent) + 1 + below(child)`. The `+1` is the proposed edge itself.

    The upward half is always zero *today* at the moment an edge is written,
    because the parent of a new edge is a `DRAFT` and a draft is never a child.
    It is measured anyway, and not out of caution: `validate_version_graph` runs
    the same arithmetic at **activation**, where the version being certified may
    already be somebody's component.

    The trail names every node from the top of the chain down to its deepest
    leaf, **including the two ends of the proposed edge**, so a refusal can show
    the whole path rather than the fragment below it.
    """
    up, up_trail = depth_above(graph, parent_version_id)
    down, down_trail = depth_below(graph, child_version_id)
    path = [
        *reversed(up_trail),
        label_of(graph, parent_version_id),
        label_of(graph, child_version_id),
        *down_trail,
    ]
    return up + 1 + down, path


# ---------------------------------------------------------------------------
# Eligibility of one proposed edge
# ---------------------------------------------------------------------------


def validate_component_edge(
    *,
    parent: RecipeVersion,
    child: RecipeVersion,
    graph: Graph,
) -> None:
    """
    Everything that makes one parent/child pair unacceptable, in a fixed order.

    Ordered cheapest and most fundamental first: a foreign organization is
    refused before the shape is examined, and the shape before the graph is
    walked, so the message a caller gets names the *first* thing wrong rather
    than an incidental consequence of it.

    Called under the graph lock with both rows already re-read. It never reads a
    status off a caller-supplied object.
    """
    if child.recipe.organization_id != parent.recipe.organization_id:
        # 404-shaped concerns are the selector's; by here the child was already
        # resolved through the caller's scope, so this is the belt to that brace.
        raise _refuse(
            str(_("الوصفة الفرعية تتبع مؤسسة أخرى.")),
            "recipe_component_foreign_organization",
            "component_version",
        )
    if child.pk == parent.pk:
        raise _refuse(
            str(_("لا يجوز أن تحتوي النسخة على نفسها.")),
            "recipe_component_cycle",
            "component_version",
        )
    if child.recipe_id == parent.recipe_id:
        raise _refuse(
            str(_("لا يجوز أن تحتوي الوصفة على نسخة أخرى من نفسها.")),
            "recipe_component_cycle",
            "component_version",
        )

    # RCP-070. The mutual exclusion, and the reason double counting cannot be
    # represented: a recipe with an output item is stock, and stock is consumed
    # as a line at its book value, never re-expanded.
    if child.recipe.output_item_id is not None:
        raise _refuse(
            str(
                _("الوصفة %(code)s تنتج صنفاً مخزنياً، فتُستهلك كسطر مكوّن على الصنف لا كوصفة فرعية.")
                % {"code": child.recipe.code}
            ),
            "recipe_component_child_is_stocked",
            "component_version",
        )
    if not child.recipe.is_active:
        raise _refuse(
            str(_("الوصفة الفرعية %(code)s مؤرشفة.") % {"code": child.recipe.code}),
            "recipe_component_child_recipe_archived",
            "component_version",
        )
    if child.status not in COMPONENT_ELIGIBLE_STATUSES:
        raise _refuse(
            str(
                _("النسخة الفرعية %(label)s في حالة %(status)s ولا تصلح كمكوّن.")
                % {
                    "label": f"{child.recipe.code} v{child.version_number}",
                    "status": child.get_status_display(),
                }
            ),
            "recipe_component_child_not_eligible",
            "component_version",
        )

    path = cycle_path(
        graph,
        parent_recipe_id=parent.recipe_id,
        parent_version_id=parent.pk,
        child_version_id=child.pk,
        child_recipe_id=child.recipe_id,
    )
    if path is not None:
        origin = f"{parent.recipe.code} v{parent.version_number}"
        raise _refuse(
            str(
                _("هذا الارتباط يُنشئ دورة: %(path)s")
                % {"path": " ← ".join([origin, *path, origin])}
            ),
            "recipe_component_cycle",
            "component_version",
        )

    depth, trail = depth_with_edge(graph, parent_version_id=parent.pk, child_version_id=child.pk)
    if depth > MAX_COMPONENT_DEPTH:
        raise _refuse(
            str(
                _("عمق التداخل %(depth)s يتجاوز الحد %(limit)s: %(path)s")
                % {
                    "depth": depth,
                    "limit": MAX_COMPONENT_DEPTH,
                    "path": " ← ".join(trail) or f"{child.recipe.code} v{child.version_number}",
                }
            ),
            "recipe_component_depth_exceeded",
            "component_version",
        )


def validate_version_graph(version: RecipeVersion, *, graph: Graph | None = None) -> None:
    """
    Re-validate every edge a version already carries, against the persisted
    graph.

    Run again at submission, approval and activation — not only when an edge is
    written. Between writing a component and approving the parent, the child may
    have been rejected, a sibling recipe may have been archived, and somebody
    may have completed a cycle from the other end. Approval is the moment the
    graph acquires authority, so it is the moment to check it again (RCP-076:
    *"on every draft save and again at approval"*).
    """
    graph = graph if graph is not None else read_graph(version.recipe.organization_id)
    components = list(
        RecipeComponent.objects.filter(version=version)
        .select_related(
            "component_version",
            "component_version__recipe",
            "component_recipe",
        )
        .order_by("line_order")
    )
    for component in components:
        child = component.component_version
        if child.recipe.output_item_id is not None:
            raise _refuse(
                str(_("الوصفة الفرعية %(code)s صارت وصفة مخزنية.") % {"code": child.recipe.code}),
                "recipe_component_child_is_stocked",
                "components",
            )
        if child.status not in COMPONENT_ELIGIBLE_STATUSES:
            raise _refuse(
                str(
                    _("النسخة الفرعية %(label)s في حالة %(status)s ولا تصلح كمكوّن.")
                    % {
                        "label": f"{child.recipe.code} v{child.version_number}",
                        "status": child.get_status_display(),
                    }
                ),
                "recipe_component_child_not_eligible",
                "components",
            )
        if component.multiplier <= 0:
            raise _refuse(
                str(_("معامل المكوّن يجب أن يكون أكبر من صفر.")),
                "recipe_component_multiplier_not_positive",
                "components",
            )

    # Cycle and depth over the graph as it actually stands, from this version
    # down. An edge that was safe when it was written may not be now: somebody
    # may have closed the loop from the far end while this draft sat open.
    for component in components:
        path = cycle_path(
            graph,
            parent_recipe_id=version.recipe_id,
            parent_version_id=version.pk,
            child_version_id=component.component_version_id,
            child_recipe_id=component.component_recipe_id,
        )
        if path is not None:
            origin = f"{version.recipe.code} v{version.version_number}"
            raise _refuse(
                str(
                    _("النسخة تحتوي دورة: %(path)s") % {"path": " ← ".join([origin, *path, origin])}
                ),
                "recipe_component_cycle",
                "components",
            )

    depth, trail = depth_below(graph, version.pk)
    above, _above_trail = depth_above(graph, version.pk)
    if depth + above > MAX_COMPONENT_DEPTH:
        raise _refuse(
            str(
                _("عمق التداخل %(depth)s يتجاوز الحد %(limit)s: %(path)s")
                % {
                    "depth": depth + above,
                    "limit": MAX_COMPONENT_DEPTH,
                    "path": " ← ".join(trail),
                }
            ),
            "recipe_component_depth_exceeded",
            "components",
        )


# ---------------------------------------------------------------------------
# Effective coverage (RCP-074, RCP-075)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageGap:
    """
    One branch/date span a parent would claim and its child does not cover.

    Carries its own stable `code`. The alternative — deciding the code by
    matching on the Arabic reason text — would silently change which error a
    caller sees the first time somebody improved the wording.
    """

    child_label: str
    branch_code: str
    reason: str
    #: `recipe_component_branch_mismatch` when the child is effective nowhere at
    #: this branch; `recipe_component_not_effective` when it is, but not for the
    #: whole of the parent's range.
    code: str


def coverage_gaps(
    *,
    parent_version: RecipeVersion,
    branches: list[int],
    effective_from: datetime.date,
    effective_to: datetime.date | None = None,
) -> list[CoverageGap]:
    """
    Every branch on which a child is not effective at the parent's **start date**.

    This is the gate at *initial activation*, and it is deliberately narrow.
    For each applicable branch the child must:

    * belong to the same organization (checked when the edge is written);
    * have an eligible frozen status;
    * have an effective branch scope at that branch;
    * be effective **on the parent's `effective_from`**.

    **The child's range is not required to cover the parent's future.** That
    rule was tried and removed: it made an open-ended parent demand an
    open-ended child, which pinned the child forever and blocked ordinary
    supersession — and it confused two different questions.

    *Selecting* a version for a new, independent transaction is a date question,
    answered by `resolve_recipe_version`. The *validity of an already-frozen
    exact reference* is not a date question at all: `component_version` is an
    immutable foreign key to a specific frozen row, and it stays valid after
    that row is superseded for new selection. A blend superseded in September
    does not retroactively empty the July dish that named it — it is still
    there, still frozen, still expandable.

    So costing (Task 3.3) and production expansion (Task 3.4) must follow
    `RecipeComponent.component_version` **directly**, and must never re-resolve
    "the currently effective child" by date. Re-resolving would be exactly the
    silent re-pointing RCP-072 forbids, arriving through the back door.

    `effective_to` is accepted and ignored, kept in the signature so callers and
    the activation screen read the same way. Read-only: returns the gaps rather
    than raising, so the screen can list all of them at once.
    """
    gaps: list[CoverageGap] = []
    components = list(
        RecipeComponent.objects.filter(version=parent_version)
        .select_related("component_version", "component_recipe")
        .order_by("line_order")
    )
    if not components:
        return gaps

    scopes = list(
        RecipeVersionBranchScope.objects.filter(
            version__in=[component.component_version_id for component in components],
            branch_id__in=branches,
        ).select_related("branch")
    )
    by_version_and_branch: dict[tuple[int, int], RecipeVersionBranchScope] = {
        (scope.version_id, scope.branch_id): scope for scope in scopes
    }
    branch_codes = _branch_codes(branches)

    for component in components:
        child = component.component_version
        label = f"{component.component_recipe.code} v{child.version_number}"

        if child.status not in RESOLVABLE_VERSION_STATUSES:
            # APPROVED but never activated: agreed, and effective nowhere. A
            # parent may be *drafted* against it (see COMPONENT_ELIGIBLE_STATUSES)
            # but may not take effect on a date the child does not cover.
            for branch_id in branches:
                gaps.append(
                    CoverageGap(
                        child_label=label,
                        branch_code=branch_codes.get(branch_id, str(branch_id)),
                        reason=str(
                            _("النسخة الفرعية %(status)s ولم تُفعَّل على أي فرع.")
                            % {"status": child.get_status_display()}
                        ),
                        code="recipe_component_not_effective",
                    )
                )
            continue

        for branch_id in branches:
            branch_code = branch_codes.get(branch_id, str(branch_id))
            scope = by_version_and_branch.get((child.pk, branch_id))
            if scope is None:
                gaps.append(
                    CoverageGap(
                        child_label=label,
                        branch_code=branch_code,
                        reason=str(_("لا سريان للنسخة الفرعية على هذا الفرع.")),
                        code="recipe_component_branch_mismatch",
                    )
                )
                continue
            # Effective *on the parent's start date* — both ends of the child's
            # own range are tested against that one day, and against nothing
            # else. The parent's end date is not consulted.
            if scope.effective_from > effective_from:
                gaps.append(
                    CoverageGap(
                        child_label=label,
                        branch_code=branch_code,
                        reason=str(
                            _("سريان الفرعية يبدأ %(child)s بعد بداية الأصل %(parent)s.")
                            % {
                                "child": scope.effective_from.isoformat(),
                                "parent": effective_from.isoformat(),
                            }
                        ),
                        code="recipe_component_not_effective",
                    )
                )
            if scope.effective_to is not None and scope.effective_to < effective_from:
                gaps.append(
                    CoverageGap(
                        child_label=label,
                        branch_code=branch_code,
                        reason=str(
                            _("سريان الفرعية انتهى %(child)s قبل بداية الأصل %(parent)s.")
                            % {
                                "child": scope.effective_to.isoformat(),
                                "parent": effective_from.isoformat(),
                            }
                        ),
                        code="recipe_component_not_effective",
                    )
                )
    return gaps


def _branch_codes(branch_ids: list[int]) -> dict[int, str]:
    from apps.organizations.models import Branch

    return dict(Branch.objects.filter(pk__in=branch_ids).values_list("pk", "code"))


def require_effective_coverage(
    *,
    parent_version: RecipeVersion,
    branches: list[int],
    effective_from: datetime.date,
    effective_to: datetime.date | None = None,
) -> None:
    """`coverage_gaps`, as a refusal. Every gap is named, not just the first."""
    gaps = coverage_gaps(
        parent_version=parent_version,
        branches=branches,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    if not gaps:
        return
    raise ValidationError(
        [
            ValidationError(
                str(
                    _("%(child)s @ %(branch)s: %(reason)s")
                    % {
                        "child": gap.child_label,
                        "branch": gap.branch_code,
                        "reason": gap.reason,
                    }
                ),
                code=gap.code,
            )
            for gap in gaps
        ]
    )


# ---------------------------------------------------------------------------
# Downstream dependencies — advisory, never a refusal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dependency:
    """One active parent version that names a given child version."""

    parent_version: RecipeVersion
    branch_code: str
    parent_effective_from: datetime.date
    parent_effective_to: datetime.date | None


def dependents_of(child_version: RecipeVersion) -> list[Dependency]:
    """
    Every `ACTIVE` parent version that names this exact child, per branch scope.

    Ordered by recipe code then branch code so the refusal message, the screen
    and the verifier all list them the same way.
    """
    components = (
        RecipeComponent.objects.filter(component_version=child_version)
        .select_related("version", "version__recipe")
        .order_by("version__recipe__code", "version__version_number")
    )
    dependencies: list[Dependency] = []
    for component in components:
        parent = component.version
        if parent.status != RecipeVersionStatus.ACTIVE:
            continue
        for scope in parent.branch_scopes.select_related("branch").order_by("branch__code"):
            dependencies.append(
                Dependency(
                    parent_version=parent,
                    branch_code=scope.branch.code,
                    parent_effective_from=scope.effective_from,
                    parent_effective_to=scope.effective_to,
                )
            )
    return dependencies


def parents_outliving_child(
    *, child_version: RecipeVersion, close_at: datetime.date
) -> list[Dependency]:
    """
    The `ACTIVE` parents still effective after this child's range closes.

    **Advisory only. Nothing refuses on this.** An earlier version of this
    module blocked the supersession outright, which was wrong twice over: it
    pinned an open-ended child forever, and it treated an immutable exact
    reference as though it were a date-based lookup that could go stale.

    A parent's `component_version` is a frozen foreign key. Superseding the
    child ends that child's availability for *new, independent selection*; it
    does not reach backwards into a parent that already named it. The parent
    stays valid, its tree stays identical, and its expansion stays
    deterministic — which is the whole point of adopting an exact version.

    What this list is genuinely useful for is telling somebody, without
    stopping them: *"the marinade you just replaced is still what three active
    dishes contain — you may want new versions of those too."* That is a
    decision for a person, and the correction remains versioning rather than
    repointing (RCP-081): a new **parent** version adopts the new child.

    Nothing cascades. No parent is re-pointed and no parent is auto-superseded.
    """
    outliving: list[Dependency] = []
    for dependency in dependents_of(child_version):
        if dependency.parent_effective_to is None or dependency.parent_effective_to > close_at:
            outliving.append(dependency)
    return outliving


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------


@dataclass
class TreeNode:
    """
    One node of a version's component tree, with its cumulative scaling factor.

    `cumulative_multiplier` is the product of every multiplier from the root to
    here, at **full precision and never quantized on the way down** (RCP-073,
    ADR-006). Quantizing at each level would round a gram of saffron three times
    on the way to the leaf. It is a scaling identity for display and validation
    only: no quantity, no cost and no production line is derived from it here.
    """

    version: RecipeVersion
    recipe: Recipe
    depth: int
    line_order: int
    multiplier: Decimal
    cumulative_multiplier: Decimal
    note: str
    children: list[TreeNode] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.recipe.code} v{self.version.version_number}"

    @property
    def multiplier_display(self) -> str:
        """
        This edge's own factor, at the stored precision.

        Rendered here rather than by a template filter so it reads identically
        wherever it appears: `stringformat:"f"` would quietly print six places
        and make the tree disagree with the editor about the same number.
        """
        quantum = Decimal(1).scaleb(-FACTOR_PLACES)
        return f"{self.multiplier.quantize(quantum):f}"

    @property
    def cumulative_display(self) -> str:
        """
        The product from the root, locale-independent like every other factor.

        **Not** quantized: it is a product of stored factors and its exact scale
        is the arithmetic's, not a column's. Quantizing it here would be the
        rounding-on-the-way-down that RCP-073 forbids, one display at a time.
        """
        return f"{self.cumulative_multiplier.normalize():f}"


def component_tree(version: RecipeVersion) -> TreeNode:
    """
    The whole tree under one version, ordered by `line_order` at every level.

    Deterministic by construction: never queryset order, never primary-key
    order. A tree whose branches moved between two loads is one nobody can
    review.

    Bounded by `MAX_COMPONENT_DEPTH + 1` rather than trusting the graph to be
    acyclic. This is a *read* path — it runs on a screen, for data that may
    predate a constraint — and a read that hangs the request thread on a corrupt
    graph is a worse failure than one that stops early.
    """
    root = TreeNode(
        version=version,
        recipe=version.recipe,
        depth=0,
        line_order=0,
        multiplier=ONE,
        cumulative_multiplier=ONE,
        note="",
    )
    _expand(root, set())
    return root


def _expand(node: TreeNode, on_path: set[int]) -> None:
    if node.depth > MAX_COMPONENT_DEPTH or node.version.pk in on_path:
        return
    components = (
        RecipeComponent.objects.filter(version=node.version)
        .select_related("component_version", "component_recipe", "component_version__recipe")
        .order_by("line_order")
    )
    for component in components:
        child = TreeNode(
            version=component.component_version,
            recipe=component.component_recipe,
            depth=node.depth + 1,
            line_order=component.line_order,
            multiplier=component.multiplier,
            # Full precision. See the class docstring.
            cumulative_multiplier=node.cumulative_multiplier * component.multiplier,
            note=component.note,
        )
        node.children.append(child)
        _expand(child, on_path | {node.version.pk})


def flatten_tree(root: TreeNode) -> list[TreeNode]:
    """The tree as an indented list, parents before their children."""
    rows: list[TreeNode] = []

    def walk(node: TreeNode) -> None:
        for child in node.children:
            rows.append(child)
            walk(child)

    walk(root)
    return rows


def component_paths(version: RecipeVersion) -> list[str]:
    """
    Every root-to-leaf path under a version, as readable label chains.

    What the depth refusal and the verifier both quote, so a reader sees the
    same path shape wherever it appears.
    """
    root = component_tree(version)
    paths: list[str] = []

    def walk(node: TreeNode, trail: list[str]) -> None:
        here = [*trail, node.label]
        if not node.children:
            if len(here) > 1:
                paths.append(" ← ".join(here))
            return
        for child in node.children:
            walk(child, here)

    walk(root, [])
    return sorted(paths)
