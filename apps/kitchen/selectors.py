"""
Scoped reads for the kitchen module.

Every queryset a screen, an API endpoint or a service uses starts here, so an
organization's recipes are invisible to anyone outside it — never filtered in a
template, and never fetched first and checked afterwards.

The resolvers take an id **and** the caller together (ADR-016). A submitted id
can therefore only ever select from what the caller already reaches; it can
never add to it, and there is no moment where an out-of-scope row exists in a
local variable.
"""

from __future__ import annotations

from django.db.models import Model, Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.inventory.models import Warehouse
from apps.kitchen.models import (
    COMPONENT_ELIGIBLE_STATUSES,
    OPEN_VERSION_STATUSES,
    BatchDocumentLink,
    MealRecord,
    ProductionBatch,
    ProductionBatchActualLine,
    ProductionBatchAllocation,
    ProductionBatchLine,
    Recipe,
    RecipeCategory,
    RecipeComponent,
    RecipeCostSnapshot,
    RecipeLine,
    RecipeLineSubstitute,
    RecipeServing,
    RecipeStep,
    RecipeStepIngredient,
    RecipeVersion,
    RecipeVersionBranchScope,
    RecipeVersionReview,
    RecipeVersionStatus,
)
from apps.kitchen.permissions import (
    CREATE_PRODUCTION_BATCH,
    LINK_BATCH_DOCUMENT,
    MANAGE_RECIPE,
    POST_PRODUCTION_BATCH,
    RECORD_MEAL,
    REVERSE_PRODUCTION_BATCH,
    VIEW_KITCHEN_REPORT,
    VIEW_PRODUCTION,
    VIEW_RECIPE_COST,
)
from apps.organizations.authorization import (
    OutOfScope,
    branches_with_permission,
    organization_scope,
    organizations_with_permission,
)
from apps.organizations.models import Branch, Organization
from apps.organizations.selectors import accessible_branches
from apps.users.models import User


def reachable_organization_ids(user: User) -> list[int]:
    """
    Organizations this caller reaches — through organization authority, or
    through a branch of one.

    Recipes are organization master data, so *reaching* the organization is the
    read boundary. Whether the caller may **change** anything is a separate
    question answered by `manageable_organizations`.
    """
    reachable = set(organization_scope(user))
    reachable.update(accessible_branches(user).values_list("organization_id", flat=True))
    return sorted(reachable)


def manageable_organizations(user: User) -> QuerySet[Organization]:
    """Organizations where a post the caller holds carries `manage_recipe`."""
    return organizations_with_permission(user, MANAGE_RECIPE)


def visible_categories(user: User) -> QuerySet[RecipeCategory]:
    """Recipe categories in organizations this caller reaches."""
    return RecipeCategory.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization")


def visible_recipes(user: User) -> QuerySet[Recipe]:
    """Recipes in organizations this caller reaches."""
    return Recipe.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization", "category", "output_item")


def visible_versions(user: User) -> QuerySet[RecipeVersion]:
    """Versions of recipes this caller reaches."""
    return RecipeVersion.objects.filter(
        recipe__organization_id__in=reachable_organization_ids(user)
    ).select_related("recipe", "recipe__organization", "output_unit")


def visible_lines(user: User) -> QuerySet[RecipeLine]:
    """Lines of versions this caller reaches."""
    return RecipeLine.objects.filter(
        version__recipe__organization_id__in=reachable_organization_ids(user)
    ).select_related("version", "version__recipe", "item", "entered_unit", "package_unit")


def visible_substitutes(user: User) -> QuerySet[RecipeLineSubstitute]:
    """Substitutes of lines this caller reaches."""
    return RecipeLineSubstitute.objects.filter(
        line__version__recipe__organization_id__in=reachable_organization_ids(user)
    ).select_related("line", "line__item", "line__version", "substitute_item")


def visible_steps(user: User) -> QuerySet[RecipeStep]:
    """Steps of versions this caller reaches."""
    return RecipeStep.objects.filter(
        version__recipe__organization_id__in=reachable_organization_ids(user)
    ).select_related("version", "version__recipe")


def visible_step_ingredients(user: User) -> QuerySet[RecipeStepIngredient]:
    """Step-to-line links this caller reaches."""
    return RecipeStepIngredient.objects.filter(
        step__version__recipe__organization_id__in=reachable_organization_ids(user)
    ).select_related("step", "recipe_line", "recipe_line__item")


def visible_servings(user: User) -> QuerySet[RecipeServing]:
    """Servings of versions this caller reaches."""
    return RecipeServing.objects.filter(
        version__recipe__organization_id__in=reachable_organization_ids(user)
    ).select_related("version", "version__recipe", "serving_unit")


def _resolve[RowT: Model](queryset: QuerySet[RowT], pk: int, label: str) -> RowT:
    """
    One row from an already-scoped queryset, or `OutOfScope`.

    The message never says *why* the row is unreachable. A missing record and
    somebody else's record must be indistinguishable, or the 404 only fixed the
    status code.
    """
    row = queryset.filter(pk=pk).first()
    if row is None:
        raise OutOfScope(_("%(label)s %(id)s does not exist.") % {"label": label, "id": pk})
    return row


def resolve_category(user: User, category_id: int) -> RecipeCategory:
    """Turn a submitted category id into one the caller reaches."""
    return _resolve(visible_categories(user), category_id, "RecipeCategory")


def resolve_recipe(user: User, recipe_id: int) -> Recipe:
    """Turn a submitted recipe id into one the caller reaches."""
    return _resolve(visible_recipes(user), recipe_id, "Recipe")


def resolve_version(user: User, version_id: int) -> RecipeVersion:
    """Turn a submitted version id into one the caller reaches."""
    return _resolve(visible_versions(user), version_id, "RecipeVersion")


def resolve_line(user: User, line_id: int) -> RecipeLine:
    """Turn a submitted line id into one the caller reaches."""
    return _resolve(visible_lines(user), line_id, "RecipeLine")


def resolve_substitute(user: User, substitute_id: int) -> RecipeLineSubstitute:
    """Turn a submitted substitute id into one the caller reaches."""
    return _resolve(visible_substitutes(user), substitute_id, "RecipeLineSubstitute")


def resolve_step(user: User, step_id: int) -> RecipeStep:
    """Turn a submitted step id into one the caller reaches."""
    return _resolve(visible_steps(user), step_id, "RecipeStep")


def resolve_serving(user: User, serving_id: int) -> RecipeServing:
    """Turn a submitted serving id into one the caller reaches."""
    return _resolve(visible_servings(user), serving_id, "RecipeServing")


def visible_reviews(user: User) -> QuerySet[RecipeVersionReview]:
    """Review signoffs on versions this caller reaches."""
    return RecipeVersionReview.objects.filter(
        version__recipe__organization_id__in=reachable_organization_ids(user)
    ).select_related("version", "version__recipe", "reviewer")


def visible_scopes(user: User) -> QuerySet[RecipeVersionBranchScope]:
    """Effective branch scope rows this caller reaches."""
    return RecipeVersionBranchScope.objects.filter(
        recipe__organization_id__in=reachable_organization_ids(user)
    ).select_related("version", "recipe", "branch")


def resolve_scope(user: User, scope_id: int) -> RecipeVersionBranchScope:
    """Turn a submitted scope id into one the caller reaches."""
    return _resolve(visible_scopes(user), scope_id, "RecipeVersionBranchScope")


def draft_version_for(recipe: Recipe) -> RecipeVersion | None:
    """
    This recipe's open draft, if it has one.

    Exactly one may exist, held by a partial unique index. An `ACTIVE` version
    and a new `DRAFT` coexist happily — that is the relaxation Task 3.1
    promised and Task 3.2A delivered by widening the index to cover `SUBMITTED`
    as well rather than by loosening it.
    """
    return recipe.versions.filter(status=RecipeVersionStatus.DRAFT).first()


def open_version_for(recipe: Recipe) -> RecipeVersion | None:
    """
    This recipe's version in flight — a draft, or one under review.

    The screen's question. `draft_version_for` answers "what may I edit"; this
    answers "what is happening", and they differ for exactly as long as a
    version sits in review.
    """
    return recipe.versions.filter(status__in=sorted(OPEN_VERSION_STATUSES)).first()


def versions_of(recipe: Recipe) -> QuerySet[RecipeVersion]:
    """Every version of one recipe, newest first — the history panel's read."""
    return (
        recipe.versions.select_related("output_unit", "approved_by", "superseded_by_version")
        .prefetch_related("reviews__reviewer", "branch_scopes__branch")
        .order_by("-version_number")
    )


# ---------------------------------------------------------------------------
# Nested components
# ---------------------------------------------------------------------------


def visible_components(user: User) -> QuerySet[RecipeComponent]:
    """Component links on versions this caller reaches."""
    return RecipeComponent.objects.filter(
        recipe__organization_id__in=reachable_organization_ids(user)
    ).select_related(
        "version",
        "version__recipe",
        "recipe",
        "component_version",
        "component_version__recipe",
        "component_recipe",
    )


def resolve_component(user: User, component_id: int) -> RecipeComponent:
    """Turn a submitted component id into one the caller reaches."""
    return _resolve(visible_components(user), component_id, "RecipeComponent")


def components_for_version(version: RecipeVersion) -> QuerySet[RecipeComponent]:
    """
    One version's own components, in `line_order`.

    Explicit order, never queryset order: the component list is read beside the
    ingredient list, and a list whose rows moved between two loads is one nobody
    can review against a paper card.
    """
    return version.components.select_related(
        "component_version",
        "component_version__recipe",
        "component_recipe",
    ).order_by("line_order")


def component_candidates(user: User, version: RecipeVersion) -> QuerySet[RecipeVersion]:
    """
    The child versions a screen may offer for this parent draft.

    Filters what the server would refuse anyway, so the operator is not invited
    to make a choice that cannot work:

    * the caller's own organization only;
    * never the parent's own recipe (`A → A` at recipe identity);
    * never a recipe with an `output_item` — that is the **stocked** shape and
      belongs on a `RecipeLine`, which is RCP-070's whole point;
    * never an archived recipe;
    * only frozen, approved child versions;
    * never a recipe this parent already names.

    **This is a convenience, never an enforcement.** Every one of these rules is
    re-checked by `validate_component_edge` under the graph lock, against the
    persisted graph. A candidate list is a suggestion a caller may ignore by
    posting an id directly, and the tests post directly for exactly that reason.

    Cycles deeper than one hop are deliberately *not* filtered here: deciding
    them needs the whole graph walked per candidate, and a list that quietly
    omitted a recipe would leave the operator wondering where it went. The
    refusal names the path instead, which is more use than an absence.
    """
    already_named = version.components.values_list("component_recipe_id", flat=True)
    return (
        RecipeVersion.objects.filter(
            recipe__organization_id=version.recipe.organization_id,
            recipe__output_item__isnull=True,
            recipe__is_active=True,
            status__in=sorted(COMPONENT_ELIGIBLE_STATUSES),
        )
        .exclude(recipe_id=version.recipe_id)
        .exclude(recipe_id__in=already_named)
        .filter(recipe__organization_id__in=reachable_organization_ids(user))
        .select_related("recipe")
        .order_by("recipe__code", "-version_number")
    )


def component_dependencies(user: User, version: RecipeVersion) -> QuerySet[RecipeComponent]:
    """
    Which parent versions use this version as a component.

    The panel that answers *"may I supersede this blend?"* before somebody tries
    and is refused. Ordered by parent recipe then version so the screen, the
    refusal message and the verifier all list them identically.
    """
    return (
        visible_components(user)
        .filter(component_version=version)
        .order_by("version__recipe__code", "version__version_number")
    )


# ---------------------------------------------------------------------------
# Task 3.3 - cost snapshots
# ---------------------------------------------------------------------------


def cost_readable_organization_ids(user: User) -> list[int]:
    """
    Organizations whose **money** this caller may read.

    Narrower than `reachable_organization_ids` on purpose, and the narrowing is
    the whole control: a cook reaches the organization and reads every recipe
    card in it, and reads no cost anywhere. `view_recipe_cost` is
    organization-scoped master-data authority (ADR-016), so this asks the same
    question `visible_recipes` asks and then asks for the second permission too.

    Used by every cost read. A screen or endpoint that filtered on
    `reachable_organization_ids` and then checked the permission separately
    would be one refactor away from checking neither.
    """
    return sorted(
        organizations_with_permission(user, VIEW_RECIPE_COST).values_list("pk", flat=True)
    )


def visible_cost_snapshots(user: User) -> QuerySet[RecipeCostSnapshot]:
    """
    Every cost snapshot this caller may read.

    Scoped by `view_recipe_cost` rather than by `view_recipe`, so a storekeeper
    who legitimately reads the recipe list sees **no** snapshot at all - not an
    empty-costed one. Out of scope is 404 through `_resolve`, never 403: a 403
    about another organization's snapshot would confirm the snapshot exists,
    and ids are sequential.
    """
    return RecipeCostSnapshot.objects.filter(
        organization_id__in=cost_readable_organization_ids(user)
    ).select_related(
        "organization",
        "recipe",
        "version",
        "branch",
        "warehouse",
        "created_by",
    )


def resolve_cost_snapshot(user: User, snapshot_id: int) -> RecipeCostSnapshot:
    """One snapshot, resolved **with** the caller. Out of scope is 404."""
    return _resolve(visible_cost_snapshots(user), snapshot_id, "Cost snapshot")


def snapshots_for_version(version: RecipeVersion) -> QuerySet[RecipeCostSnapshot]:
    """
    Every snapshot taken of one exact version, newest first.

    Unscoped by design - the caller has already resolved the version through
    `visible_versions`, and a second scope filter here would silently hide rows
    on a screen that had already proved its right to them.
    """
    return (
        RecipeCostSnapshot.objects.filter(version=version)
        .select_related("warehouse", "branch", "created_by")
        .order_by("-created_at", "-id")
    )


# ---------------------------------------------------------------------------
# Task 3.4 - production drafts
# ---------------------------------------------------------------------------
#
# Production is the first kitchen concern scoped to a **warehouse** rather than
# to the organization. Every read below therefore starts from
# `accessible_warehouses`, which is inventory's own custody answer, narrowed by
# the production permission - not from `reachable_organization_ids`, which
# would hand somebody who can read the menu the branch's production plan.


def _warehouses_with_permission(user: User, permission: str) -> QuerySet[Warehouse]:
    """
    Warehouses where a post the caller actually holds carries this permission.

    The bulk form of `has_warehouse_permission`, which `apps.organizations`
    offers only per warehouse. Written here rather than there because Task 3.4
    is not authorized to modify that module, and asking the single-object
    question once per row would be a query per warehouse on every list screen.

    It answers **identically**, and the way it stays identical is by mirroring
    `roles_at_warehouse` clause for clause: an `ALL` branch membership covers
    every warehouse in its branch, a `SELECTED` one covers only the warehouses
    it lists, and organization-wide authority covers the whole organization. A
    `SELECTED` membership that does not list a warehouse contributes no role
    there, so narrowing custody narrows authority with it. A test holds the two
    against each other rather than trusting this comment.
    """
    from apps.organizations.authorization import accessible_warehouses, roles_granting
    from apps.organizations.models import WarehouseScopeMode

    if not user.is_authenticated or not user.is_active:
        return Warehouse.objects.none()

    reachable = accessible_warehouses(user)
    if user.is_superuser:
        return reachable

    roles = roles_granting(permission)
    if not roles:
        return Warehouse.objects.none()

    return reachable.filter(
        # A branch membership covering the whole branch.
        Q(
            branch__memberships__user=user,
            branch__memberships__is_active=True,
            branch__memberships__role__in=roles,
            branch__memberships__warehouse_scope_mode=WarehouseScopeMode.ALL,
        )
        # A branch membership restricted to specific warehouses, this one among
        # them. Matched through the scope row so a membership that lists other
        # warehouses contributes nothing here.
        | Q(
            membership_scopes__branch_membership__user=user,
            membership_scopes__branch_membership__is_active=True,
            membership_scopes__branch_membership__role__in=roles,
            membership_scopes__branch_membership__warehouse_scope_mode=(
                WarehouseScopeMode.SELECTED
            ),
        )
        # Organization-wide authority reaches every warehouse it owns.
        | Q(
            branch__organization__memberships__user=user,
            branch__organization__memberships__is_active=True,
            branch__organization__memberships__role__in=roles,
        )
    ).distinct()


def readable_production_warehouses(user: User) -> QuerySet[Warehouse]:
    """
    Warehouses whose production this caller may read.

    Two questions and both must answer yes: can the caller reach this warehouse
    at all (`accessible_warehouses`, which respects `ALL` versus `SELECTED`
    warehouse scope), and does a post they actually hold there carry
    `view_production`. A permission is never a reach and a reach is never a
    permission (ADR-016).
    """
    return _warehouses_with_permission(user, VIEW_PRODUCTION)


def draftable_production_warehouses(user: User) -> QuerySet[Warehouse]:
    """Warehouses this caller may draft **into**. The create side of the pair."""
    return _warehouses_with_permission(user, CREATE_PRODUCTION_BATCH)


def postable_production_warehouses(user: User) -> QuerySet[Warehouse]:
    """
    Warehouses whose production this caller may **commit** to the ledger.

    A separate question from drafting, and it must stay separate: drafting
    consumes nothing and a wrong draft is discarded, while posting moves stock
    and writes a journal. Same machinery, different permission.
    """
    return _warehouses_with_permission(user, POST_PRODUCTION_BATCH)


def reversible_production_warehouses(user: User) -> QuerySet[Warehouse]:
    """Warehouses whose posted production this caller may reverse. Elevated."""
    return _warehouses_with_permission(user, REVERSE_PRODUCTION_BATCH)


def visible_production_batches(user: User) -> QuerySet[ProductionBatch]:
    """
    Every production batch this caller may read, newest planned date first.

    Out of scope is 404 through `_resolve`, never 403: a 403 about another
    branch's batch would confirm the batch exists, and ids are sequential.
    """
    return (
        ProductionBatch.objects.filter(warehouse__in=readable_production_warehouses(user))
        .select_related(
            "organization",
            "branch",
            "warehouse",
            "recipe",
            "recipe_version",
            "recipe_version__output_unit",
            "created_by",
        )
        .order_by("-planned_business_date", "-id")
    )


def resolve_production_batch(user: User, batch_id: int) -> ProductionBatch:
    """One batch, resolved **with** the caller. A submitted id never widens scope."""
    return _resolve(visible_production_batches(user), batch_id, "Production batch")


def visible_production_lines(user: User) -> QuerySet[ProductionBatchLine]:
    """Requirement rows under batches this caller may read."""
    return ProductionBatchLine.objects.filter(
        batch__warehouse__in=readable_production_warehouses(user)
    ).select_related("batch", "item", "item__base_unit", "source_version", "source_line")


def resolve_production_line(user: User, line_id: int) -> ProductionBatchLine:
    return _resolve(visible_production_lines(user), line_id, "Production requirement")


def visible_production_actuals(user: User) -> QuerySet[ProductionBatchActualLine]:
    """Actual-consumption rows under batches this caller may read."""
    return ProductionBatchActualLine.objects.filter(
        line__batch__warehouse__in=readable_production_warehouses(user)
    ).select_related("line", "line__batch", "line__item", "item", "item__base_unit", "substitute")


def resolve_production_actual(user: User, actual_id: int) -> ProductionBatchActualLine:
    return _resolve(visible_production_actuals(user), actual_id, "Production actual line")


def visible_production_allocations(user: User) -> QuerySet[ProductionBatchAllocation]:
    """Every allocation row this caller may read, scoped the same way its batch is."""
    return ProductionBatchAllocation.objects.filter(
        actual__line__batch__warehouse__in=readable_production_warehouses(user)
    ).select_related("actual", "actual__line", "actual__line__batch", "lot", "location")


def resolve_production_allocation(user: User, allocation_id: int) -> ProductionBatchAllocation:
    """Out of scope is 404, exactly as it is for the batch the row belongs to."""
    return _resolve(visible_production_allocations(user), allocation_id, "allocation")


def production_lines_for(batch: ProductionBatch) -> QuerySet[ProductionBatchLine]:
    """
    One batch's requirements in their own deterministic order.

    Unscoped by design - the caller has already resolved the batch through
    `visible_production_batches`, and a second scope filter here would silently
    hide rows on a screen that had already proved its right to them.
    """
    return (
        ProductionBatchLine.objects.filter(batch=batch)
        .select_related("item", "item__base_unit", "source_version", "source_line")
        .prefetch_related("actuals__item", "actuals__substitute__substitute_item")
        .order_by("line_order")
    )


def substitute_candidates(line: ProductionBatchLine) -> QuerySet[RecipeLineSubstitute]:
    """
    The approved stand-ins for one requirement, in the recipe's own ranking.

    Read from the **source line**, not from the item: a substitute approved for
    the rice line is not approved for the oil line even when both name rice.
    Archived rows are excluded - somebody withdrew that approval - and the
    service re-checks every one of these rules anyway, because a filtered
    dropdown is a courtesy and never the control.
    """
    return (
        RecipeLineSubstitute.objects.filter(line=line.source_line, is_active=True)
        .select_related("substitute_item", "substitute_item__base_unit")
        .order_by("priority", "substitute_item__code")
    )


def visible_meal_records(user: User) -> QuerySet[MealRecord]:
    """
    Every meal this caller may read, newest consumed date first.

    Scoped by **branch reach** rather than by warehouse: a meal is fed at a
    branch and moves no stock, so there is no custody to scope it to. Reading
    is tied to the report family, so somebody who reads the kitchen reports
    reads the meal log with them.
    """
    return MealRecord.objects.filter(
        organization_id__in=organizations_with_permission(user, VIEW_KITCHEN_REPORT).values_list(
            "pk", flat=True
        )
    ).select_related("organization", "branch", "recipe", "recipe_version", "serving", "recorded_by")


def resolve_meal_record(user: User, meal_id: int) -> MealRecord:
    """Out of scope is 404: a 403 would confirm another branch's meal exists."""
    return _resolve(visible_meal_records(user), meal_id, "MealRecord")


def recordable_branches(user: User) -> QuerySet[Branch]:
    """Branches where a post this caller holds carries `record_meal`."""
    return branches_with_permission(user, RECORD_MEAL)


def draftable_recipes(user: User, organization: Organization) -> QuerySet[Recipe]:
    """
    Recipes that may actually be produced, for the create screen.

    Batch recipes only: a portion recipe has no `output_item`, and producing one
    would create stock of an item that deliberately does not exist (RCP-032).
    Filtering here is a courtesy - `create_production_batch` refuses the same
    shape with a named error, so a hand-made request naming a portion recipe is
    refused on its merits.
    """
    return (
        Recipe.objects.filter(organization=organization, is_active=True, output_item__isnull=False)
        .select_related("output_item")
        .order_by("code")
    )


def visible_batch_document_links(user: User) -> QuerySet[BatchDocumentLink]:
    """
    Every attribution this caller may read, newest first.

    Scoped through `visible_production_batches`, which is **warehouse**-scoped:
    a link is a statement about one kitchen store's flow, so it is readable
    exactly where the batch it annotates is readable. Scoping it to the report
    permission instead would let somebody who may read reports at one branch
    see attributions on another branch's stores.
    """
    return BatchDocumentLink.objects.filter(
        batch__in=visible_production_batches(user)
    ).select_related(
        "organization",
        "branch",
        "warehouse",
        "batch",
        "item",
        "item__base_unit",
        "transfer_line",
        "transfer_line__transfer",
        "waste_line",
        "waste_line__document",
        "created_by",
        "cancelled_by",
    )


def resolve_batch_document_link(user: User, link_id: int) -> BatchDocumentLink:
    """Out of scope is 404: a 403 would confirm another store's link exists."""
    return _resolve(visible_batch_document_links(user), link_id, "BatchDocumentLink")


def linkable_production_warehouses(user: User) -> QuerySet[Warehouse]:
    """Warehouses where a post this caller holds carries `link_batch_document`."""
    return _warehouses_with_permission(user, LINK_BATCH_DOCUMENT)
