"""
The production drafting screens: list, preview, create, and the draft workspace.

Kept out of `views.py` and `cost_views.py` for the reason those two are kept
apart — those edit a recipe, those read money, and these draft a document about
what one kitchen will cook on one day. The scaffolding is shared, so a production
screen behaves exactly like a screen the operator already knows.

## Four rules hold throughout

* **Authority is a warehouse question.** Every view resolves its batch through
  `visible_production_batches` — scoped to warehouses where a post the caller
  actually holds carries `view_production` — and every write re-checks
  `create_production_batch` at that same warehouse. Somebody who reads the whole
  menu sees an empty list here. `KitchenViewMixin.test_func` checks the codename
  globally, which stops an anonymous or unprivileged caller at the door; it
  cannot check *where*, so each view asks again once it has resolved a warehouse.
* **Hiding a button is presentation, never protection.** Every action checks the
  same authorization the API checks, so a hand-made POST from somebody who never
  saw the control is refused on its merits.
* **No view calls `form.save()`.** Every mutation goes through
  `apps/kitchen/production.py`, which re-reads the row under a lock in the
  canonical order, checks the invariants and records the audit event.
* **Every HTMX interaction has a full-page fallback.** The same URL answers both;
  the fragment is a rendering choice, not a second door, and the identical route
  works with JavaScript switched off.

## What these screens deliberately do not show

No post button. No reversal button. No lot picker, no location picker, no
availability or reservation panel. No inventory value, no recipe cost, no actual
cost, no journal, no account and no cost centre.

Not one of those is hidden — none of them **exists** at Task 3.4. A disabled
"post" control would tell the operator that posting is one permission away, when
in fact the service does not exist and a check constraint named
`production_batch_is_draft_only_until_task_3_5` refuses the row outright.

## The workspace is one page

The fourteen capabilities the task asks for are not fourteen routes. The batch
detail screen *is* the requirement table, the actual editor, the substitute
picker, the readiness panel, the component-path display and the audit timeline,
because they are one document and an operator reading a variance needs the plan
beside it. The actions that need a confirmation — rescale, reset-and-rescale,
discard — are their own routes because each is a decision with consequences worth
reading before signing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.core.models import AuditEvent
from apps.inventory.selectors import resolve_manageable_warehouse
from apps.kitchen.forms import (
    ProductionActualForm,
    ProductionAllocationForm,
    ProductionBatchCreateForm,
    ProductionBatchFilterForm,
    ProductionDiscardForm,
    ProductionNotesForm,
    ProductionOutputForm,
    ProductionPostForm,
    ProductionPreviewForm,
    ProductionRescaleForm,
    ProductionReverseForm,
    ProductionSubstituteForm,
)
from apps.kitchen.models import ProductionBatch
from apps.kitchen.permissions import (
    CREATE_PRODUCTION_BATCH,
    POST_PRODUCTION_BATCH,
    REVERSE_PRODUCTION_BATCH,
    VIEW_PRODUCTION,
)
from apps.kitchen.production import (
    add_production_batch_substitute,
    consumption_comparisons,
    create_production_batch,
    discard_production_batch,
    preview_production_batch,
    production_batch_readiness,
    record_production_output,
    remove_production_batch_substitute,
    rescale_production_batch,
    update_production_batch_actuals,
    update_production_batch_notes,
)
from apps.kitchen.production_posting import (
    AllocationInput,
    allocation_is_required,
    build_posting_plan,
    post_production_batch,
    reverse_production_batch,
    set_production_allocations,
)
from apps.kitchen.production_reconciliation import batch_findings
from apps.kitchen.selectors import (
    cost_readable_organization_ids,
    draftable_production_warehouses,
    production_lines_for,
    resolve_production_actual,
    resolve_production_batch,
    resolve_production_line,
    substitute_candidates,
    visible_production_batches,
)
from apps.kitchen.views import KitchenListView, KitchenViewMixin
from apps.organizations.authorization import (
    has_warehouse_permission,
    require_warehouse_permission,
)

if TYPE_CHECKING:
    from apps.users.models import User


class ProductionViewMixin(KitchenViewMixin):
    """
    Signed in, holding `view_production`, and holding it **at this warehouse**.

    The global codename check happens in `test_func`; `_require_here` is what
    answers *where*, and it is called by every view below once it has a batch. A
    Django group membership with no warehouse reach authorizes nothing (ADR-016).
    """

    required_permission = VIEW_PRODUCTION

    def _require_here(self, warehouse: Any) -> None:
        require_warehouse_permission(self.actor, VIEW_PRODUCTION, warehouse)

    def _require_draft_authority(self, warehouse: Any) -> None:
        require_warehouse_permission(self.actor, CREATE_PRODUCTION_BATCH, warehouse)

    def _base(self) -> str:
        return "kitchen/_bare.html" if self.is_htmx() else "shell.html"


def can_draft_here(actor: User, warehouse: Any) -> bool:
    """
    Whether the edit controls render at all on this batch.

    A helper rather than a template check, so the answer comes from the same
    function the view uses to refuse. The controls are **absent** without the
    authority rather than disabled: an inert button announces that an action
    exists and that the reader is not trusted with it, which is a different
    statement from the one intended.
    """
    return has_warehouse_permission(actor, CREATE_PRODUCTION_BATCH, warehouse)


def batch_context(actor: User, batch: ProductionBatch) -> dict[str, Any]:
    """
    Everything the workspace shows, built once.

    Requirements with their actual rows, the comparison that says when a variance
    is a number and when it is not, readiness with its non-blocking findings, and
    the draft's own audit timeline. One function because the fragments and the
    full page must show the same document — a panel that recomputed its own view
    of readiness could disagree with the banner above it.
    """
    lines = list(production_lines_for(batch))
    comparisons = {row.line.pk: row for row in consumption_comparisons(batch)}
    readiness = production_batch_readiness(batch)
    return {
        "batch": batch,
        "lines": [
            {
                "line": line,
                "actuals": list(line.actuals.all()),
                "comparison": comparisons.get(line.pk),
                "candidates": substitute_candidates(line),
            }
            for line in lines
        ],
        "readiness": readiness,
        "is_ready": readiness.is_ready,
        "findings": batch_findings(batch),
        "timeline": AuditEvent.objects.filter(
            target_type="kitchen.ProductionBatch", target_id=str(batch.pk)
        ).order_by("-occurred_at")[:50],
        "can_draft": can_draft_here(actor, batch.warehouse),
    }


class ProductionBatchListView(KitchenListView):
    """
    Every draft this caller may read, newest planned date first.

    Scoped by `view_production` at the **warehouse** through
    `visible_production_batches`, so a purchasing officer who legitimately reads
    every recipe sees an empty list rather than a filtered one.

    The create button renders only where the caller may actually draft, because
    offering it everywhere and refusing on submit teaches an operator that the
    system is arbitrary.
    """

    required_permission = VIEW_PRODUCTION
    manage_permission = CREATE_PRODUCTION_BATCH
    template_name = "kitchen/production_list.html"
    context_object_name = "batches"
    page_title = _("أوامر الإنتاج")
    page_hint = _("مسودات الإنتاج. لا ترحيل ولا حركة مخزنية في هذه المهمة.")
    search_fields = ("recipe__code", "recipe__name_ar", "notes")
    paginate_by = 25

    def scoped_queryset(self) -> QuerySet[ProductionBatch]:
        rows = visible_production_batches(self.actor)
        form = ProductionBatchFilterForm(self.request.GET or None, actor=self.actor)
        self.filter_form = form
        if form.is_valid():
            if form.cleaned_data.get("recipe"):
                rows = rows.filter(recipe=form.cleaned_data["recipe"])
            if form.cleaned_data.get("warehouse"):
                rows = rows.filter(warehouse=form.cleaned_data["warehouse"])
            if form.cleaned_data.get("planned_business_date"):
                rows = rows.filter(planned_business_date=form.cleaned_data["planned_business_date"])
        return rows

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["filter_form"] = self.filter_form
        context["create_url"] = (
            reverse("kitchen:production_create")
            if draftable_production_warehouses(self.actor).exists()
            else None
        )
        context["create_label"] = _("مسودة إنتاج جديدة")
        return context


class ProductionPreviewView(ProductionViewMixin, View):
    """
    "Which version governs, and what would it ask for?" — without writing a row.

    Reads the same resolver, the same expansion and the same arithmetic as the
    create command, deliberately: a preview computed a second way is a preview
    that can disagree with the thing it previews.

    `GET` with no parameters renders the selector alone. No figures, no guessed
    date, nothing that could be mistaken for an answer.
    """

    template_name = "kitchen/production_preview.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = ProductionPreviewForm(request.GET or None, actor=self.actor)
        context: dict[str, Any] = {
            "form": form,
            "page_title": _("معاينة أمر إنتاج"),
            "fragment_base_template": self._base(),
        }
        if request.GET and form.is_valid():
            try:
                preview = preview_production_batch(
                    recipe=form.cleaned_data["recipe"],
                    branch=form.cleaned_data["branch"],
                    planned_business_date=form.cleaned_data["planned_business_date"],
                    multiplier=form.cleaned_data["multiplier"],
                )
            except ValidationError as refusal:
                # 200 with the message in the form: htmx does not swap an error
                # response, and a resolver refusal is information rather than a
                # server fault.
                form.add_error(None, refusal)
            else:
                context["preview"] = preview
                context["planned"] = preview.planned
        return render(request, self.template_name, context)


class ProductionBatchCreateView(ProductionViewMixin, View):
    """
    Draft one batch from the version in force at a branch on a date.

    The warehouse is resolved through `resolve_manageable_warehouse` — with the
    caller, never fetched and then checked — so there is no moment at which an
    out-of-scope warehouse exists in a local variable.
    """

    required_permission = CREATE_PRODUCTION_BATCH
    template_name = "kitchen/production_create.html"

    def _context(self, form: Any) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": _("مسودة إنتاج جديدة"),
            "page_hint": _("النسخة تُحدَّد مرة واحدة من الفرع والتاريخ، ولا يُعاد تحديدها بعدها."),
            "fragment_base_template": self._base(),
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(
            request, self.template_name, self._context(ProductionBatchCreateForm(actor=self.actor))
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = ProductionBatchCreateForm(request.POST, actor=self.actor)
        if not form.is_valid():
            return render(request, self.template_name, self._context(form))
        try:
            warehouse = resolve_manageable_warehouse(self.actor, form.cleaned_data["warehouse"].pk)
            self._require_draft_authority(warehouse)
            batch = create_production_batch(
                recipe=form.cleaned_data["recipe"],
                branch=form.cleaned_data["branch"],
                warehouse=warehouse,
                planned_business_date=form.cleaned_data["planned_business_date"],
                multiplier=form.cleaned_data["multiplier"],
                actor=self.actor,
                idempotency_key=form.cleaned_data["idempotency_key"],
                notes=form.cleaned_data["notes"],
            )
        except ValidationError as refusal:
            form.add_error(None, refusal)
            return render(request, self.template_name, self._context(form))
        messages.success(request, _("تم إنشاء مسودة الإنتاج."))
        return self._go(reverse("kitchen:production_detail", args=[batch.pk]))

    def _go(self, target: str) -> HttpResponse:
        if self.is_htmx():
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        return HttpResponseRedirect(target)


class ProductionBatchDetailView(ProductionViewMixin, View):
    """
    One draft, whole: the plan, what was consumed, and what is still missing.

    The requirement table shows the exact `component_path` — `2.1` — beside the
    label path, because a variance on a nested ingredient is only actionable if
    the reader can see **which level** of the tree it came from. A flattened list
    that lost the path would save a column and lose the report's subject.

    The variance column is a number only where the dimensions agree. A
    requirement met with a stand-in measured in another dimension shows both rows
    separately and says, in words, that the two are not quantitatively comparable.
    """

    template_name = "kitchen/production_detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = resolve_production_batch(self.actor, self.kwargs["pk"])
        self._require_here(batch.warehouse)
        context = batch_context(self.actor, batch)
        # The posting half of the same document. Merged here rather than shown
        # on a separate screen because an operator deciding whether to post is
        # reading the plan and the consumption at the same moment, and a
        # posted batch's evidence belongs beside the requirements it came from.
        context.update(posting_context(self.actor, batch))
        context["page_title"] = _("مسودة إنتاج") if batch.is_draft else _("أمر إنتاج مرحّل")
        context["fragment_base_template"] = self._base()
        context["notes_form"] = ProductionNotesForm(
            actor=self.actor, initial={"notes": batch.notes}
        )
        output_item = batch.recipe.output_item
        context["output_form"] = (
            ProductionOutputForm(
                actor=self.actor,
                output_item=output_item,
                initial={
                    "entered_quantity": batch.actual_output_entered_quantity,
                    "entered_unit": batch.actual_output_unit_id,
                },
            )
            if output_item is not None
            else None
        )
        return render(request, self.template_name, context)


class ProductionRequirementsView(ProductionViewMixin, View):
    """
    The requirement table alone, for an HTMX swap after any edit.

    Renders the same partial the detail page includes, so the two cannot drift:
    a fragment with its own copy of the table would agree until somebody fixed a
    column in one of them.
    """

    template_name = "kitchen/_production_requirements.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = resolve_production_batch(self.actor, self.kwargs["pk"])
        self._require_here(batch.warehouse)
        return render(request, self.template_name, batch_context(self.actor, batch))


class ProductionReadinessView(ProductionViewMixin, View):
    """
    The readiness panel: every problem at once, and every non-blocking finding.

    Derived, never stored. There is no `READY` status and no readiness column — a
    stored flag would go stale the moment somebody edited a quantity, and the only
    way to trust it would be to recompute it, which is what this does.

    Checks **no stock**. Availability, lots, expiry, locations and negative-stock
    refusal are Task 3.5's, at posting, and a draft that reserved stock would make
    drafting a thing that can fail for reasons nothing about the draft can fix.
    """

    template_name = "kitchen/_production_readiness.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = resolve_production_batch(self.actor, self.kwargs["pk"])
        self._require_here(batch.warehouse)
        return render(request, self.template_name, batch_context(self.actor, batch))


class ProductionTimelineView(ProductionViewMixin, View):
    """Who did what to this draft, newest first. Read-only, and append-only below."""

    template_name = "kitchen/_production_timeline.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = resolve_production_batch(self.actor, self.kwargs["pk"])
        self._require_here(batch.warehouse)
        return render(request, self.template_name, batch_context(self.actor, batch))


class ProductionWriteView(ProductionViewMixin, View):
    """
    One form, one service call, one re-rendered fragment.

    `authorize` runs before `perform` and both run again on every POST, so the
    template's decision to render a control is never the control. A refusal comes
    back as **200** with the message in the form, because htmx does not swap an
    error response and a refusal returned as one would leave the operator staring
    at an unchanged form with no explanation.
    """

    required_permission = CREATE_PRODUCTION_BATCH
    template_name = "kitchen/production_action.html"
    form_class: Any = None
    page_title: Any = ""
    page_hint: Any = ""
    success_message: Any = ""

    def load(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def batch_of(self, instance: Any) -> ProductionBatch:  # pragma: no cover - overridden
        raise NotImplementedError

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {}

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {}

    def authorize(self, batch: ProductionBatch) -> None:
        """
        Which authority this particular command needs at this warehouse.

        Drafting for every Task 3.4 command, which is why that is the default.
        Task 3.5's posting and reversal override it: they are separate grants
        because they are separate acts, and a subclass that forgot to override
        would ask for the weaker one, so the override is a deliberate line of
        code rather than an omission.
        """
        self._require_draft_authority(batch.warehouse)

    def extra_context_for(self, instance: Any) -> dict[str, Any]:
        """What this command's confirmation screen shows besides its form."""
        return {}

    def perform(self, instance: Any, form: Any) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:production_detail", args=[self.batch_of(instance).pk])

    def build_form(self, instance: Any, data: Any = None) -> Any:
        kwargs: dict[str, Any] = {"actor": self.actor, **self.form_kwargs(instance)}
        if data is not None:
            return self.form_class(data, **kwargs)
        return self.form_class(initial=self.initial_for(instance), **kwargs)

    def render_form(self, instance: Any, form: Any) -> HttpResponse:
        context: dict[str, Any] = {
            "form": form,
            "instance": instance,
            "batch": self.batch_of(instance),
            "page_title": self.page_title,
            "page_hint": self.page_hint,
            "fragment_base_template": self._base(),
        }
        context.update(self.extra_context_for(instance))
        return render(self.request, self.template_name, context)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        instance = self.load()
        self.authorize(self.batch_of(instance))
        return self.render_form(instance, self.build_form(instance))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        instance = self.load()
        # Checked on POST as well as on GET. A hand-made POST from somebody who
        # never saw the form reaches exactly this line.
        self.authorize(self.batch_of(instance))
        form = self.build_form(instance, data=request.POST)
        if not form.is_valid():
            return self.render_form(instance, form)
        try:
            result = self.perform(instance, form)
        except ValidationError as refusal:
            form.add_error(None, refusal)
            return self.render_form(instance, form)
        if self.success_message:
            messages.success(request, self.success_message)
        target = self.success_url(instance, result)
        if self.is_htmx():
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        return HttpResponseRedirect(target)


class ProductionActualUpdateView(ProductionWriteView):
    """
    Record what was actually consumed on one row.

    More than planned, less, or zero — all accepted. A variance is the batch
    variance report's business, never a refusal here (RCP-030): refusing would
    teach kitchens to falsify quantities to match the recipe, which is the one
    outcome that makes the whole module useless.
    """

    form_class = ProductionActualForm
    page_title = _("الكمية المستهلكة فعلاً")
    success_message = _("تم تسجيل الكمية الفعلية.")

    def load(self) -> Any:
        return resolve_production_actual(self.actor, self.kwargs["pk"])

    def batch_of(self, instance: Any) -> ProductionBatch:
        batch: ProductionBatch = instance.line.batch
        return batch

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {"item": instance.item}

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "entered_quantity": instance.entered_quantity,
            "entered_unit": instance.entered_unit_id,
            "package_unit": instance.package_unit_id,
            "measured_base_quantity": instance.measured_base_quantity,
            "note": instance.note,
        }

    def perform(self, instance: Any, form: Any) -> Any:
        return update_production_batch_actuals(
            actual=instance,
            entered_quantity=form.cleaned_data["entered_quantity"],
            entered_unit=form.cleaned_data.get("entered_unit"),
            package_unit=form.cleaned_data.get("package_unit"),
            measured_base_quantity=form.cleaned_data.get("measured_base_quantity"),
            note=form.cleaned_data.get("note", ""),
            actor=self.actor,
        )


class ProductionSubstituteCreateView(ProductionWriteView):
    """
    Record an approved stand-in **beside** the primary row, not instead of it.

    A split is the case this exists for: 3 kg of the primary plus 1 kg of a
    substitute is two facts about one requirement, and the operator decides what
    the primary row becomes. Nothing here reduces it automatically, because "the
    rest was substituted" is an assumption and the kitchen knows.
    """

    form_class = ProductionSubstituteForm
    page_title = _("إضافة بديل معتمد")
    success_message = _("تم تسجيل البديل.")

    def load(self) -> Any:
        return resolve_production_line(self.actor, self.kwargs["pk"])

    def batch_of(self, instance: Any) -> ProductionBatch:
        batch: ProductionBatch = instance.batch
        return batch

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {"line": instance}

    def perform(self, instance: Any, form: Any) -> Any:
        return add_production_batch_substitute(
            line=instance,
            item=form.cleaned_data["substitute"].substitute_item,
            entered_quantity=form.cleaned_data["entered_quantity"],
            entered_unit=form.cleaned_data.get("entered_unit"),
            package_unit=form.cleaned_data.get("package_unit"),
            measured_base_quantity=form.cleaned_data.get("measured_base_quantity"),
            reason=form.cleaned_data.get("reason", ""),
            actor=self.actor,
        )


class ProductionActualDeleteView(ProductionWriteView):
    """
    Withdraw one actual row.

    The primary row may go when a substitution was **complete** — the kitchen used
    none of the planned item — because forcing a zero row to remain would be
    forcing a statement about an item that never entered the pot. What may not
    happen is a requirement left with no actual row at all: that is not "no
    consumption", it is "nobody said".
    """

    form_class = ProductionDiscardForm
    template_name = "kitchen/production_confirm.html"
    page_title = _("حذف سطر استهلاك")
    page_hint = _("لا يمكن حذف آخر سطر — اجعل كميته صفراً بدلاً من ذلك.")
    success_message = _("تم حذف سطر الاستهلاك.")

    def load(self) -> Any:
        return resolve_production_actual(self.actor, self.kwargs["pk"])

    def batch_of(self, instance: Any) -> ProductionBatch:
        batch: ProductionBatch = instance.line.batch
        return batch

    def perform(self, instance: Any, form: Any) -> Any:
        remove_production_batch_substitute(
            actual=instance, actor=self.actor, reason=form.cleaned_data.get("reason", "")
        )
        return None

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:production_detail", args=[self.batch_of(instance).pk])


class ProductionRescaleView(ProductionWriteView):
    """
    Change how much of the recipe this run is — and say so when it costs somebody.

    An ordinary rescale is refused the moment an operator has entered anything.
    Reset-and-rescale does it anyway and requires a deliberate flag **and** a
    reason, because the consequence is the point of the command and should be
    stated rather than discovered.

    The confirmation screen is the same route for both, so the operator reads what
    they are about to discard on the screen where they decide it.
    """

    form_class = ProductionRescaleForm
    template_name = "kitchen/production_rescale.html"
    page_title = _("تغيير معامل الإنتاج")
    success_message = _("تم تغيير المعامل.")

    def load(self) -> Any:
        return resolve_production_batch(self.actor, self.kwargs["pk"])

    def batch_of(self, instance: Any) -> ProductionBatch:
        batch: ProductionBatch = instance
        return batch

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {"multiplier": instance.multiplier}

    def render_form(self, instance: Any, form: Any) -> HttpResponse:
        response = super().render_form(instance, form)
        return response

    def perform(self, instance: Any, form: Any) -> Any:
        return rescale_production_batch(
            batch=instance,
            multiplier=form.cleaned_data["multiplier"],
            actor=self.actor,
            reset_actuals=form.cleaned_data.get("reset_actuals", False),
            reason=form.cleaned_data.get("reason", ""),
        )


class ProductionOutputView(ProductionWriteView):
    """What actually came out. Entered by the operator, never derived (RCP-031)."""

    form_class = ProductionOutputForm
    page_title = _("الناتج الفعلي")
    success_message = _("تم تسجيل الناتج الفعلي.")

    def load(self) -> Any:
        return resolve_production_batch(self.actor, self.kwargs["pk"])

    def batch_of(self, instance: Any) -> ProductionBatch:
        batch: ProductionBatch = instance
        return batch

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        output_item = instance.recipe.output_item
        if output_item is None:
            # Refused at creation (RCP-032), so reaching here means the recipe
            # lost its output item afterwards. A named refusal rather than an
            # attribute error on a `None`, because the operator can act on the
            # first and not on the second.
            raise ValidationError(
                _("هذه الوصفة بلا صنف ناتج."),
                code="production_batch_recipe_has_no_output_item",
            )
        return {"output_item": output_item}

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "entered_quantity": instance.actual_output_entered_quantity,
            "entered_unit": instance.actual_output_unit_id,
        }

    def perform(self, instance: Any, form: Any) -> Any:
        return record_production_output(
            batch=instance,
            entered_quantity=form.cleaned_data["entered_quantity"],
            entered_unit=form.cleaned_data["entered_unit"],
            actor=self.actor,
        )


class ProductionNotesView(ProductionWriteView):
    """The operator's own note, through a service so the edit leaves a trail."""

    form_class = ProductionNotesForm
    page_title = _("ملاحظات المسودة")
    success_message = _("تم حفظ الملاحظات.")

    def load(self) -> Any:
        return resolve_production_batch(self.actor, self.kwargs["pk"])

    def batch_of(self, instance: Any) -> ProductionBatch:
        batch: ProductionBatch = instance
        return batch

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {"notes": instance.notes}

    def perform(self, instance: Any, form: Any) -> Any:
        return update_production_batch_notes(
            batch=instance, notes=form.cleaned_data.get("notes", ""), actor=self.actor
        )


class ProductionDiscardView(ProductionWriteView):
    """
    Throw a draft away.

    Cascades to its own requirement and actual rows and to nothing else: no stock
    moved, no journal exists, and the recipe it was drafted from is untouched. A
    reason is required once anything has been entered.
    """

    form_class = ProductionDiscardForm
    template_name = "kitchen/production_confirm.html"
    page_title = _("حذف مسودة الإنتاج")
    page_hint = _("لا يوجد ترحيل ولا حركة مخزنية — الحذف يزيل المسودة وسطورها فقط.")
    success_message = _("تم حذف المسودة.")

    def load(self) -> Any:
        return resolve_production_batch(self.actor, self.kwargs["pk"])

    def batch_of(self, instance: Any) -> ProductionBatch:
        batch: ProductionBatch = instance
        return batch

    def perform(self, instance: Any, form: Any) -> Any:
        discard_production_batch(
            batch=instance, actor=self.actor, reason=form.cleaned_data.get("reason", "")
        )
        return None

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:production_list")


# ---------------------------------------------------------------------------
# Task 3.5 — allocation, posting, reversal, and the posted document
# ---------------------------------------------------------------------------


def posting_context(actor: User, batch: ProductionBatch) -> dict[str, Any]:
    """
    Everything the posting half of the workspace shows.

    Separate from `batch_context` because the two answer different questions,
    and a posted batch has no readiness left to compute: readiness is about
    what still has to be entered, and after posting the answer is nothing, for
    good.

    The **movement timeline** is the evidence panel — what left, what arrived,
    at what value, out of which lot. It matters most when a batch wrote no
    journal, because then the stock ledger is the only place the event exists.
    """
    plan = None
    if batch.is_draft:
        try:
            plan = build_posting_plan(batch)
        except ValidationError:
            plan = None
    entry = batch.stock_entry
    reversal_entry = batch.reversal_stock_entry
    movements: list[Any] = []
    if entry is not None:
        movements = list(
            entry.movements.select_related("item", "lot", "warehouse").order_by("posted_sequence")
        )
    reversal_movements: list[Any] = []
    if reversal_entry is not None:
        reversal_movements = list(
            reversal_entry.movements.select_related("item", "lot").order_by("posted_sequence")
        )
    return {
        "plan": plan,
        "movements": movements,
        "reversal_movements": reversal_movements,
        # Money is **omitted, not blanked**: the template renders no value
        # column at all without `view_recipe_cost`, because a blanked column
        # tells the reader a number exists and that they are not trusted with
        # it, which is a different statement from the one intended.
        "can_read_cost": batch.organization_id in cost_readable_organization_ids(actor),
        "can_post": has_warehouse_permission(actor, POST_PRODUCTION_BATCH, batch.warehouse),
        "can_reverse": has_warehouse_permission(actor, REVERSE_PRODUCTION_BATCH, batch.warehouse),
        # The sentence a reader needs when there is no journal to click through
        # to. A journal that is rightly absent and one that is wrongly missing
        # look identical on a screen, so the screen says which this is.
        "no_journal_reason": (
            _("لا يوجد قيد — صافي حسابات المخزون صفر.")
            if entry is not None and batch.journal_entry_id is None
            else ""
        ),
    }


class ProductionMovementsView(ProductionViewMixin, View):
    """The posted movement timeline, as a fragment and as a page."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        batch = resolve_production_batch(self.actor, kwargs["pk"])
        self._require_here(batch.warehouse)
        context: dict[str, Any] = {"base_template": self._base(), "batch": batch}
        context.update(posting_context(self.actor, batch))
        return render(request, "kitchen/production_movements.html", context)


class ProductionAllocateView(ProductionViewMixin, View):
    """
    Name the lots and bins one consumption row came out of.

    A `GET` lists what is allocated so far and offers one more row; a `POST`
    adds a row, and `clear` empties the set. The service replaces the whole set
    on every call, so this view reads the current rows and hands back the
    complete intended answer rather than an increment — which is what makes an
    interrupted operator's second attempt produce the same state as their
    first.
    """

    def _load(self, pk: int) -> tuple[Any, ProductionBatch]:
        actual = resolve_production_actual(self.actor, pk)
        batch: ProductionBatch = actual.line.batch
        self._require_here(batch.warehouse)
        return actual, batch

    def _render(
        self, request: HttpRequest, actual: Any, batch: ProductionBatch, form: Any
    ) -> HttpResponse:
        return render(
            request,
            "kitchen/production_allocate.html",
            {
                "base_template": self._base(),
                "fragment_base_template": self._base(),
                "page_title": _("تخصيص اللوطات والمواقع"),
                "batch": batch,
                "actual": actual,
                "allocations": list(actual.allocations.select_related("lot", "location").all()),
                "allocation_required": allocation_is_required(actual),
                "form": form,
                "can_draft": can_draft_here(self.actor, batch.warehouse),
            },
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        actual, batch = self._load(kwargs["pk"])
        form = ProductionAllocationForm(
            actor=self.actor, item=actual.item, warehouse=batch.warehouse
        )
        return self._render(request, actual, batch, form)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        actual, batch = self._load(kwargs["pk"])
        self._require_draft_authority(batch.warehouse)

        if request.POST.get("clear"):
            set_production_allocations(actual=actual, rows=[])
            messages.success(request, _("تم مسح التخصيصات."))
            return HttpResponseRedirect(reverse("kitchen:production_detail", args=[batch.pk]))

        form = ProductionAllocationForm(
            request.POST, actor=self.actor, item=actual.item, warehouse=batch.warehouse
        )
        if form.is_valid():
            wanted = [
                AllocationInput(base_quantity=row.base_quantity, lot=row.lot, location=row.location)
                for row in actual.allocations.all()
            ]
            wanted.append(
                AllocationInput(
                    base_quantity=form.cleaned_data["base_quantity"],
                    lot=form.cleaned_data.get("lot"),
                    location=form.cleaned_data.get("location"),
                )
            )
            try:
                set_production_allocations(actual=actual, rows=wanted)
            except ValidationError as refusal:
                form.add_error(None, refusal)
            else:
                messages.success(request, _("تم حفظ التخصيص."))
                return HttpResponseRedirect(reverse("kitchen:production_detail", args=[batch.pk]))
        return self._render(request, actual, batch, form)


class ProductionPostView(ProductionWriteView):
    """
    Commit the batch to both ledgers, or refuse and change nothing.

    The confirmation is a real screen rather than a JavaScript dialogue because
    it is where the operator reads what is about to move: every consumption
    with its lot, the output, and — when the accounts all net to zero — the
    sentence saying that no journal will exist and why.
    """

    form_class = ProductionPostForm
    template_name = "kitchen/production_post.html"
    page_title = _("ترحيل أمر الإنتاج")
    success_message = _("تم ترحيل أمر الإنتاج.")

    def load(self) -> Any:
        return resolve_production_batch(self.actor, self.kwargs["pk"])

    def batch_of(self, instance: Any) -> ProductionBatch:
        batch: ProductionBatch = instance
        return batch

    def authorize(self, batch: ProductionBatch) -> None:
        require_warehouse_permission(self.actor, POST_PRODUCTION_BATCH, batch.warehouse)

    def extra_context_for(self, instance: Any) -> dict[str, Any]:
        context = batch_context(self.actor, instance)
        context.update(posting_context(self.actor, instance))
        return context

    def perform(self, instance: Any, form: Any) -> Any:
        return post_production_batch(
            batch=instance,
            idempotency_key=f"screen-post:{instance.public_id}",
            actor=self.actor,
            reason=form.cleaned_data.get("reason", ""),
        )


class ProductionReverseView(ProductionWriteView):
    """Undo a posted batch, once, with a reason that is kept forever."""

    form_class = ProductionReverseForm
    template_name = "kitchen/production_reverse.html"
    page_title = _("عكس أمر الإنتاج")
    page_hint = _("العكس يعيد كل مدخل بقيمته المرحّلة ويسحب الناتج بقيمته.")
    success_message = _("تم عكس أمر الإنتاج.")

    def load(self) -> Any:
        return resolve_production_batch(self.actor, self.kwargs["pk"])

    def batch_of(self, instance: Any) -> ProductionBatch:
        batch: ProductionBatch = instance
        return batch

    def authorize(self, batch: ProductionBatch) -> None:
        require_warehouse_permission(self.actor, REVERSE_PRODUCTION_BATCH, batch.warehouse)

    def extra_context_for(self, instance: Any) -> dict[str, Any]:
        return posting_context(self.actor, instance)

    def perform(self, instance: Any, form: Any) -> Any:
        return reverse_production_batch(
            batch=instance,
            idempotency_key=f"screen-reverse:{instance.public_id}",
            reason=form.cleaned_data["reason"],
            actor=self.actor,
        )
