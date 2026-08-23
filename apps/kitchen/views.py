"""
Kitchen screens, inside the Khan Mandi shell.

Reuses the inventory and procurement scaffolding rather than inventing a third
one, so the recipe screens look and behave exactly like the screens an operator
already knows.

Two rules hold throughout, and they are the same two every module before this
one follows:

* **No view calls `form.save()`.** Every mutation goes through
  `apps/kitchen/services.py`, which re-reads the authoritative row under a
  lock, checks the invariants, and records the audit event.
* **Hiding a button is presentation, never protection.** Each write view checks
  the same authorization the service does, so a hand-made POST to an action the
  operator never saw is refused on its merits.

Every screen here edits a **draft**. There is no submit, approve or activate
button because there is no such service (Task 3.2), and the completeness panel
says so plainly rather than offering a dead control.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import ListView

if TYPE_CHECKING:
    # django-stubs types ListView generically; the runtime class is not
    # subscriptable. Same arrangement as `apps/inventory/views.py`.
    _ListView = ListView[Any]
else:
    _ListView = ListView

from apps.core.views import ModuleViewMixin
from apps.kitchen.dashboard import kitchen_overview
from apps.kitchen.forms import (
    RecipeCategoryForm,
    RecipeForm,
    RecipeLineForm,
    RecipeLineSubstituteForm,
    RecipeServingForm,
    RecipeStepForm,
    RecipeVersionForm,
    StepIngredientForm,
)
from apps.kitchen.models import RecipeType
from apps.kitchen.permissions import MANAGE_RECIPE, VIEW_RECIPE, VIEW_RECIPE_COST
from apps.kitchen.selectors import (
    manageable_organizations,
    resolve_category,
    resolve_line,
    resolve_recipe,
    resolve_serving,
    resolve_step,
    resolve_substitute,
    resolve_version,
    visible_categories,
    visible_recipes,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_line_substitute,
    add_recipe_serving,
    add_recipe_step,
    archive_recipe,
    create_draft_recipe_version,
    create_recipe,
    create_recipe_category,
    delete_draft_recipe_version,
    link_step_ingredient,
    reactivate_recipe,
    remove_recipe_line,
    remove_recipe_line_substitute,
    remove_recipe_serving,
    remove_recipe_step,
    set_recipe_branches,
    unlink_step_ingredient,
    update_draft_recipe_version,
    update_recipe,
    update_recipe_category,
    update_recipe_line,
    update_recipe_serving,
    update_recipe_step,
)
from apps.organizations.authorization import (
    organizations_with_permission,
    require_reachable_organization_permission,
)
from apps.users.models import User


class KitchenViewMixin(LoginRequiredMixin, UserPassesTestMixin, ModuleViewMixin):
    """
    Signed in, and holding the permission this screen needs.

    `required_permission` is checked globally here; the *scope* is enforced by
    the selector that builds the queryset, which is the only place that can
    know which rows the caller reaches.
    """

    module_key = "kitchen"
    required_permission: str = VIEW_RECIPE
    request: HttpRequest

    def test_func(self) -> bool:
        user = self.request.user
        return bool(user.is_authenticated and user.has_perm(self.required_permission))

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        """
        Out of scope answers **404**, not 403.

        A 403 about another organization's recipe would confirm the recipe is
        real, which turns an id-guessing loop into a census of their menu.
        """
        try:
            return super().dispatch(request, *args, **kwargs)
        except ObjectDoesNotExist as missing:
            raise Http404(str(missing)) from missing

    @property
    def actor(self) -> User:
        user: User = self.request.user  # type: ignore[assignment]
        return user

    def is_htmx(self) -> bool:
        return self.request.headers.get("HX-Request") == "true"


class KitchenListView(KitchenViewMixin, _ListView):
    """A searchable, paged list of one master-data kind, with its actions."""

    paginate_by = 25
    page_title: Any = ""
    page_hint: Any = ""
    search_fields: tuple[str, ...] = ()
    create_url_name: str | None = None
    create_label: Any = ""
    manage_permission: str = MANAGE_RECIPE

    def get_queryset(self) -> QuerySet[Any]:
        queryset = self.scoped_queryset()
        search = self.request.GET.get("q", "").strip()
        if search and self.search_fields:
            matches = Q()
            for field in self.search_fields:
                matches |= Q(**{f"{field}__icontains": search})
            queryset = queryset.filter(matches)
        return queryset

    def scoped_queryset(self) -> QuerySet[Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def manageable_ids(self) -> list[int]:
        return list(
            organizations_with_permission(self.actor, self.manage_permission).values_list(
                "id", flat=True
            )
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        manageable = self.manageable_ids()
        context["page_title"] = self.page_title
        context["page_hint"] = self.page_hint
        context["search"] = self.request.GET.get("q", "")
        context["create_label"] = self.create_label
        context["manageable_ids"] = manageable
        context["htmx_list"] = True
        context["list_base_template"] = (
            "settings/_list_fragment.html" if self.is_htmx() else "shell.html"
        )
        context["create_url"] = (
            reverse(self.create_url_name) if self.create_url_name and manageable else None
        )
        return context


class RecipeListView(KitchenListView):
    template_name = "kitchen/recipe_list.html"
    context_object_name = "recipes"
    page_title = _("الوصفات")
    page_hint = _("سجل الوصفات على مستوى المؤسسة. كل النسخ هنا مسودات — الاعتماد يأتي في مهمة 3.2.")
    search_fields = ("code", "name_ar", "name_en")
    create_url_name = "kitchen:recipe_create"
    create_label = _("وصفة جديدة")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_recipes(self.actor)
        recipe_type = self.request.GET.get("recipe_type", "").strip()
        if recipe_type in RecipeType.values:
            queryset = queryset.filter(recipe_type=recipe_type)
        state = self.request.GET.get("state", "").strip()
        if state == "active":
            queryset = queryset.filter(is_active=True)
        elif state == "archived":
            queryset = queryset.filter(is_active=False)
        return queryset.order_by("code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["recipe_types"] = RecipeType.choices
        context["selected_recipe_type"] = self.request.GET.get("recipe_type", "")
        context["selected_state"] = self.request.GET.get("state", "")
        return context


class RecipeCategoryListView(KitchenListView):
    template_name = "kitchen/category_list.html"
    context_object_name = "categories"
    page_title = _("مجموعات الوصفات")
    page_hint = _("تصنيف الأطباق كما يسجله نموذج اعتماد الأصناف المعتمد في الفرع.")
    search_fields = ("code", "name_ar", "name_en")
    create_url_name = "kitchen:category_create"
    create_label = _("مجموعة جديدة")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_categories(self.actor).order_by("code")


class KitchenWriteView(KitchenViewMixin, View):
    """
    One form, one service call.

    `authorize` runs before `perform`, and both run again on every POST — the
    template's decision to show a button is never the control.
    """

    template_name = "kitchen/form.html"
    form_class: Any = None
    required_permission = MANAGE_RECIPE
    page_title: Any = ""
    page_hint: Any = ""
    success_message: Any = ""

    def load(self) -> Any:
        return None

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {}

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {}

    def authorize(self, instance: Any, form: Any) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def perform(self, instance: Any, form: Any) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def success_url(self, instance: Any, result: Any) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def build_form(self, instance: Any, data: Any = None) -> Any:
        kwargs: dict[str, Any] = {"actor": self.actor, **self.form_kwargs(instance)}
        if data is not None:
            return self.form_class(data, **kwargs)
        return self.form_class(initial=self.initial_for(instance), **kwargs)

    def render(self, instance: Any, form: Any, status: int = 200) -> HttpResponse:
        return render(
            self.request,
            self.template_name,
            {
                "form": form,
                "page_title": self.page_title,
                "page_hint": self.page_hint,
                "instance": instance,
            },
            status=status,
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        instance = self.load()
        return self.render(instance, self.build_form(instance))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        instance = self.load()
        form = self.build_form(instance, data=request.POST)
        if not form.is_valid():
            return self.render(instance, form, status=200)
        try:
            self.authorize(instance, form)
            result = self.perform(instance, form)
        except ValidationError as invalid:
            for message in invalid.messages:
                form.add_error(None, message)
            return self.render(instance, form, status=200)
        if self.success_message:
            messages.success(request, self.success_message)
        return HttpResponseRedirect(self.success_url(instance, result))


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class RecipeCategoryCreateView(KitchenWriteView):
    form_class = RecipeCategoryForm
    page_title = _("مجموعة وصفات جديدة")
    success_message = _("تمت إضافة المجموعة.")

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {"instance": instance}

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> Any:
        return create_recipe_category(
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            notes=form.cleaned_data["notes"],
            source_document=form.cleaned_data["source_document"],
            source_page=form.cleaned_data["source_page"],
            source_reference=form.cleaned_data["source_reference"],
            source_note=form.cleaned_data["source_note"],
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:category_list")


class RecipeCategoryUpdateView(RecipeCategoryCreateView):
    page_title = _("تعديل المجموعة")
    success_message = _("تم حفظ المجموعة.")

    def load(self) -> Any:
        return resolve_category(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "organization": instance.organization,
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "notes": instance.notes,
            "is_active": instance.is_active,
            "source_document": instance.source_document,
            "source_page": instance.source_page,
            "source_reference": instance.source_reference,
            "source_note": instance.source_note,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(self.actor, MANAGE_RECIPE, instance.organization)

    def perform(self, instance: Any, form: Any) -> Any:
        return update_recipe_category(
            category=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            notes=form.cleaned_data["notes"],
            is_active=form.cleaned_data["is_active"],
        )


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


class RecipeCreateView(KitchenWriteView):
    form_class = RecipeForm
    page_title = _("وصفة جديدة")
    page_hint = _("وصفة الدفعة تنتج صنفاً مخزنياً؛ وصفة الحصة طبق يُجهَّز عند الطلب.")
    success_message = _("تمت إضافة الوصفة.")

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {"instance": instance}

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> Any:
        recipe = create_recipe(
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            recipe_type=form.cleaned_data["recipe_type"],
            category=form.cleaned_data["category"],
            output_item=form.cleaned_data["output_item"],
            description_ar=form.cleaned_data["description_ar"],
            description_en=form.cleaned_data["description_en"],
            notes=form.cleaned_data["notes"],
            created_by=self.actor,
            source_document=form.cleaned_data["source_document"],
            source_page=form.cleaned_data["source_page"],
            source_reference=form.cleaned_data["source_reference"],
            source_note=form.cleaned_data["source_note"],
        )
        set_recipe_branches(recipe=recipe, branches=list(form.cleaned_data["branches"]))
        return recipe

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[result.pk])


class RecipeUpdateView(RecipeCreateView):
    page_title = _("تعديل الوصفة")
    success_message = _("تم حفظ الوصفة.")

    def load(self) -> Any:
        return resolve_recipe(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "organization": instance.organization,
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "recipe_type": instance.recipe_type,
            "category": instance.category,
            "output_item": instance.output_item,
            "description_ar": instance.description_ar,
            "description_en": instance.description_en,
            "notes": instance.notes,
            "branches": [row.branch for row in instance.branch_applicability.all()],
            "source_document": instance.source_document,
            "source_page": instance.source_page,
            "source_reference": instance.source_reference,
            "source_note": instance.source_note,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(self.actor, MANAGE_RECIPE, instance.organization)

    def perform(self, instance: Any, form: Any) -> Any:
        recipe = update_recipe(
            recipe=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            category=form.cleaned_data["category"],
            output_item=form.cleaned_data["output_item"],
            description_ar=form.cleaned_data["description_ar"],
            description_en=form.cleaned_data["description_en"],
            notes=form.cleaned_data["notes"],
        )
        set_recipe_branches(recipe=recipe, branches=list(form.cleaned_data["branches"]))
        return recipe


class RecipeDetailView(KitchenViewMixin, View):
    """
    The recipe workspace: master data, the draft, its lines, steps and servings.

    Everything an operator does to a draft happens from here, and every panel
    is its own HTMX fragment so editing one does not redraw the others.
    """

    template_name = "kitchen/recipe_detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        recipe = resolve_recipe(self.actor, self.kwargs["pk"])
        draft = recipe.versions.select_related("output_unit").first()
        can_manage = manageable_organizations(self.actor).filter(pk=recipe.organization_id).exists()

        context: dict[str, Any] = {
            "recipe": recipe,
            "draft": draft,
            "can_manage": can_manage,
            "branches": [
                row.branch for row in recipe.branch_applicability.select_related("branch")
            ],
            "page_title": f"{recipe.code} — {recipe.name_ar}",
        }
        if draft is not None:
            lines = list(
                draft.lines.select_related("item", "entered_unit", "package_unit")
                .prefetch_related("substitutes__substitute_item")
                .order_by("line_order")
            )
            context |= {
                "lines": lines,
                "steps": list(
                    draft.steps.prefetch_related("ingredient_links__recipe_line__item").order_by(
                        "sequence"
                    )
                ),
                "servings": list(
                    draft.servings.select_related("serving_unit").order_by("display_order")
                ),
                "completeness": _completeness(draft),
            }
        return render(request, self.template_name, context)


def _completeness(draft: Any) -> list[dict[str, Any]]:
    """
    What is still missing from this draft, stated plainly.

    Not a validation gate — Task 3.1 has nothing to gate — but the honest
    answer to "is this ready", which is the question a chef actually asks. A
    version with an overview and no steps is a version whose method has not
    been captured, and the screen says so rather than pretending the paragraph
    is a procedure (RCP-063).
    """
    lines = draft.lines.count()
    steps = draft.steps.count()
    servings = draft.servings.count()
    return [
        {"label": _("مكوّنات"), "count": lines, "ok": lines > 0},
        {"label": _("خطوات الطريقة"), "count": steps, "ok": steps > 0},
        {"label": _("تعريفات الحصص"), "count": servings, "ok": servings > 0},
        {
            "label": _("مصدر موثّق"),
            "count": 1 if draft.has_source else 0,
            "ok": draft.has_source,
        },
    ]


class KitchenActionView(KitchenViewMixin, View):
    """A POST-only action with no form of its own."""

    required_permission = MANAGE_RECIPE
    success_message: Any = ""

    def target(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def organization_of(self, target: Any) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def act(self, target: Any) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def redirect_to(self, target: Any) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        target = self.target()
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, self.organization_of(target)
        )
        destination = self.redirect_to(target)
        try:
            self.act(target)
        except ValidationError as invalid:
            messages.error(request, " ".join(invalid.messages))
            return HttpResponseRedirect(destination)
        if self.success_message:
            messages.success(request, self.success_message)
        return HttpResponseRedirect(destination)


class RecipeArchiveView(KitchenActionView):
    success_message = _("تمت أرشفة الوصفة. الرمز يبقى محجوزاً.")

    def target(self) -> Any:
        return resolve_recipe(self.actor, self.kwargs["pk"])

    def organization_of(self, target: Any) -> Any:
        return target.organization

    def act(self, target: Any) -> None:
        archive_recipe(recipe=target, reason=self.request.POST.get("reason", ""))

    def redirect_to(self, target: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[target.pk])


class RecipeReactivateView(RecipeArchiveView):
    success_message = _("تمت إعادة تفعيل الوصفة.")

    def act(self, target: Any) -> None:
        reactivate_recipe(recipe=target, reason=self.request.POST.get("reason", ""))


# ---------------------------------------------------------------------------
# Draft versions and their children
# ---------------------------------------------------------------------------


class DraftVersionCreateView(KitchenWriteView):
    form_class = RecipeVersionForm
    page_title = _("مسودة نسخة جديدة")
    page_hint = _("النسخة تبقى مسودة. الاعتماد والتفعيل والتأريخ الفعّال من مهمة 3.2.")
    success_message = _("تم فتح مسودة النسخة.")

    def load(self) -> Any:
        return resolve_recipe(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(self.actor, MANAGE_RECIPE, instance.organization)

    def perform(self, instance: Any, form: Any) -> Any:
        return create_draft_recipe_version(
            recipe=instance,
            batch_size=form.cleaned_data["batch_size"],
            expected_output_quantity=form.cleaned_data["expected_output_quantity"],
            output_unit=form.cleaned_data["output_unit"],
            preparation_loss=form.cleaned_data["preparation_loss"],
            cooking_yield=form.cleaned_data["cooking_yield"],
            instructions=form.cleaned_data["instructions"],
            notes=form.cleaned_data["notes"],
            created_by=self.actor,
            source_document=form.cleaned_data["source_document"],
            source_page=form.cleaned_data["source_page"],
            source_reference=form.cleaned_data["source_reference"],
            source_note=form.cleaned_data["source_note"],
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[instance.pk])


class DraftVersionUpdateView(KitchenWriteView):
    form_class = RecipeVersionForm
    page_title = _("تعديل مسودة النسخة")
    success_message = _("تم حفظ المسودة.")

    def load(self) -> Any:
        return resolve_version(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "batch_size": instance.batch_size,
            "expected_output_quantity": instance.expected_output_quantity,
            "output_unit": instance.output_unit,
            "preparation_loss": instance.preparation_loss,
            "cooking_yield": instance.cooking_yield,
            "instructions": instance.instructions,
            "notes": instance.notes,
            "source_document": instance.source_document,
            "source_page": instance.source_page,
            "source_reference": instance.source_reference,
            "source_note": instance.source_note,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, instance.recipe.organization
        )

    def perform(self, instance: Any, form: Any) -> Any:
        return update_draft_recipe_version(
            version=instance,
            batch_size=form.cleaned_data["batch_size"],
            expected_output_quantity=form.cleaned_data["expected_output_quantity"],
            output_unit=form.cleaned_data["output_unit"],
            preparation_loss=form.cleaned_data["preparation_loss"],
            cooking_yield=form.cleaned_data["cooking_yield"],
            instructions=form.cleaned_data["instructions"],
            notes=form.cleaned_data["notes"],
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[instance.recipe_id])


class DraftVersionDiscardView(KitchenActionView):
    success_message = _("تم إلغاء المسودة. رقم النسخة لا يُعاد استخدامه.")

    def target(self) -> Any:
        return resolve_version(self.actor, self.kwargs["pk"])

    def organization_of(self, target: Any) -> Any:
        return target.recipe.organization

    def act(self, target: Any) -> None:
        delete_draft_recipe_version(version=target, reason=self.request.POST.get("reason", ""))

    def redirect_to(self, target: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[target.recipe_id])


class LineCreateView(KitchenWriteView):
    form_class = RecipeLineForm
    page_title = _("إضافة مكوّن")
    success_message = _("تمت إضافة المكوّن.")

    def load(self) -> Any:
        return resolve_version(self.actor, self.kwargs["pk"])

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {"organization": instance.recipe.organization}

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, instance.recipe.organization
        )

    def perform(self, instance: Any, form: Any) -> Any:
        return add_recipe_line(
            version=instance,
            item=form.cleaned_data["item"],
            entered_quantity=form.cleaned_data["entered_quantity"],
            entered_unit=form.cleaned_data["entered_unit"],
            package_unit=form.cleaned_data["package_unit"],
            measured_base_quantity=form.cleaned_data["measured_base_quantity"],
            measured_quantity=form.cleaned_data["measured_quantity"],
            loss_rate=form.cleaned_data["loss_rate"],
            cost_class=form.cleaned_data["cost_class"],
            preparation_stage=form.cleaned_data["preparation_stage"],
            measurement_basis=form.cleaned_data["measurement_basis"],
            is_optional=form.cleaned_data["is_optional"],
            note=form.cleaned_data["note"],
            source_document=form.cleaned_data["source_document"],
            source_page=form.cleaned_data["source_page"],
            source_reference=form.cleaned_data["source_reference"],
            source_note=form.cleaned_data["source_note"],
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[instance.recipe_id])


class LineUpdateView(KitchenWriteView):
    form_class = RecipeLineForm
    page_title = _("تعديل المكوّن")
    success_message = _("تم حفظ المكوّن.")

    def load(self) -> Any:
        return resolve_line(self.actor, self.kwargs["pk"])

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {"organization": instance.version.recipe.organization}

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "item": instance.item,
            "entered_quantity": instance.entered_quantity,
            "entered_unit": instance.entered_unit,
            "package_unit": instance.package_unit,
            "measured_base_quantity": instance.base_quantity,
            "measured_quantity": instance.measured_quantity,
            "loss_rate": instance.loss_rate,
            "cost_class": instance.cost_class,
            "preparation_stage": instance.preparation_stage,
            "measurement_basis": instance.measurement_basis,
            "is_optional": instance.is_optional,
            "note": instance.note,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, instance.version.recipe.organization
        )

    def perform(self, instance: Any, form: Any) -> Any:
        return update_recipe_line(
            line=instance,
            entered_quantity=form.cleaned_data["entered_quantity"],
            entered_unit=form.cleaned_data["entered_unit"],
            package_unit=form.cleaned_data["package_unit"],
            measured_base_quantity=form.cleaned_data["measured_base_quantity"],
            measured_quantity=form.cleaned_data["measured_quantity"],
            loss_rate=form.cleaned_data["loss_rate"],
            cost_class=form.cleaned_data["cost_class"],
            preparation_stage=form.cleaned_data["preparation_stage"],
            measurement_basis=form.cleaned_data["measurement_basis"],
            is_optional=form.cleaned_data["is_optional"],
            note=form.cleaned_data["note"],
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[instance.version.recipe_id])


class LineDeleteView(KitchenActionView):
    success_message = _("تم حذف المكوّن.")

    def target(self) -> Any:
        return resolve_line(self.actor, self.kwargs["pk"])

    def organization_of(self, target: Any) -> Any:
        return target.version.recipe.organization

    def act(self, target: Any) -> None:
        remove_recipe_line(line=target, reason=self.request.POST.get("reason", ""))

    def redirect_to(self, target: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[target.version.recipe_id])


class SubstituteCreateView(KitchenWriteView):
    form_class = RecipeLineSubstituteForm
    page_title = _("إضافة بديل")
    page_hint = _("قائمة استرشادية. لا يستبدل النظام شيئاً تلقائياً.")
    success_message = _("تمت إضافة البديل.")

    def load(self) -> Any:
        return resolve_line(self.actor, self.kwargs["pk"])

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {"organization": instance.version.recipe.organization}

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, instance.version.recipe.organization
        )

    def perform(self, instance: Any, form: Any) -> Any:
        return add_recipe_line_substitute(
            line=instance,
            substitute_item=form.cleaned_data["substitute_item"],
            priority=form.cleaned_data["priority"],
            reason=form.cleaned_data["reason"],
            note=form.cleaned_data["note"],
            source_document=form.cleaned_data["source_document"],
            source_page=form.cleaned_data["source_page"],
            source_reference=form.cleaned_data["source_reference"],
            source_note=form.cleaned_data["source_note"],
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[instance.version.recipe_id])


class SubstituteDeleteView(KitchenActionView):
    success_message = _("تم حذف البديل.")

    def target(self) -> Any:
        return resolve_substitute(self.actor, self.kwargs["pk"])

    def organization_of(self, target: Any) -> Any:
        return target.line.version.recipe.organization

    def act(self, target: Any) -> None:
        remove_recipe_line_substitute(substitute=target, reason=self.request.POST.get("reason", ""))

    def redirect_to(self, target: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[target.line.version.recipe_id])


class StepCreateView(KitchenWriteView):
    form_class = RecipeStepForm
    page_title = _("إضافة خطوة")
    page_hint = _("الحرارة الرقمية تبقى فارغة ما لم يذكرها المصدر. تعليمات النار تُكتب نصاً.")
    success_message = _("تمت إضافة الخطوة.")

    def load(self) -> Any:
        return resolve_version(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, instance.recipe.organization
        )

    def perform(self, instance: Any, form: Any) -> Any:
        return add_recipe_step(
            version=instance,
            sequence=form.cleaned_data["sequence"],
            instruction_ar=form.cleaned_data["instruction_ar"],
            instruction_en=form.cleaned_data["instruction_en"],
            stage=form.cleaned_data["stage"],
            expected_duration=form.expected_duration,
            temperature_c=form.cleaned_data["temperature_c"],
            heat_instruction_ar=form.cleaned_data["heat_instruction_ar"],
            checkpoint_ar=form.cleaned_data["checkpoint_ar"],
            is_critical=form.cleaned_data["is_critical"],
            media_reference=form.cleaned_data["media_reference"],
            note=form.cleaned_data["note"],
            source_document=form.cleaned_data["source_document"],
            source_page=form.cleaned_data["source_page"],
            source_reference=form.cleaned_data["source_reference"],
            source_note=form.cleaned_data["source_note"],
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[instance.recipe_id])


class StepUpdateView(KitchenWriteView):
    form_class = RecipeStepForm
    page_title = _("تعديل الخطوة")
    success_message = _("تم حفظ الخطوة.")

    def load(self) -> Any:
        return resolve_step(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        minutes = (
            int(instance.expected_duration.total_seconds() // 60)
            if instance.expected_duration
            else None
        )
        return {
            "sequence": instance.sequence,
            "instruction_ar": instance.instruction_ar,
            "instruction_en": instance.instruction_en,
            "stage": instance.stage,
            "expected_minutes": minutes,
            "temperature_c": instance.temperature_c,
            "heat_instruction_ar": instance.heat_instruction_ar,
            "checkpoint_ar": instance.checkpoint_ar,
            "is_critical": instance.is_critical,
            "media_reference": instance.media_reference,
            "note": instance.note,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, instance.version.recipe.organization
        )

    def perform(self, instance: Any, form: Any) -> Any:
        return update_recipe_step(
            step=instance,
            sequence=form.cleaned_data["sequence"],
            instruction_ar=form.cleaned_data["instruction_ar"],
            instruction_en=form.cleaned_data["instruction_en"],
            stage=form.cleaned_data["stage"],
            expected_duration=form.expected_duration,
            temperature_c=form.cleaned_data["temperature_c"],
            heat_instruction_ar=form.cleaned_data["heat_instruction_ar"],
            checkpoint_ar=form.cleaned_data["checkpoint_ar"],
            is_critical=form.cleaned_data["is_critical"],
            media_reference=form.cleaned_data["media_reference"],
            note=form.cleaned_data["note"],
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[instance.version.recipe_id])


class StepDeleteView(KitchenActionView):
    success_message = _("تم حذف الخطوة. كميات المكوّنات لم تتغير.")

    def target(self) -> Any:
        return resolve_step(self.actor, self.kwargs["pk"])

    def organization_of(self, target: Any) -> Any:
        return target.version.recipe.organization

    def act(self, target: Any) -> None:
        remove_recipe_step(step=target, reason=self.request.POST.get("reason", ""))

    def redirect_to(self, target: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[target.version.recipe_id])


class StepLinkView(KitchenWriteView):
    form_class = StepIngredientForm
    page_title = _("ربط مكوّن بخطوة")
    page_hint = _("توثيق لوقت الإضافة فقط. لا يغيّر أي كمية ولا أي كلفة.")
    success_message = _("تم الربط.")

    def load(self) -> Any:
        return resolve_step(self.actor, self.kwargs["pk"])

    def form_kwargs(self, instance: Any) -> dict[str, Any]:
        return {"version_id": instance.version_id}

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, instance.version.recipe.organization
        )

    def perform(self, instance: Any, form: Any) -> Any:
        return link_step_ingredient(
            step=instance,
            recipe_line=form.cleaned_data["recipe_line"],
            share=form.cleaned_data["share"],
            note=form.cleaned_data["note"],
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[instance.version.recipe_id])


class StepUnlinkView(KitchenActionView):
    success_message = _("تم فك الربط. كمية المكوّن كما هي.")

    def target(self) -> Any:
        from apps.kitchen.selectors import visible_step_ingredients

        link = visible_step_ingredients(self.actor).filter(pk=self.kwargs["pk"]).first()
        if link is None:
            raise Http404("RecipeStepIngredient does not exist.")
        return link

    def organization_of(self, target: Any) -> Any:
        return target.step.version.recipe.organization

    def act(self, target: Any) -> None:
        unlink_step_ingredient(link=target, reason=self.request.POST.get("reason", ""))

    def redirect_to(self, target: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[target.step.version.recipe_id])


class ServingCreateView(KitchenWriteView):
    form_class = RecipeServingForm
    page_title = _("إضافة تعريف حصة")
    page_hint = _("الحصة تقسّم الناتج. تكلفة الطبق التجاري وصفة مستقلة، لا ضِعف حصة.")
    success_message = _("تمت إضافة الحصة.")

    def load(self) -> Any:
        return resolve_version(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, instance.recipe.organization
        )

    def perform(self, instance: Any, form: Any) -> Any:
        return add_recipe_serving(
            version=instance,
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            serving_quantity=form.cleaned_data["serving_quantity"],
            serving_unit=form.cleaned_data["serving_unit"],
            is_primary=form.cleaned_data["is_primary"],
            rounding_increment=form.cleaned_data["rounding_increment"],
            rounding_policy=form.cleaned_data["rounding_policy"],
            measurement_basis=form.cleaned_data["measurement_basis"],
            display_order=form.cleaned_data["display_order"],
            source_document=form.cleaned_data["source_document"],
            source_page=form.cleaned_data["source_page"],
            source_reference=form.cleaned_data["source_reference"],
            source_note=form.cleaned_data["source_note"],
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[instance.recipe_id])


class ServingUpdateView(KitchenWriteView):
    form_class = RecipeServingForm
    page_title = _("تعديل الحصة")
    success_message = _("تم حفظ الحصة.")

    def load(self) -> Any:
        return resolve_serving(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "serving_quantity": instance.serving_quantity,
            "serving_unit": instance.serving_unit,
            "is_primary": instance.is_primary,
            "rounding_increment": instance.rounding_increment,
            "rounding_policy": instance.rounding_policy,
            "measurement_basis": instance.measurement_basis,
            "display_order": instance.display_order,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_RECIPE, instance.version.recipe.organization
        )

    def perform(self, instance: Any, form: Any) -> Any:
        return update_recipe_serving(
            serving=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            serving_quantity=form.cleaned_data["serving_quantity"],
            serving_unit=form.cleaned_data["serving_unit"],
            is_primary=form.cleaned_data["is_primary"],
            rounding_increment=form.cleaned_data["rounding_increment"],
            rounding_policy=form.cleaned_data["rounding_policy"],
            measurement_basis=form.cleaned_data["measurement_basis"],
            display_order=form.cleaned_data["display_order"],
            is_active=form.cleaned_data["is_active"],
        )

    def success_url(self, instance: Any, result: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[instance.version.recipe_id])


class ServingDeleteView(KitchenActionView):
    success_message = _("تم حذف الحصة.")

    def target(self) -> Any:
        return resolve_serving(self.actor, self.kwargs["pk"])

    def organization_of(self, target: Any) -> Any:
        return target.version.recipe.organization

    def act(self, target: Any) -> None:
        remove_recipe_serving(serving=target, reason=self.request.POST.get("reason", ""))

    def redirect_to(self, target: Any) -> str:
        return reverse("kitchen:recipe_detail", args=[target.version.recipe_id])


class KitchenOverviewView(KitchenViewMixin, View):
    """
    The module's opening screen: the recipe estate and its approval pipeline.

    No live plate cost appears here — a cost needs a named version, warehouse
    and date, and this screen names none of them. The cost panel shows stored
    `RecipeCostSnapshot` rows instead, behind the same permission the cost
    card itself requires, omitted rather than zeroed for everyone else.
    """

    required_permission = VIEW_RECIPE
    template_name = "kitchen/overview.html"

    @property
    def include_cost(self) -> bool:
        return bool(self.request.user.has_perm(VIEW_RECIPE_COST))

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        overview = kitchen_overview(self.actor, include_cost=self.include_cost)
        return render(
            request,
            self.template_name,
            {
                "overview": overview,
                "show_cost": self.include_cost,
                "page_title": _("نظرة عامة على المطبخ"),
                "page_hint": _("الوصفات ونسخها ومسار اعتمادها."),
            },
        )
