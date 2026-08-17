"""
The recipe costing screens: the card, the historical resolver, and snapshots.

Kept out of `version_views.py` for the reason that module is kept out of
`views.py` — those screens edit a draft, those decide what happens to a
version, and these read money. The scaffolding is shared, so a costing screen
behaves exactly like a screen the operator already knows.

Three rules hold throughout:

* **Money is a second permission.** Every view here sets
  `required_permission = VIEW_RECIPE_COST` *and* re-checks it against the
  recipe's own organization, because a global Django group grants a codename
  and not a reach (ADR-016). `view_recipe` gets somebody the card; it does not
  get them the cost.
* **Cost columns are omitted, not blanked.** A caller without the permission
  never reaches these URLs at all, and the cost link does not render on the
  version detail page. A blanked column would tell the reader a number exists
  and that they are not trusted with it, which is a different statement from
  the one intended.
* **Hiding a button is presentation, never protection.** The snapshot action
  checks the same permission the API checks, so a hand-made POST from somebody
  who never saw the button is refused on its merits.

Every HTMX interaction has a full-page fallback and both paths run the same
authorization: the fragment is a rendering choice, not a second door.

**No selling price, margin, cost percentage, commission, labour or overhead
appears on any of these screens.** The label is `كلفة المواد المباشرة` —
direct material cost — because that is what the number is.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.inventory.selectors import resolve_manageable_warehouse
from apps.kitchen.cost_reconciliation import snapshot_findings
from apps.kitchen.costing import (
    PREVIEWABLE_VERSION_STATUSES,
    RecipeCostCard,
    cost_recipe_on_date,
    cost_recipe_version,
    preview_recipe_cost,
)
from apps.kitchen.forms import (
    CostCardForm,
    CostSnapshotFilterForm,
    CostSnapshotForm,
    HistoricalCostForm,
)
from apps.kitchen.models import RecipeCostSnapshot
from apps.kitchen.permissions import VIEW_RECIPE_COST
from apps.kitchen.selectors import (
    resolve_cost_snapshot,
    resolve_recipe,
    resolve_version,
    snapshots_for_version,
    visible_cost_snapshots,
)
from apps.kitchen.snapshots import create_recipe_cost_snapshot
from apps.kitchen.views import KitchenListView, KitchenViewMixin
from apps.organizations.authorization import require_reachable_organization_permission


class CostViewMixin(KitchenViewMixin):
    """
    Signed in, holding `view_recipe_cost`, and holding it **here**.

    `KitchenViewMixin.test_func` checks the codename globally, which stops an
    anonymous or unprivileged caller at the door. It cannot check *where*, so
    every view below also calls `require_reachable_organization_permission`
    against the recipe's organization once it has resolved one. A Django group
    membership with no organizational reach authorizes nothing.
    """

    required_permission = VIEW_RECIPE_COST

    def _require_here(self, organization: Any) -> None:
        require_reachable_organization_permission(self.actor, VIEW_RECIPE_COST, organization)

    def _base(self) -> str:
        return "kitchen/_bare.html" if self.is_htmx() else "shell.html"


def _card_context(card: RecipeCostCard) -> dict[str, Any]:
    """
    What every cost-card template needs, built once.

    `is_preview` rather than `not is_authoritative` in the template, because a
    banner that reads "PREVIEW — NOT APPROVED" should be driven by a positive
    fact rather than by the absence of one.
    """
    return {
        "card": card,
        "recipe": card.recipe,
        "version": card.version,
        "is_preview": not card.is_authoritative,
        "lines": card.lines,
        "missing": card.missing,
        "servings": card.servings,
        "class_totals": [
            (_("كلفة الغذاء"), card.food_total),
            (_("كلفة التغليف"), card.packaging_total),
            (_("كلفة المرافقات"), card.accompaniment_total),
        ],
    }


class RecipeCostCardView(CostViewMixin, View):
    """
    One version's cost card, for a warehouse and a date the reader names.

    `GET` with no parameters renders the selector alone — no figures, no
    guessed date, nothing that could be mistaken for an answer. Submitting the
    two fields re-renders the same screen with the card, so the warehouse/date
    refresh is one HTMX swap of one region and the identical URL works with
    JavaScript switched off.

    A `DRAFT` or `SUBMITTED` version is costed through `preview_recipe_cost`
    and carries the preview banner; anything frozen goes through
    `cost_recipe_version` and is authoritative. Which one is chosen is decided
    by the **stored status**, never by a caller-supplied flag.
    """

    template_name = "kitchen/cost_card.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        version = resolve_version(self.actor, self.kwargs["pk"])
        self._require_here(version.recipe.organization)
        form = CostCardForm(request.GET or None, actor=self.actor, recipe=version.recipe)
        context: dict[str, Any] = {
            "version": version,
            "recipe": version.recipe,
            "form": form,
            "page_title": _("كلفة الوصفة والطبق"),
            "snapshots": snapshots_for_version(version)[:10],
            "can_snapshot": str(version.status) not in PREVIEWABLE_VERSION_STATUSES,
            "fragment_base_template": self._base(),
        }
        if request.GET and form.is_valid():
            warehouse = resolve_manageable_warehouse(self.actor, form.cleaned_data["warehouse"].pk)
            as_of_date = form.cleaned_data["as_of_date"]
            try:
                card = (
                    preview_recipe_cost(version=version, warehouse=warehouse, as_of_date=as_of_date)
                    if str(version.status) in PREVIEWABLE_VERSION_STATUSES
                    else cost_recipe_version(
                        version=version, warehouse=warehouse, as_of_date=as_of_date
                    )
                )
            except ValidationError as refusal:
                # 200 with the message in the form: htmx does not swap an error
                # response, and a costing refusal is information rather than a
                # server fault.
                form.add_error(None, refusal)
            else:
                context.update(_card_context(card))
        return render(request, self.template_name, context)


class RecipeHistoricalCostView(CostViewMixin, View):
    """
    "What did this recipe cost at this branch on this day?"

    Version first, then costs, both driven by the same date (RCP-026). The
    resolver's own refusals surface as form errors rather than as a 500: a date
    before the recipe existed has an honest answer, and it is
    `recipe_version_not_effective` rather than a blank card.
    """

    template_name = "kitchen/cost_historical.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        recipe = resolve_recipe(self.actor, self.kwargs["pk"])
        self._require_here(recipe.organization)
        form = HistoricalCostForm(request.GET or None, actor=self.actor, recipe=recipe)
        context: dict[str, Any] = {
            "recipe": recipe,
            "form": form,
            "page_title": _("كلفة الوصفة بتاريخ"),
            "fragment_base_template": self._base(),
        }
        if request.GET and form.is_valid():
            warehouse = resolve_manageable_warehouse(self.actor, form.cleaned_data["warehouse"].pk)
            try:
                card = cost_recipe_on_date(
                    recipe=recipe,
                    branch=form.cleaned_data["branch"],
                    warehouse=warehouse,
                    on_date=form.cleaned_data["as_of_date"],
                )
            except ValidationError as refusal:
                form.add_error(None, refusal)
            else:
                context.update(_card_context(card))
        return render(request, self.template_name, context)


class CostSnapshotCreateView(CostViewMixin, View):
    """
    Freeze one authoritative card. The only write on any of these screens.

    `GET` renders the confirmation with the figures being frozen, so nobody
    signs a total they have not seen. `POST` recomputes the card **server-side**
    from the version, warehouse and date — never from anything the browser sent
    back — and hands that to the snapshot service. A form that round-tripped the
    totals would let a crafted POST record a number nobody calculated.
    """

    template_name = "kitchen/cost_snapshot_create.html"

    def _context(self, request: HttpRequest, version: Any, form: Any) -> dict[str, Any]:
        return {
            "version": version,
            "recipe": version.recipe,
            "form": form,
            "page_title": _("حفظ لقطة كلفة"),
            "fragment_base_template": self._base(),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        version = resolve_version(self.actor, self.kwargs["pk"])
        self._require_here(version.recipe.organization)
        form = CostSnapshotForm(request.GET or None, actor=self.actor, recipe=version.recipe)
        context = self._context(request, version, form)
        if request.GET and form.is_valid():
            try:
                warehouse = resolve_manageable_warehouse(
                    self.actor, form.cleaned_data["warehouse"].pk
                )
                card = cost_recipe_version(
                    version=version,
                    warehouse=warehouse,
                    as_of_date=form.cleaned_data["as_of_date"],
                )
            except ValidationError as refusal:
                form.add_error(None, refusal)
            else:
                context.update(_card_context(card))
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        version = resolve_version(self.actor, self.kwargs["pk"])
        self._require_here(version.recipe.organization)
        form = CostSnapshotForm(request.POST, actor=self.actor, recipe=version.recipe)
        if not form.is_valid():
            return render(request, self.template_name, self._context(request, version, form))

        try:
            warehouse = resolve_manageable_warehouse(self.actor, form.cleaned_data["warehouse"].pk)
            card = cost_recipe_version(
                version=version,
                warehouse=warehouse,
                as_of_date=form.cleaned_data["as_of_date"],
            )
            snapshot = create_recipe_cost_snapshot(
                card=card,
                actor=self.actor,
                idempotency_key=form.cleaned_data["idempotency_key"],
                reference=form.cleaned_data.get("reference", ""),
                reason=form.cleaned_data.get("reason", ""),
                note=form.cleaned_data.get("note", ""),
            )
        except ValidationError as refusal:
            form.add_error(None, refusal)
            context = self._context(request, version, form)
            # 200, not 4xx: htmx does not swap an error response, so a refusal
            # returned as one would leave the operator staring at an unchanged
            # form with no message.
            return render(request, self.template_name, context)

        messages.success(request, _("تم حفظ لقطة الكلفة."))
        target = reverse("kitchen:cost_snapshot_detail", args=[snapshot.pk])
        if self.is_htmx():
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        return HttpResponseRedirect(target)


class CostSnapshotListView(KitchenListView):
    """
    Every snapshot this caller may read, newest first.

    Scoped by `view_recipe_cost` through `visible_cost_snapshots`, so a
    storekeeper who legitimately reads every recipe card sees an **empty** list
    rather than a costed one with the money removed.
    """

    required_permission = VIEW_RECIPE_COST
    template_name = "kitchen/cost_snapshot_list.html"
    context_object_name = "snapshots"
    paginate_by = 25

    def get_queryset(self) -> QuerySet[RecipeCostSnapshot]:
        rows = visible_cost_snapshots(self.actor)
        form = CostSnapshotFilterForm(self.request.GET or None, actor=self.actor)
        self.filter_form = form
        if form.is_valid():
            if form.cleaned_data.get("recipe"):
                rows = rows.filter(recipe=form.cleaned_data["recipe"])
            if form.cleaned_data.get("warehouse"):
                rows = rows.filter(warehouse=form.cleaned_data["warehouse"])
            if form.cleaned_data.get("as_of_date"):
                rows = rows.filter(as_of_date=form.cleaned_data["as_of_date"])
        return rows

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["filter_form"] = self.filter_form
        context["page_title"] = _("لقطات الكلفة")
        return context


class CostSnapshotDetailView(CostViewMixin, View):
    """
    One frozen snapshot, with the evidence that makes it explicable.

    Shows the ledger cutoff and the calculation version beside the totals,
    because "what did the books say" needs both to be answerable. Runs the
    coherence checks on this one row and reports them inline — read-only, and
    there is no repair button because there is no repair.

    No edit, no delete, no archive. The rows refuse both verbs at the database.
    """

    template_name = "kitchen/cost_snapshot_detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        snapshot = resolve_cost_snapshot(self.actor, self.kwargs["pk"])
        self._require_here(snapshot.organization)
        return render(
            request,
            self.template_name,
            {
                "snapshot": snapshot,
                "lines": snapshot.lines.all(),
                "servings": snapshot.servings.all(),
                "findings": snapshot_findings(snapshot),
                "class_totals": [
                    (_("كلفة الغذاء"), snapshot.food_total),
                    (_("كلفة التغليف"), snapshot.packaging_total),
                    (_("كلفة المرافقات"), snapshot.accompaniment_total),
                ],
                "page_title": _("لقطة كلفة"),
                "fragment_base_template": self._base(),
            },
        )


def cost_link_is_visible(actor: Any, organization: Any) -> bool:
    """
    Whether the version-detail page should render its cost link at all.

    A helper rather than a template check so the answer comes from the same
    function the view uses. The link is **absent** without the permission, not
    disabled: an inert control still announces that a costing screen exists for
    this recipe, which is one more thing a reader has learned that they were
    not meant to.
    """
    from apps.organizations.authorization import has_organization_master_data_permission

    return has_organization_master_data_permission(actor, VIEW_RECIPE_COST, organization)
