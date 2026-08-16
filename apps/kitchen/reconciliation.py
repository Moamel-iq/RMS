"""
Check what the recipe version lifecycle claims against what it actually holds.

The constraints and triggers in migration `0005` make most of the findings
below unrepresentable. This module exists anyway, for the same reason
`verify_stock_projection` exists over an append-only ledger: *"cannot happen"*
and *"is not detected"* are different claims, and only one of them survives a
migration that was reverted on one machine, a constraint dropped during a
restore, or a future task that adds a write path and forgets a rule.

**Report only. There is no repair mode, and its absence is deliberate.**

A safe repair would need an organization maintenance lock, a guarantee that
nothing is approving concurrently, an explicit flag, a stated reason, an
identified actor, one transaction, audit evidence and a final verification
before commit. Any one of those missing turns "repair" into "overwrite the
evidence of a defect with a plausible number" — and after that runs, nobody can
tell what happened. An ambiguous effective range is not wear; it is a claim
that two different recipes were in force on one Tuesday, and somebody has to
decide which one the branch actually cooked.

Findings are planted only inside tests that roll back, and only by deferring
the exclusion constraint the migration deliberately made deferrable.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.db.models import Count, Q

from apps.kitchen.graph import (
    coverage_gaps,
    cycle_path,
    depth_above,
    depth_below,
    read_graph,
)
from apps.kitchen.lifecycle import covers_on_date, review_gaps
from apps.kitchen.models import (
    APPROVED_VERSION_STATUSES,
    COMPONENT_ELIGIBLE_STATUSES,
    DEMO_CODE_PREFIX,
    FROZEN_VERSION_STATUSES,
    MAX_COMPONENT_DEPTH,
    RESOLVABLE_VERSION_STATUSES,
    ApprovalEvidenceKind,
    Recipe,
    RecipeComponent,
    RecipeVersion,
    RecipeVersionBranchScope,
    RecipeVersionStatus,
)
from apps.organizations.models import Organization


@dataclass(frozen=True)
class Finding:
    """One thing that is wrong, named precisely enough to act on."""

    #: A stable machine key. Reports group on this; the message is for people.
    code: str
    organization_code: str
    recipe_code: str
    version: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.organization_code}/{self.recipe_code} {self.version}: {self.message}"


def _finding(code: str, version: RecipeVersion, message: str) -> Finding:
    return Finding(
        code=code,
        organization_code=version.recipe.organization.code,
        recipe_code=version.recipe.code,
        version=f"v{version.version_number}",
        message=message,
    )


def verify_organization(organization: Organization) -> list[Finding]:
    """Every check, over one organization's recipes, in a stable order."""
    versions = list(
        RecipeVersion.objects.filter(recipe__organization=organization)
        .select_related("recipe", "recipe__organization", "superseded_by_version")
        .prefetch_related(
            "reviews",
            "branch_scopes__branch",
            "servings",
            "components__component_recipe",
            "components__component_version",
        )
        .order_by("recipe__code", "version_number")
    )
    findings: list[Finding] = []
    for version in versions:
        findings.extend(_check_version(version))
    findings.extend(_check_overlaps(organization))
    findings.extend(_check_ambiguous_resolution(organization))
    findings.extend(_check_component_graph(organization))
    findings.extend(_check_component_coverage(organization))
    return findings


def _check_version(version: RecipeVersion) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_check_evidence(version))
    findings.extend(_check_metadata(version))
    findings.extend(_check_scope_agreement(version))
    findings.extend(_check_supersession(version))
    findings.extend(_check_structure(version))
    findings.extend(_check_components(version))
    return findings


def _check_evidence(version: RecipeVersion) -> list[Finding]:
    """An active version with no approval behind it is the finding that matters."""
    if version.status not in APPROVED_VERSION_STATUSES:
        return []
    findings: list[Finding] = []
    if not version.approved_by_id or not version.approved_at:
        findings.append(
            _finding("approval_without_actor", version, "approved with no approver or moment")
        )
    if not version.approval_reference or not version.approval_evidence_kind:
        findings.append(
            _finding("approval_without_evidence", version, "approved with no evidence reference")
        )
    if version.created_by_id and version.approved_by_id == version.created_by_id:
        findings.append(
            _finding("approval_by_the_author", version, "the author approved their own version")
        )
    gaps = review_gaps(version)
    if gaps:
        findings.append(
            _finding("approval_missing_reviews", version, "; ".join(gaps)),
        )
    is_demo = version.recipe.code.startswith(DEMO_CODE_PREFIX)
    if version.approval_evidence_kind == ApprovalEvidenceKind.DEMO_FICTIONAL and not is_demo:
        findings.append(
            _finding(
                "real_recipe_on_demo_evidence",
                version,
                "a real recipe carries fictional approval evidence",
            )
        )
    if version.approval_evidence_kind == ApprovalEvidenceKind.SIGNED_FORM and is_demo:
        findings.append(
            _finding(
                "demo_recipe_claims_a_signature",
                version,
                "a demo recipe claims a signed approval form",
            )
        )
    return findings


def _check_metadata(version: RecipeVersion) -> list[Finding]:
    """Incomplete lifecycle metadata on a version that has left DRAFT."""
    findings: list[Finding] = []
    if version.status == RecipeVersionStatus.SUBMITTED and not (
        version.submitted_by_id and version.submitted_at
    ):
        findings.append(
            _finding("submission_without_actor", version, "submitted with no submitter or moment")
        )
    if version.status == RecipeVersionStatus.REJECTED and not (
        version.rejected_by_id and version.rejected_at and version.rejection_reason
    ):
        findings.append(
            _finding("rejection_without_reason", version, "rejected with no actor or reason")
        )
    if version.status in RESOLVABLE_VERSION_STATUSES:
        if version.effective_from is None:
            findings.append(
                _finding("effective_without_start", version, "in effect with no start date")
            )
        if not version.branch_scopes.exists():
            findings.append(_finding("effective_without_scope", version, "in effect at no branch"))
        if version.effective_to is not None and version.effective_from is not None:
            if version.effective_to < version.effective_from:
                findings.append(
                    _finding("inverted_range", version, "the range ends before it begins")
                )
    return findings


def _check_scope_agreement(version: RecipeVersion) -> list[Finding]:
    """A scope row that has drifted from the version whose range it inherits."""
    findings: list[Finding] = []
    for scope in version.branch_scopes.all():
        if scope.recipe_id != version.recipe_id:
            findings.append(
                _finding(
                    "scope_names_another_recipe",
                    version,
                    f"scope at {scope.branch.code} names another recipe",
                )
            )
        if scope.branch.organization_id != version.recipe.organization_id:
            findings.append(
                _finding(
                    "scope_names_a_foreign_branch",
                    version,
                    f"scope names branch {scope.branch.code} of another organization",
                )
            )
        if (
            scope.effective_from != version.effective_from
            or scope.effective_to != version.effective_to
        ):
            findings.append(
                _finding(
                    "scope_range_disagrees",
                    version,
                    f"scope at {scope.branch.code} claims a different range from its version",
                )
            )
    return findings


def _check_supersession(version: RecipeVersion) -> list[Finding]:
    """Broken links in the replacement chain."""
    findings: list[Finding] = []
    replacement = version.superseded_by_version
    if version.status == RecipeVersionStatus.SUPERSEDED:
        if replacement is None:
            findings.append(
                _finding("supersession_without_replacement", version, "superseded by nothing")
            )
        if version.effective_to is None:
            findings.append(
                _finding("supersession_left_open", version, "superseded with an open range")
            )
    if replacement is None:
        return findings
    if replacement.pk == version.pk:
        findings.append(_finding("supersession_loop", version, "superseded by itself"))
    if replacement.recipe_id != version.recipe_id:
        findings.append(
            _finding(
                "supersession_across_recipes",
                version,
                "superseded by a version of another recipe",
            )
        )
    if (
        replacement.effective_from is not None
        and version.effective_to is not None
        and version.effective_to >= replacement.effective_from
    ):
        findings.append(
            _finding(
                "supersession_seam_overlaps",
                version,
                "the closed range still reaches into its replacement's",
            )
        )
    return findings


def _check_structure(version: RecipeVersion) -> list[Finding]:
    """
    Structure that a frozen version should not be able to hold.

    Every one of these is refused at the database today. They are checked
    because a restore that lost a trigger looks exactly like a database that
    never had one, and this is the only thing that would notice.
    """
    findings: list[Finding] = []
    primaries = [serving for serving in version.servings.all() if serving.is_primary]
    if len(primaries) > 1:
        findings.append(
            _finding("multiple_primary_servings", version, f"{len(primaries)} primary servings")
        )
    if version.status in FROZEN_VERSION_STATUSES and not version.servings.exists():
        findings.append(_finding("frozen_without_servings", version, "frozen with no serving"))
    if version.status in FROZEN_VERSION_STATUSES and not version.lines.exists():
        findings.append(_finding("frozen_without_lines", version, "frozen with no ingredient"))

    broken = _broken_provenance(version)
    if broken:
        findings.append(
            _finding("broken_provenance", version, f"{broken} rows carry half a provenance")
        )
    return findings


def _broken_provenance(version: RecipeVersion) -> int:
    """Rows with a document and no page, or a page and no document."""
    half_set = Q(source_document="", source_page__isnull=False) | (
        ~Q(source_document="") & Q(source_page__isnull=True)
    )
    return (
        version.lines.filter(half_set).count()
        + version.steps.filter(half_set).count()
        + version.servings.filter(half_set).count()
    )


def _check_overlaps(organization: Organization) -> list[Finding]:
    """
    Two effective claims on one (recipe, branch) whose ranges intersect.

    Computed in Python over the scope rows rather than asked of the database,
    because the point is to catch the case where the database's own constraint
    is no longer there.
    """
    findings: list[Finding] = []
    scopes = list(
        RecipeVersionBranchScope.objects.filter(recipe__organization=organization)
        .select_related("version", "recipe", "recipe__organization", "branch")
        .order_by("recipe__code", "branch__code", "effective_from", "version__version_number")
    )
    by_pair: dict[tuple[int, int], list[RecipeVersionBranchScope]] = {}
    for scope in scopes:
        by_pair.setdefault((scope.recipe_id, scope.branch_id), []).append(scope)

    for rows in by_pair.values():
        for index, earlier in enumerate(rows):
            for later in rows[index + 1 :]:
                if _ranges_intersect(earlier, later):
                    findings.append(
                        Finding(
                            code="overlapping_effective_ranges",
                            organization_code=earlier.recipe.organization.code,
                            recipe_code=earlier.recipe.code,
                            version=f"v{earlier.version.version_number}/"
                            f"v{later.version.version_number}",
                            message=(
                                f"both claim branch {earlier.branch.code} over intersecting dates"
                            ),
                        )
                    )
    return findings


def _ranges_intersect(left: RecipeVersionBranchScope, right: RecipeVersionBranchScope) -> bool:
    """Inclusive at both ends; a null upper bound is unbounded."""
    left_end = left.effective_to or datetime.date.max
    right_end = right.effective_to or datetime.date.max
    return left.effective_from <= right_end and right.effective_from <= left_end


def _check_ambiguous_resolution(organization: Organization) -> list[Finding]:
    """
    Any (recipe, branch, date) that resolves to more than one version.

    Every distinct start and end-plus-one-day is a candidate date: a range
    intersection can only begin or end at one of those, so checking them proves
    the whole timeline without walking every day since the branch opened.
    """
    findings: list[Finding] = []
    scopes = list(
        RecipeVersionBranchScope.objects.filter(
            recipe__organization=organization,
            version__status__in=sorted(RESOLVABLE_VERSION_STATUSES),
        ).select_related("recipe", "recipe__organization", "branch")
    )
    candidates: dict[tuple[int, int], set[datetime.date]] = {}
    for scope in scopes:
        dates = candidates.setdefault((scope.recipe_id, scope.branch_id), set())
        dates.add(scope.effective_from)
        if scope.effective_to is not None:
            dates.add(scope.effective_to)

    lookup = {(scope.recipe_id, scope.branch_id): scope for scope in scopes}
    for (recipe_id, branch_id), dates in sorted(candidates.items()):
        sample = lookup[(recipe_id, branch_id)]
        for on_date in sorted(dates):
            matching = (
                RecipeVersionBranchScope.objects.filter(
                    recipe_id=recipe_id,
                    branch_id=branch_id,
                    effective_from__lte=on_date,
                    version__status__in=sorted(RESOLVABLE_VERSION_STATUSES),
                )
                .filter(covers_on_date(on_date))
                .aggregate(found=Count("id"))["found"]
            )
            if matching > 1:
                findings.append(
                    Finding(
                        code="ambiguous_resolution",
                        organization_code=sample.recipe.organization.code,
                        recipe_code=sample.recipe.code,
                        version="—",
                        message=(
                            f"{matching} versions resolve at branch {sample.branch.code} "
                            f"on {on_date.isoformat()}"
                        ),
                    )
                )
    return findings


def recipes_checked(organization: Organization) -> int:
    """How many recipes the run looked at — an empty clean result is not a pass."""
    return Recipe.objects.filter(organization=organization).count()


# ---------------------------------------------------------------------------
# The nested-recipe graph (Task 3.2B)
# ---------------------------------------------------------------------------
#
# Extends the existing command rather than adding a second one. Two verifiers
# checking overlapping contracts differently is how a repository ends up unable
# to say which of them is right.


def _check_components(version: RecipeVersion) -> list[Finding]:
    """Component rows a frozen graph should not be able to hold."""
    return component_findings(
        version,
        list(
            version.components.select_related(
                "component_version", "component_version__recipe", "component_recipe"
            ).order_by("line_order")
        ),
    )


def component_findings(version: RecipeVersion, components: list[RecipeComponent]) -> list[Finding]:
    """
    The checks themselves, over rows the caller supplies.

    Separated from the read so the tests can hand it **in-memory mutated
    copies**. Every finding below is unrepresentable at the database, so the
    only honest way to prove the check works is to call it on an object that was
    never written — not to disable a trigger, and not to drop a constraint.
    """
    findings: list[Finding] = []
    if not components:
        return findings

    seen_orders: set[int] = set()
    seen_children: set[int] = set()
    for component in components:
        child = component.component_version
        label = f"{component.component_recipe.code} v{child.version_number}"

        if component.recipe_id != version.recipe_id:
            findings.append(
                _finding(
                    "component_names_another_recipe",
                    version,
                    f"{label} is filed under a different recipe from its version",
                )
            )
        if component.component_recipe_id != child.recipe_id:
            findings.append(
                _finding(
                    "component_child_recipe_disagrees",
                    version,
                    f"{label} names a recipe its child version does not belong to",
                )
            )
        if child.recipe.organization_id != version.recipe.organization_id:
            findings.append(
                _finding("component_crosses_organizations", version, f"{label} is another org's")
            )
        if component.component_recipe.output_item_id is not None:
            findings.append(
                _finding(
                    "stocked_recipe_used_as_component",
                    version,
                    f"{label} produces a stocked item and must be a line, not a component",
                )
            )
        if child.status not in COMPONENT_ELIGIBLE_STATUSES:
            findings.append(
                _finding(
                    "component_child_not_eligible",
                    version,
                    f"{label} is {child.status} and is not an approved child",
                )
            )
        if component.multiplier <= 0:
            findings.append(
                _finding("component_multiplier_not_positive", version, f"{label} scales by <= 0")
            )
        if component.line_order <= 0 or component.line_order in seen_orders:
            findings.append(
                _finding(
                    "component_order_invalid",
                    version,
                    f"{label} has a duplicate or non-positive order",
                )
            )
        seen_orders.add(component.line_order)
        if component.component_recipe_id in seen_children:
            findings.append(
                _finding("component_child_recipe_repeated", version, f"{label} appears twice")
            )
        seen_children.add(component.component_recipe_id)
        if bool(component.source_document) != (component.source_page is not None):
            findings.append(
                _finding("component_broken_provenance", version, f"{label} carries half a source")
            )
    return findings


def _check_component_graph(organization: Organization) -> list[Finding]:
    """
    Cycles and excessive depth, over the whole persisted graph.

    Neither is expressible as a row constraint, so unlike most of this module
    these are the *only* thing standing between a corrupt graph and a costing
    run that never terminates. The path is reported, not just the fact.
    """
    findings: list[Finding] = []
    graph = read_graph(organization.pk)
    versions = list(
        RecipeVersion.objects.filter(recipe__organization=organization, components__isnull=False)
        .select_related("recipe", "recipe__organization")
        .distinct()
        .order_by("recipe__code", "version_number")
    )
    for version in versions:
        for edge in graph.edges_below(version.pk):
            path = cycle_path(
                graph,
                parent_recipe_id=version.recipe_id,
                parent_version_id=version.pk,
                child_version_id=edge.child_version_id,
                child_recipe_id=edge.child_recipe_id,
            )
            if path is not None:
                origin = f"{version.recipe.code} v{version.version_number}"
                findings.append(
                    _finding(
                        "component_cycle",
                        version,
                        "cycle: " + " -> ".join([origin, *path, origin]),
                    )
                )
        depth, trail = depth_below(graph, version.pk)
        above, _ = depth_above(graph, version.pk)
        if depth + above > MAX_COMPONENT_DEPTH:
            findings.append(
                _finding(
                    "component_depth_exceeded",
                    version,
                    f"nesting depth {depth + above} exceeds {MAX_COMPONENT_DEPTH}: "
                    + " -> ".join(trail),
                )
            )
    return findings


def _check_component_coverage(organization: Organization) -> list[Finding]:
    """
    An ACTIVE parent whose child is not effective across everything it claims.

    The state RCP-074 refuses at activation, checked again from the other end:
    a child whose range was shortened afterwards, or a branch scope that arrived
    later, would both land here.
    """
    findings: list[Finding] = []
    parents = list(
        RecipeVersion.objects.filter(
            recipe__organization=organization,
            status=RecipeVersionStatus.ACTIVE,
            components__isnull=False,
        )
        .select_related("recipe", "recipe__organization")
        .distinct()
        .order_by("recipe__code", "version_number")
    )
    for parent in parents:
        scopes = list(parent.branch_scopes.select_related("branch"))
        if not scopes:
            continue
        gaps = coverage_gaps(
            parent_version=parent,
            branches=[scope.branch_id for scope in scopes],
            effective_from=min(scope.effective_from for scope in scopes),
            effective_to=(
                None
                if any(scope.effective_to is None for scope in scopes)
                else max(scope.effective_to for scope in scopes if scope.effective_to)
            ),
        )
        for gap in gaps:
            findings.append(
                _finding(
                    "component_child_not_covering_parent",
                    parent,
                    f"{gap.child_label} @ {gap.branch_code}: {gap.reason}",
                )
            )
    return findings
