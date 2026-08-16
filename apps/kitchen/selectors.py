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

from django.db.models import Model, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.kitchen.models import (
    COMPONENT_ELIGIBLE_STATUSES,
    OPEN_VERSION_STATUSES,
    Recipe,
    RecipeCategory,
    RecipeComponent,
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
from apps.kitchen.permissions import MANAGE_RECIPE
from apps.organizations.authorization import (
    OutOfScope,
    organization_scope,
    organizations_with_permission,
)
from apps.organizations.models import Organization
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
