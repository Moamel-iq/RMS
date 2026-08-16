"""
The recipe version workspace: history, detail, comparison, timeline and the
five lifecycle actions.

Kept out of `views.py` for the same reason `lifecycle.py` is kept out of
`services.py`: those screens edit a draft, these decide what happens to one.
The scaffolding is shared — `KitchenViewMixin`, `KitchenWriteView` and
`KitchenActionView` are imported rather than reimplemented, so a lifecycle
screen behaves exactly like a screen the operator already knows.

Two rules hold throughout, unchanged from Task 3.1 and load-bearing here:

* **No view calls a model's `save()`.** Every mutation goes through
  `apps/kitchen/lifecycle.py`, which re-reads the authoritative row under a
  lock, re-checks the control, records the audit event and commits.
* **Hiding a button is presentation, never protection.** Each action view
  checks the same permission the API does, so a hand-made POST to an action the
  operator never saw is refused on its merits. There is a test for exactly
  that, per action.

Every HTMX interaction here has a full-page fallback, and both paths run the
same authorization: the fragment is a rendering choice, not a second door.

**No cost column and no cost endpoint.** Task 3.3 owns costing.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.kitchen.comparison import compare_recipe_versions
from apps.kitchen.forms import (
    ActivateVersionForm,
    ApproveVersionForm,
    RejectVersionForm,
    ResolverPreviewForm,
    ReviewSignoffForm,
    SupersedeVersionForm,
)
from apps.kitchen.lifecycle import (
    activate_recipe_version,
    approve_recipe_version,
    record_recipe_version_review,
    reject_recipe_version,
    resolve_recipe_version,
    review_gaps,
    submission_problems,
    submit_recipe_version,
    supersede_recipe_version,
    version_timeline,
)
from apps.kitchen.models import (
    OPEN_VERSION_STATUSES,
    RecipeVersion,
    RecipeVersionStatus,
)
from apps.kitchen.permissions import (
    ACTIVATE_RECIPE_VERSION,
    APPROVE_RECIPE_VERSION,
    REJECT_RECIPE_VERSION,
    REVIEW_RECIPE_VERSION,
    SUBMIT_RECIPE_VERSION,
    VIEW_RECIPE,
)
from apps.kitchen.selectors import resolve_recipe, resolve_version, versions_of, visible_versions
from apps.kitchen.views import KitchenListView, KitchenViewMixin, KitchenWriteView
from apps.organizations.authorization import (
    has_organization_master_data_permission,
    require_reachable_organization_permission,
)
from apps.organizations.models import Branch


def _held(actor: Any, permission: str, organization: Any) -> bool:
    """Whether this caller may exercise this authority here — for the template."""
    return has_organization_master_data_permission(actor, permission, organization)


class VersionListView(KitchenListView):
    """
    Every version the caller reaches, filterable by status, recipe and date.

    The history screen. It is deliberately organization-wide rather than
    per-recipe: the question an approver arrives with is "what is waiting for
    me", and that is not a question about one dish.
    """

    template_name = "kitchen/version_list.html"
    context_object_name = "versions"
    page_title = _("نسخ الوصفات")
    page_hint = _("سجل النسخ وحالاتها ومدى سريانها. الاعتماد فعل بشري باسم ووقت وسبب.")
    search_fields = ("recipe__code", "recipe__name_ar", "approval_reference")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_versions(self.actor).select_related(
            "recipe", "approved_by", "superseded_by_version"
        )
        status = self.request.GET.get("status", "").strip()
        if status in RecipeVersionStatus.values:
            queryset = queryset.filter(status=status)
        on_date = self.request.GET.get("on_date", "").strip()
        if on_date:
            try:
                asked = datetime.date.fromisoformat(on_date)
            except ValueError:
                asked = None
            if asked is not None:
                queryset = queryset.filter(effective_from__lte=asked).filter(_covers(asked))
        return queryset.order_by("recipe__code", "-version_number")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = RecipeVersionStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        context["selected_on_date"] = self.request.GET.get("on_date", "")
        return context


def _covers(on_date: datetime.date) -> Any:
    from apps.kitchen.lifecycle import covers_on_date

    return covers_on_date(on_date)


class VersionDetailView(KitchenViewMixin, View):
    """
    One version: its structure, its signatures, its effect and its audit trail.

    The panels an approver reads before signing, and the only screen from which
    the five lifecycle actions are offered.
    """

    template_name = "kitchen/version_detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        version = resolve_version(self.actor, self.kwargs["pk"])
        organization = version.recipe.organization
        context = {
            "version": version,
            "recipe": version.recipe,
            "lines": list(
                version.lines.select_related("item", "entered_unit", "package_unit")
                .prefetch_related("substitutes__substitute_item")
                .order_by("line_order")
            ),
            "steps": list(
                version.steps.prefetch_related("ingredient_links__recipe_line__item").order_by(
                    "sequence"
                )
            ),
            "servings": list(
                version.servings.select_related("serving_unit").order_by("display_order")
            ),
            "reviews": list(version.reviews.select_related("reviewer").order_by("review_type")),
            "scopes": list(version.branch_scopes.select_related("branch").order_by("branch__code")),
            "review_gaps": review_gaps(version) if version.status else [],
            "submission_problems": (submission_problems(version) if version.is_draft else []),
            "siblings": list(versions_of(version.recipe).exclude(pk=version.pk)[:20]),
            "can_submit": _held(self.actor, SUBMIT_RECIPE_VERSION, organization),
            "can_review": _held(self.actor, REVIEW_RECIPE_VERSION, organization),
            "can_approve": _held(self.actor, APPROVE_RECIPE_VERSION, organization),
            "can_reject": _held(self.actor, REJECT_RECIPE_VERSION, organization),
            "can_activate": _held(self.actor, ACTIVATE_RECIPE_VERSION, organization),
            # Expiry is derived, never stored, so the screen derives it too —
            # against today only, and labelled as a fact about a date.
            "is_expired": version.is_expired_on(timezone.localdate()),
            "page_title": f"{version.recipe.code} — {_('نسخة')} {version.version_number}",
        }
        return render(request, self.template_name, context)


class VersionTimelineView(KitchenViewMixin, View):
    """
    The review and effect timeline for one version, as an HTMX fragment.

    Full-page fallback: without the `HX-Request` header it renders inside the
    shell, so a reader with no JavaScript sees exactly the same evidence.
    """

    template_name = "kitchen/version_timeline.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        version = resolve_version(self.actor, self.kwargs["pk"])
        return render(
            request,
            self.template_name,
            {
                "version": version,
                "reviews": list(version.reviews.select_related("reviewer").order_by("reviewed_at")),
                "scopes": list(
                    version.branch_scopes.select_related("branch").order_by("branch__code")
                ),
                "timeline": version_timeline(version.recipe),
                "page_title": _("سجل الاعتماد والسريان"),
                "fragment_base_template": (
                    "kitchen/_bare.html" if self.is_htmx() else "shell.html"
                ),
            },
        )


class VersionCompareView(KitchenViewMixin, View):
    """
    What changed between two versions of one recipe.

    The other version is chosen from a select that refreshes the diff through
    HTMX; the same URL without the header renders the whole page, so the
    comparison is reachable from a bookmark.
    """

    template_name = "kitchen/version_compare.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        left = resolve_version(self.actor, self.kwargs["pk"])
        siblings = list(versions_of(left.recipe).exclude(pk=left.pk))
        chosen = request.GET.get("against", "").strip()
        right = None
        if chosen:
            right = resolve_version(self.actor, int(chosen))
        elif siblings:
            right = siblings[0]

        comparison = None
        error = None
        if right is not None:
            try:
                comparison = compare_recipe_versions(left=left, right=right)
            except ValidationError as invalid:
                error = " ".join(invalid.messages)

        return render(
            request,
            self.template_name,
            {
                "version": left,
                "left": left,
                "right": right,
                "siblings": siblings,
                "comparison": comparison,
                "error": error,
                "page_title": _("مقارنة نسختين"),
                "fragment_base_template": (
                    "kitchen/_bare.html" if self.is_htmx() else "shell.html"
                ),
            },
        )


class ResolverPreviewView(KitchenViewMixin, View):
    """
    "Which version governs this branch on this date?", asked from a screen.

    Read-only and permission-checked on both paths. The answer is whatever
    `resolve_recipe_version` says — the preview calls the same function a
    posting would, because a preview that used a different query would be a
    second implementation of the only rule that matters here.
    """

    template_name = "kitchen/resolver_preview.html"
    required_permission = VIEW_RECIPE

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        recipe = resolve_recipe(self.actor, self.kwargs["pk"])
        form = ResolverPreviewForm(request.GET or None, actor=self.actor, recipe=recipe)
        resolved = None
        problem = None
        if request.GET and form.is_valid():
            try:
                resolved = resolve_recipe_version(
                    recipe=recipe,
                    branch=form.cleaned_data["branch"],
                    on_date=form.cleaned_data["on_date"],
                )
            except ValidationError as invalid:
                problem = " ".join(invalid.messages)

        return render(
            request,
            self.template_name,
            {
                "recipe": recipe,
                "form": form,
                "resolved": resolved,
                "problem": problem,
                "timeline": version_timeline(recipe),
                "page_title": _("أي نسخة تسري؟"),
                "fragment_base_template": (
                    "kitchen/_bare.html" if self.is_htmx() else "shell.html"
                ),
            },
        )


class RecipeVersionHistoryView(KitchenViewMixin, View):
    """One recipe's whole version history, with its effective-date timeline."""

    template_name = "kitchen/recipe_versions.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        recipe = resolve_recipe(self.actor, self.kwargs["pk"])
        return render(
            request,
            self.template_name,
            {
                "recipe": recipe,
                "versions": list(versions_of(recipe)),
                "timeline": version_timeline(recipe),
                "open_statuses": sorted(OPEN_VERSION_STATUSES),
                "page_title": f"{recipe.code} — {_('سجل النسخ')}",
            },
        )


# ---------------------------------------------------------------------------
# The five actions
# ---------------------------------------------------------------------------


class VersionActionMixin(KitchenViewMixin):
    """
    Resolve the version with the caller, then check the authority.

    Never the other way round: an out-of-scope version must never exist in a
    local variable long enough for a permission check to turn it into a 403,
    because a 403 confirms the row is real.
    """

    action_permission: str = VIEW_RECIPE
    #: Supplied by `View.setup`; declared so the mixin is checkable on its own
    #: rather than only when mixed into a class that happens to provide it.
    kwargs: dict[str, Any]

    def version(self) -> RecipeVersion:
        version = resolve_version(self.actor, self.kwargs["pk"])
        require_reachable_organization_permission(
            self.actor, self.action_permission, version.recipe.organization
        )
        return version

    def detail_url(self, version: RecipeVersion) -> str:
        return reverse("kitchen:version_detail", args=[version.pk])


class VersionSubmitView(VersionActionMixin, View):
    """POST-only. Submission has no form: the draft either passes or lists why."""

    action_permission = SUBMIT_RECIPE_VERSION
    required_permission = SUBMIT_RECIPE_VERSION

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        version = self.version()
        destination = self.detail_url(version)
        try:
            submit_recipe_version(version=version, actor=self.actor)
        except ValidationError as invalid:
            messages.error(request, " ".join(invalid.messages))
            return HttpResponseRedirect(destination)
        messages.success(request, _("أُرسلت النسخة للمراجعة. بنيتها مجمّدة الآن."))
        return HttpResponseRedirect(destination)


class VersionLifecycleFormView(VersionActionMixin, KitchenWriteView):
    """One form, one lifecycle command, and the same authority on GET and POST."""

    template_name = "kitchen/form.html"

    def load(self) -> Any:
        return self.version()

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, self.action_permission, instance.recipe.organization
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return self.detail_url(instance)


class VersionReviewView(VersionLifecycleFormView):
    form_class = ReviewSignoffForm
    action_permission = REVIEW_RECIPE_VERSION
    required_permission = REVIEW_RECIPE_VERSION
    page_title = _("تسجيل مراجعة")
    page_hint = _("توقيع واحد على نسخة مُرسلة. لا يُعدَّل بعد تسجيله؛ التصحيح بنسخة جديدة.")
    success_message = _("سُجّلت المراجعة.")

    def perform(self, instance: Any, form: Any) -> Any:
        return record_recipe_version_review(
            version=instance,
            review_type=form.cleaned_data["review_type"],
            reviewer=self.actor,
            decision=form.cleaned_data["decision"],
            reason=form.cleaned_data["reason"],
            evidence_reference=form.cleaned_data["evidence_reference"],
            evidence_kind=form.cleaned_data["evidence_kind"],
            note=form.cleaned_data["note"],
        )


class VersionApproveView(VersionLifecycleFormView):
    form_class = ApproveVersionForm
    action_permission = APPROVE_RECIPE_VERSION
    required_permission = APPROVE_RECIPE_VERSION
    page_title = _("الاعتماد النهائي")
    page_hint = _("لا يعتمد كاتب النسخة ولا مُرسلها ولا أي مراجع لها. الاعتماد لا يجعلها سارية.")
    success_message = _("اعتُمدت النسخة. التفعيل أمر منفصل.")

    def perform(self, instance: Any, form: Any) -> Any:
        return approve_recipe_version(
            version=instance,
            actor=self.actor,
            approval_reference=form.cleaned_data["approval_reference"],
            approval_evidence_kind=form.cleaned_data["approval_evidence_kind"],
            note=form.cleaned_data["note"],
        )


class VersionRejectView(VersionLifecycleFormView):
    form_class = RejectVersionForm
    action_permission = REJECT_RECIPE_VERSION
    required_permission = REJECT_RECIPE_VERSION
    page_title = _("رفض النسخة")
    page_hint = _("يبقى السجل مع سببه. الرفض دليل، لا حذف.")
    success_message = _("رُفضت النسخة، وبقي السبب معها.")

    def perform(self, instance: Any, form: Any) -> Any:
        return reject_recipe_version(
            version=instance, actor=self.actor, reason=form.cleaned_data["reason"]
        )


class VersionActivateView(VersionLifecycleFormView):
    form_class = ActivateVersionForm
    action_permission = ACTIVATE_RECIPE_VERSION
    required_permission = ACTIVATE_RECIPE_VERSION
    page_title = _("تفعيل النسخة")
    page_hint = _("المدى شامل للطرفين. اتركه مفتوحاً بلا تاريخ نهاية.")
    success_message = _("أصبحت النسخة سارية.")

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {"recipe": instance.recipe}

    def perform(self, instance: Any, form: Any) -> Any:
        branches: list[Branch] | None = list(form.cleaned_data["branches"]) or None
        return activate_recipe_version(
            version=instance,
            actor=self.actor,
            effective_from=form.cleaned_data["effective_from"],
            effective_to=form.cleaned_data["effective_to"],
            branches=branches,
            supersedes=form.cleaned_data["supersedes"],
            reason=form.cleaned_data["reason"],
        )


class VersionSupersedeView(VersionLifecycleFormView):
    form_class = SupersedeVersionForm
    action_permission = ACTIVATE_RECIPE_VERSION
    required_permission = ACTIVATE_RECIPE_VERSION
    page_title = _("استبدال النسخة")
    page_hint = _("تُغلق النسخة في اليوم السابق لبداية البديلة، وتبقى قابلة للاستعلام عن أيامها.")
    success_message = _("أُغلقت النسخة واستُبدلت.")

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {"version": instance}

    def perform(self, instance: Any, form: Any) -> Any:
        return supersede_recipe_version(
            version=instance,
            replacement=form.cleaned_data["replacement"],
            actor=self.actor,
            reason=form.cleaned_data["reason"],
        )
