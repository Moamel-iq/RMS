"""
Inventory master-data screens, inside the Khan Mandi shell.

Reuses the foundation list/form scaffolding rather than inventing a second
one, so the inventory screens look and behave exactly like the settings
screens an operator already knows.

Two differences from the foundation screens, both deliberate:

* Access is by **inventory permission**, not by staff flag. A storekeeper is
  not staff and must still see the item master.
* Every queryset is scoped through `apps/inventory/selectors.py`, so an
  organization's items are invisible to anyone outside it — never filtered in
  the template.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView

if TYPE_CHECKING:
    # django-stubs types ListView generically; the runtime class is not
    # subscriptable. Same arrangement as `apps/accounting/admin.py`.
    _ListView = ListView[Any]
else:
    _ListView = ListView

from apps.core.views import ModuleViewMixin
from apps.inventory.models import ItemType
from apps.inventory.permissions import (
    MANAGE_WAREHOUSES,
    VIEW_ITEM,
)
from apps.inventory.selectors import (
    visible_categories,
    visible_conversions,
    visible_items,
    visible_package_units,
    visible_warehouses,
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

    @property
    def actor(self) -> User:
        """The signed-in caller. `test_func` has already refused anonymity."""
        user: User = self.request.user  # type: ignore[assignment]
        return user


class InventoryListView(InventoryViewMixin, _ListView):
    """A searchable, paged list of one master-data kind."""

    paginate_by = 25
    page_title: Any = ""
    page_hint: Any = ""
    search_fields: tuple[str, ...] = ()
    create_url_name: str | None = None
    create_label: Any = ""

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

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        context["page_hint"] = self.page_hint
        context["search"] = self.request.GET.get("q", "")
        context["create_label"] = self.create_label
        context["create_url"] = None
        return context


class ItemCategoryListView(InventoryListView):
    template_name = "inventory/category_list.html"
    context_object_name = "categories"
    page_title = _("مجموعات الأصناف")
    page_hint = _(
        "ثلاثة مستويات كحد أقصى. الأصناف تُربط بالمستوى الأخير فقط، حتى تبقى تقارير المجموعات صحيحة."
    )
    search_fields = ("code", "name_ar", "name_en")

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

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_package_units(self.actor).order_by("code")


class ItemListView(InventoryListView):
    template_name = "inventory/item_list.html"
    context_object_name = "items"
    page_title = _("الأصناف")
    page_hint = _("سجل الأصناف على مستوى المؤسسة، مشترك بين الفروع.")
    search_fields = ("code", "name_ar", "name_en")

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

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_warehouses(self.actor).order_by("branch__code", "code")
