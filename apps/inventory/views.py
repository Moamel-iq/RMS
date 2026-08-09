"""
Inventory master-data screens, inside the Khan Mandi shell.

Reuses the foundation list/form scaffolding rather than inventing a second
one, so the inventory screens look and behave exactly like the settings
screens an operator already knows.

Three differences from the foundation screens, all deliberate:

* Access is by **inventory permission**, not by staff flag. A storekeeper is
  not staff and must still see the item master.
* Every queryset is scoped through `apps/inventory/selectors.py`, so an
  organization's items are invisible to anyone outside it — never filtered in
  the template.
* No view calls `form.save()`. Every mutation goes through
  `apps/inventory/services.py`, which re-reads the authoritative row, checks
  the invariants, and records the audit event. A bound `ModelForm` mutates its
  instance during validation, so a "before" snapshot taken from it would
  already hold the new values.

Hiding a button is presentation, never protection. Each write view checks the
same authorization the service does, so a hand-made POST to a hidden action is
refused on its merits and not on whether the operator saw a link.
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
    # subscriptable. Same arrangement as `apps/accounting/admin.py`.
    _ListView = ListView[Any]
else:
    _ListView = ListView

from apps.core.views import ModuleViewMixin
from apps.inventory.forms import (
    InventoryItemForm,
    ItemCategoryForm,
    ItemConversionForm,
    PackageUnitForm,
    SupersedeConversionForm,
    WarehouseForm,
)
from apps.inventory.models import ItemType
from apps.inventory.permissions import (
    MANAGE_CATEGORIES,
    MANAGE_CONVERSIONS,
    MANAGE_ITEMS,
    MANAGE_PACKAGE_UNITS,
    MANAGE_WAREHOUSES,
    VIEW_ITEM,
)
from apps.inventory.selectors import (
    readable_warehouses,
    resolve_category,
    resolve_conversion,
    resolve_item,
    resolve_manageable_warehouse,
    resolve_package_unit,
    visible_categories,
    visible_conversions,
    visible_items,
    visible_package_units,
)
from apps.inventory.services import (
    create_item,
    create_item_category,
    create_item_conversion,
    create_package_unit,
    create_warehouse,
    supersede_item_conversion,
    update_item,
    update_item_category,
    update_item_conversion,
    update_package_unit,
    update_warehouse,
)
from apps.organizations.authorization import (
    branches_with_permission,
    organizations_with_permission,
    require_branch_permission,
    require_reachable_organization_permission,
)
from apps.users.models import User


class InventoryViewMixin(LoginRequiredMixin, UserPassesTestMixin, ModuleViewMixin):
    """
    Signed in, and holding the permission this screen needs.

    `required_permission` is checked globally here; the *scope* is enforced by
    the selector that builds the queryset, which is the only place that can
    know which rows the caller reaches.
    """

    module_key = "inventory"
    required_permission: str = VIEW_ITEM
    #: Declared for the type checker: the mixin is always combined with a view.
    request: HttpRequest

    def test_func(self) -> bool:
        user = self.request.user
        return bool(user.is_authenticated and user.has_perm(self.required_permission))

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        """
        Out of scope answers **404**, not 403.

        `OutOfScope` is an `ObjectDoesNotExist`, and a 403 about another
        organization's record would confirm that the record is real — which
        turns an id-guessing loop into a census of their item master. Absent
        and foreign must be indistinguishable.
        """
        try:
            return super().dispatch(request, *args, **kwargs)
        except ObjectDoesNotExist as missing:
            raise Http404(str(missing)) from missing

    @property
    def actor(self) -> User:
        """The signed-in caller. `test_func` has already refused anonymity."""
        user: User = self.request.user  # type: ignore[assignment]
        return user


class InventoryListView(InventoryViewMixin, _ListView):
    """
    A searchable, paged list of one master-data kind, with its actions.

    The action buttons are decided **per row**, from the organization or
    branch that row belongs to, because a caller can legitimately manage the
    item master of one organization while only reading another's. A single
    page-level "can manage" flag would be wrong in exactly that case, and
    wrong in the permissive direction.
    """

    paginate_by = 25
    page_title: Any = ""
    page_hint: Any = ""
    search_fields: tuple[str, ...] = ()
    create_url_name: str | None = None
    create_label: Any = ""
    #: The permission that gates create, edit, archive, and reactivate here.
    manage_permission: str = ""
    #: Whether that permission is answered per organization or per branch.
    manage_scope: str = "organization"

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
        """Organizations, or branches, where this caller may write."""
        if not self.manage_permission:
            return []
        if self.manage_scope == "branch":
            return list(
                branches_with_permission(self.actor, self.manage_permission).values_list(
                    "id", flat=True
                )
            )
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
        # No place to create it means no button. The create view refuses the
        # same request anyway; this only avoids offering a dead end.
        context["create_url"] = (
            reverse(self.create_url_name) if self.create_url_name and manageable else None
        )
        return context


class ItemCategoryListView(InventoryListView):
    template_name = "inventory/category_list.html"
    context_object_name = "categories"
    page_title = _("مجموعات الأصناف")
    page_hint = _(
        "ثلاثة مستويات كحد أقصى. الأصناف تُربط بالمستوى الأخير فقط، حتى تبقى تقارير المجموعات صحيحة."
    )
    search_fields = ("code", "name_ar", "name_en")
    manage_permission = MANAGE_CATEGORIES
    create_url_name = "inventory:category_create"
    create_label = _("مجموعة جديدة")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_categories(self.actor).order_by("depth", "code")


class PackageUnitListView(InventoryListView):
    template_name = "inventory/package_unit_list.html"
    context_object_name = "package_units"
    page_title = _("وحدات التعبئة")
    page_hint = _(
        "الكرتونة والكيس والعلبة. لا تحمل معامل تحويل عام — كرتونة الدجاج "
        "ليست كرتونة الزيت، والمعامل يُسجَّل لكل صنف على حدة."
    )
    search_fields = ("code", "name_ar", "name_en")
    manage_permission = MANAGE_PACKAGE_UNITS
    create_url_name = "inventory:package_unit_create"
    create_label = _("وحدة تعبئة جديدة")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_package_units(self.actor).order_by("code")


class ItemListView(InventoryListView):
    template_name = "inventory/item_list.html"
    context_object_name = "items"
    page_title = _("الأصناف")
    page_hint = _("سجل الأصناف على مستوى المؤسسة، مشترك بين الفروع.")
    search_fields = ("code", "name_ar", "name_en")
    manage_permission = MANAGE_ITEMS
    create_url_name = "inventory:item_create"
    create_label = _("صنف جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_items(self.actor)
        item_type = self.request.GET.get("item_type", "").strip()
        if item_type in ItemType.values:
            queryset = queryset.filter(item_type=item_type)
        return queryset.order_by("code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["item_types"] = ItemType.choices
        context["selected_item_type"] = self.request.GET.get("item_type", "")
        return context


class ItemConversionListView(InventoryListView):
    template_name = "inventory/conversion_list.html"
    context_object_name = "conversions"
    page_title = _("تحويلات وحدات الصنف")
    page_hint = _(
        "كم وحدة أساس في العبوة الواحدة، لكل صنف. التحويل يصل مباشرة إلى وحدة "
        "أساس الصنف — بلا سلاسل."
    )
    search_fields = ("item__code", "item__name_ar", "package_unit__code")
    manage_permission = MANAGE_CONVERSIONS
    create_url_name = "inventory:conversion_create"
    create_label = _("تحويل جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_conversions(self.actor).order_by(
            "item__code", "package_unit__code", "-effective_from"
        )


class WarehouseListView(InventoryListView):
    template_name = "inventory/warehouse_list.html"
    context_object_name = "warehouses"
    page_title = _("المخازن")
    page_hint = _("المخزن يتبع فرعاً واحداً، وهو الذي يملك قيمة المخزون.")
    required_permission = MANAGE_WAREHOUSES
    search_fields = ("code", "name_ar", "name_en")
    manage_permission = MANAGE_WAREHOUSES
    manage_scope = "branch"
    create_url_name = "inventory:warehouse_create"
    create_label = _("مخزن جديد")

    def scoped_queryset(self) -> QuerySet[Any]:
        # Archived warehouses stay readable — their codes are still reserved
        # and their history is still referenced. `visible_warehouses` answers
        # custody, which is only ever about active ones.
        return readable_warehouses(self.actor).order_by("branch__code", "code")


# ---------------------------------------------------------------------------
# Write screens
# ---------------------------------------------------------------------------


class InventoryWriteView(InventoryViewMixin, View):
    """
    Base for the create and edit screens.

    `test_func` is the coarse gate — it answers "may this person manage this
    kind of thing at all". It is never the authorization: the scoped check
    against the specific organization or branch happens in `authorize`, after
    the target is known, and it is what the service would enforce anyway.

    Nothing here calls `form.save()`.
    """

    template_name = "inventory/master_form.html"
    form_class: Any = None
    page_title: Any = ""
    page_hint: Any = ""
    success_message: Any = _("تم الحفظ.")
    success_url_name: str = ""
    required_permission: str = ""

    def get_success_url(self) -> str:
        return reverse(self.success_url_name)

    # -- overridden per screen ---------------------------------------------

    def load(self) -> Any:
        """The row being edited, or None when creating."""
        return None

    def initial_for(self, instance: Any) -> dict[str, Any]:  # pragma: no cover - overridden
        return {}

    def authorize(self, instance: Any, form: Any) -> None:  # pragma: no cover - overridden
        """Raise unless the caller may write here. Runs before the service."""
        raise NotImplementedError

    def perform(self, instance: Any, form: Any) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- request handling ---------------------------------------------------

    def build_form(self, instance: Any, data: Any = None) -> Any:
        kwargs: dict[str, Any] = {"actor": self.actor, "instance": instance}
        if instance is not None:
            kwargs["initial"] = self.initial_for(instance)
        if data is not None:
            kwargs["data"] = data
        return self.form_class(**kwargs)

    def context(self, instance: Any, form: Any) -> dict[str, Any]:
        return {
            "form": form,
            "page_title": self.page_title,
            "page_hint": self.page_hint,
            "cancel_url": self.get_success_url(),
            "instance": instance,
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        instance = self.load()
        return render(
            request, self.template_name, self.context(instance, self.build_form(instance))
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        instance = self.load()
        form = self.build_form(instance, data=request.POST)
        if form.is_valid():
            try:
                self.authorize(instance, form)
                self.perform(instance, form)
            except ValidationError as error:
                # A domain rule the form could not know about — a cycle, a
                # period overlap, a protected system row. Rendered as a form
                # error in Arabic rather than as a 500.
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, self.success_message)
                return HttpResponseRedirect(self.get_success_url())
        return render(request, self.template_name, self.context(instance, form))


class InventoryActionView(InventoryViewMixin, View):
    """
    A POST-only archive or reactivate action.

    POST-only because it changes state: a GET that archived a warehouse would
    be triggered by a link prefetch. There is no delete anywhere in this
    module — a code that has been used stays reserved, and a row that has been
    referenced stays readable.
    """

    #: True archives, False brings the row back.
    activate: bool = False
    success_url_name: str = ""
    required_permission: str = ""

    def get_success_url(self) -> str:
        return reverse(self.success_url_name)

    def load(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def authorize(self, instance: Any) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def perform(self, instance: Any) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        instance = self.load()
        self.authorize(instance)
        try:
            self.perform(instance)
        except ValidationError as error:
            messages.error(request, "؛ ".join(str(message) for message in error.messages))
        else:
            messages.success(
                request, _("تمت الإعادة إلى الخدمة.") if self.activate else _("تمت الأرشفة.")
            )
        return HttpResponseRedirect(self.get_success_url())


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class ItemCategoryCreateView(InventoryWriteView):
    form_class = ItemCategoryForm
    required_permission = MANAGE_CATEGORIES
    success_url_name = "inventory:category_list"
    page_title = _("مجموعة أصناف جديدة")
    page_hint = _("ثلاثة مستويات كحد أقصى. الأصناف تُربط بالمستوى الأخير فقط.")
    success_message = _("تمت إضافة المجموعة.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CATEGORIES, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_item_category(
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            parent=form.cleaned_data["parent"],
        )


class ItemCategoryUpdateView(InventoryWriteView):
    form_class = ItemCategoryForm
    required_permission = MANAGE_CATEGORIES
    success_url_name = "inventory:category_list"
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
            "parent": instance.parent,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CATEGORIES, instance.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        update_item_category(
            category=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            parent=form.cleaned_data["parent"],
            is_active=form.cleaned_data["is_active"],
        )


class ItemCategoryActionView(InventoryActionView):
    required_permission = MANAGE_CATEGORIES
    success_url_name = "inventory:category_list"

    def load(self) -> Any:
        return resolve_category(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CATEGORIES, instance.organization
        )

    def perform(self, instance: Any) -> None:
        update_item_category(
            category=instance,
            name_ar=instance.name_ar,
            name_en=instance.name_en,
            parent=instance.parent,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Package units
# ---------------------------------------------------------------------------


class PackageUnitCreateView(InventoryWriteView):
    form_class = PackageUnitForm
    required_permission = MANAGE_PACKAGE_UNITS
    success_url_name = "inventory:package_unit_list"
    page_title = _("وحدة تعبئة جديدة")
    page_hint = _(
        "لا معامل هنا. كرتونة الدجاج ليست كرتونة الزيت — المعامل يُسجَّل لكل صنف "
        "في شاشة تحويلات وحدات الصنف."
    )
    success_message = _("تمت إضافة وحدة التعبئة.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_PACKAGE_UNITS, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_package_unit(
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
        )


class PackageUnitUpdateView(InventoryWriteView):
    form_class = PackageUnitForm
    required_permission = MANAGE_PACKAGE_UNITS
    success_url_name = "inventory:package_unit_list"
    page_title = _("تعديل وحدة التعبئة")
    success_message = _("تم حفظ وحدة التعبئة.")

    def load(self) -> Any:
        return resolve_package_unit(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "organization": instance.organization,
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_PACKAGE_UNITS, instance.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        update_package_unit(
            package_unit=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            is_active=form.cleaned_data["is_active"],
        )


class PackageUnitActionView(InventoryActionView):
    required_permission = MANAGE_PACKAGE_UNITS
    success_url_name = "inventory:package_unit_list"

    def load(self) -> Any:
        return resolve_package_unit(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_PACKAGE_UNITS, instance.organization
        )

    def perform(self, instance: Any) -> None:
        update_package_unit(
            package_unit=instance,
            name_ar=instance.name_ar,
            name_en=instance.name_en,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


class ItemCreateView(InventoryWriteView):
    form_class = InventoryItemForm
    required_permission = MANAGE_ITEMS
    success_url_name = "inventory:item_list"
    page_title = _("صنف جديد")
    page_hint = _("وحدة الأساس تُثبَّت عند الإنشاء — كل كمية مخزنية تُقاس بها.")
    success_message = _("تمت إضافة الصنف.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_ITEMS, form.cleaned_data["organization"]
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_item(
            organization=form.cleaned_data["organization"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            category=form.cleaned_data["category"],
            item_type=form.cleaned_data["item_type"],
            base_unit=form.cleaned_data["base_unit"],
            tracks_lots=form.cleaned_data["tracks_lots"],
            tracks_expiry=form.cleaned_data["tracks_expiry"],
            shelf_life_days=form.cleaned_data["shelf_life_days"],
            notes=form.cleaned_data["notes"],
        )


class ItemUpdateView(InventoryWriteView):
    form_class = InventoryItemForm
    required_permission = MANAGE_ITEMS
    success_url_name = "inventory:item_list"
    page_title = _("تعديل الصنف")
    success_message = _("تم حفظ الصنف.")

    def load(self) -> Any:
        return resolve_item(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "organization": instance.organization,
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "category": instance.category,
            "item_type": instance.item_type,
            "base_unit": instance.base_unit,
            "tracks_lots": instance.tracks_lots,
            "tracks_expiry": instance.tracks_expiry,
            "shelf_life_days": instance.shelf_life_days,
            "notes": instance.notes,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(self.actor, MANAGE_ITEMS, instance.organization)

    def perform(self, instance: Any, form: Any) -> None:
        update_item(
            item=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            category=form.cleaned_data["category"],
            item_type=form.cleaned_data["item_type"],
            tracks_lots=form.cleaned_data["tracks_lots"],
            tracks_expiry=form.cleaned_data["tracks_expiry"],
            shelf_life_days=form.cleaned_data["shelf_life_days"],
            notes=form.cleaned_data["notes"],
            is_active=form.cleaned_data["is_active"],
        )


class ItemActionView(InventoryActionView):
    required_permission = MANAGE_ITEMS
    success_url_name = "inventory:item_list"

    def load(self) -> Any:
        return resolve_item(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(self.actor, MANAGE_ITEMS, instance.organization)

    def perform(self, instance: Any) -> None:
        update_item(
            item=instance,
            name_ar=instance.name_ar,
            name_en=instance.name_en,
            category=instance.category,
            item_type=instance.item_type,
            shelf_life_days=instance.shelf_life_days,
            notes=instance.notes,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Item package conversions
# ---------------------------------------------------------------------------


class ItemConversionCreateView(InventoryWriteView):
    form_class = ItemConversionForm
    required_permission = MANAGE_CONVERSIONS
    success_url_name = "inventory:conversion_list"
    page_title = _("تحويل وحدة صنف جديد")
    page_hint = _("المعامل يصل مباشرة إلى وحدة أساس الصنف — بلا سلاسل تحويل.")
    success_message = _("تمت إضافة التحويل.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CONVERSIONS, form.cleaned_data["item"].organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_item_conversion(
            item=form.cleaned_data["item"],
            package_unit=form.cleaned_data["package_unit"],
            factor_to_base=form.cleaned_data["factor_to_base"],
            effective_from=form.cleaned_data["effective_from"],
            conversion_type=form.cleaned_data["conversion_type"],
            effective_to=form.cleaned_data["effective_to"],
            allows_fractional=form.cleaned_data["allows_fractional"],
            minimum_increment=form.cleaned_data["minimum_increment"],
            is_default_purchase_package=form.cleaned_data["is_default_purchase_package"],
        )


class ItemConversionUpdateView(InventoryWriteView):
    form_class = ItemConversionForm
    required_permission = MANAGE_CONVERSIONS
    success_url_name = "inventory:conversion_list"
    page_title = _("تصحيح التحويل")
    page_hint = _(
        "التصحيح لمعامل لم يُقيَّم عليه شيء بعد. إن تغيّر حجم العبوة فعلياً "
        "فاستخدم إصداراً جديداً بدل التصحيح."
    )
    success_message = _("تم حفظ التحويل.")

    def load(self) -> Any:
        return resolve_conversion(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "item": instance.item,
            "package_unit": instance.package_unit,
            "conversion_type": instance.conversion_type,
            # The stored precision, with a period. Re-entering what is shown
            # must reproduce the same factor exactly.
            "factor_to_base": instance.factor_display,
            "effective_from": instance.effective_from,
            "effective_to": instance.effective_to,
            "allows_fractional": instance.allows_fractional,
            "minimum_increment": (
                f"{instance.minimum_increment:f}" if instance.minimum_increment is not None else ""
            ),
            "is_default_purchase_package": instance.is_default_purchase_package,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CONVERSIONS, instance.item.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        update_item_conversion(
            conversion=instance,
            factor_to_base=form.cleaned_data["factor_to_base"],
            effective_from=form.cleaned_data["effective_from"],
            effective_to=form.cleaned_data["effective_to"],
            conversion_type=form.cleaned_data["conversion_type"],
            allows_fractional=form.cleaned_data["allows_fractional"],
            minimum_increment=form.cleaned_data["minimum_increment"],
            is_default_purchase_package=form.cleaned_data["is_default_purchase_package"],
            is_active=form.cleaned_data["is_active"],
        )


class ItemConversionSupersedeView(InventoryWriteView):
    """Close this version and open its successor — a new packaging fact."""

    form_class = SupersedeConversionForm
    required_permission = MANAGE_CONVERSIONS
    success_url_name = "inventory:conversion_list"
    page_title = _("إصدار جديد من التحويل")
    page_hint = _(
        "صار الكيس ٢٥ كغم بدل ٣٠؟ هذه حقيقة تعبئة جديدة، لا تصحيح. الإصدار "
        "الحالي يُغلق في اليوم السابق وتبقى الحركات المرحّلة كما قُيّمت."
    )
    success_message = _("تم إصدار المعامل الجديد.")

    def load(self) -> Any:
        return resolve_conversion(self.actor, self.kwargs["pk"])

    def build_form(self, instance: Any, data: Any = None) -> Any:
        # No `instance` kwarg: this form describes the successor, not the row
        # it replaces, so there is nothing of the old one to prefill.
        kwargs: dict[str, Any] = {"actor": self.actor}
        if data is not None:
            kwargs["data"] = data
        return self.form_class(**kwargs)

    def context(self, instance: Any, form: Any) -> dict[str, Any]:
        context = super().context(instance, form)
        context["page_title"] = _("إصدار جديد") + f" — {instance}"
        return context

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CONVERSIONS, instance.item.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        supersede_item_conversion(
            conversion=instance,
            factor_to_base=form.cleaned_data["factor_to_base"],
            effective_from=form.cleaned_data["effective_from"],
            reason=form.cleaned_data["reason"],
        )


class ItemConversionActionView(InventoryActionView):
    required_permission = MANAGE_CONVERSIONS
    success_url_name = "inventory:conversion_list"

    def load(self) -> Any:
        return resolve_conversion(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_CONVERSIONS, instance.item.organization
        )

    def perform(self, instance: Any) -> None:
        update_item_conversion(
            conversion=instance,
            factor_to_base=instance.factor_to_base,
            effective_from=instance.effective_from,
            effective_to=instance.effective_to,
            conversion_type=instance.conversion_type,
            allows_fractional=instance.allows_fractional,
            minimum_increment=instance.minimum_increment,
            is_default_purchase_package=instance.is_default_purchase_package,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------


class WarehouseCreateView(InventoryWriteView):
    form_class = WarehouseForm
    required_permission = MANAGE_WAREHOUSES
    success_url_name = "inventory:warehouse_list"
    page_title = _("مخزن جديد")
    page_hint = _("المخزن يتبع فرعاً واحداً وهو الذي يملك قيمة المخزون.")
    success_message = _("تمت إضافة المخزن.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_branch_permission(self.actor, MANAGE_WAREHOUSES, form.cleaned_data["branch"])

    def perform(self, instance: Any, form: Any) -> None:
        create_warehouse(
            branch=form.cleaned_data["branch"],
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            warehouse_type=form.cleaned_data["warehouse_type"],
        )


class WarehouseUpdateView(InventoryWriteView):
    form_class = WarehouseForm
    required_permission = MANAGE_WAREHOUSES
    success_url_name = "inventory:warehouse_list"
    page_title = _("تعديل المخزن")
    success_message = _("تم حفظ المخزن.")

    def load(self) -> Any:
        return resolve_manageable_warehouse(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "branch": instance.branch,
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "warehouse_type": instance.warehouse_type,
            "is_active": instance.is_active,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_branch_permission(self.actor, MANAGE_WAREHOUSES, instance.branch)

    def perform(self, instance: Any, form: Any) -> None:
        update_warehouse(
            warehouse=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            is_active=form.cleaned_data["is_active"],
        )


class WarehouseActionView(InventoryActionView):
    required_permission = MANAGE_WAREHOUSES
    success_url_name = "inventory:warehouse_list"

    def load(self) -> Any:
        return resolve_manageable_warehouse(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_branch_permission(self.actor, MANAGE_WAREHOUSES, instance.branch)

    def perform(self, instance: Any) -> None:
        update_warehouse(
            warehouse=instance,
            name_ar=instance.name_ar,
            name_en=instance.name_en,
            is_active=self.activate,
        )
