"""
Sales screens, mounted inside the shell.

The list, write and action machinery is reused from `apps.inventory.views`
rather than copied, for the reason `apps.procurement.views` records: it is
generic — a scoped queryset, a per-row action decision, an htmx partial swap, a
POST-only archive — and a second copy would drift within two tasks, in the
authorization behaviour, which is the part that must not vary.

**Sales gets its own form template** (`sales/master_form.html`) rather than
reusing inventory's. Inventory's write template extends `shell.html` directly,
which is correct for inventory and wrong for a module whose forms are opened
inside htmx panels: a fragment that carries a second shell looks right until
somebody swaps it. `sales/master_form.html` extends
`form_base_template|default:"shell.html"`, the same contract lists already have,
and `SalesWriteView` supplies the fragment parent when `HX-Request` is present.
Inventory's own templates are untouched.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.inventory.views import (
    InventoryActionView,
    InventoryListView,
    InventoryViewMixin,
    InventoryWriteView,
)
from apps.organizations.authorization import (
    has_organization_permission,
    organizations_with_permission,
    require_reachable_organization_permission,
)
from apps.organizations.selectors import accessible_branches
from apps.sales.forms import (
    BranchAvailabilityForm,
    MenuCategoryForm,
    MenuItemForm,
    MenuPriceCloseForm,
    MenuPriceForm,
    SalesChannelForm,
)
from apps.sales.models import SalesChannelCategory
from apps.sales.permissions import (
    MANAGE_MENU,
    MANAGE_SALES_CHANNELS,
    VIEW_SALES,
    VIEW_SALES_COST,
)
from apps.sales.selectors import (
    effective_prices,
    resolve_menu_item,
    visible_menu_categories,
    visible_menu_items,
    visible_menu_prices,
    visible_sales_channels,
)
from apps.sales.services import (
    archive_menu_price,
    close_menu_price,
    create_menu_category,
    create_menu_item,
    create_menu_price,
    create_sales_channel,
    set_branch_availability,
    update_menu_category,
    update_menu_item,
    update_sales_channel,
)


class SalesListView(InventoryListView):
    """Every sales list: same scoping, same htmx contract, same row actions."""

    module_key = "sales"
    required_permission = VIEW_SALES

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # Sales screens do not inherit inventory's direction and density rules;
        # they use the same semantic structure with the module's own styling,
        # exactly as Settings, Accounting, Procurement and Kitchen do.
        context["inventory_ui"] = False
        # Cost columns are **omitted** rather than blanked, so the template asks
        # this once and leaves the whole column out. Answered from the set of
        # organizations the caller may read cost in, not from the rows on the
        # page: a page with no rows must not silently grant the column.
        context["cost_readable_organization_ids"] = list(
            organizations_with_permission(self.actor, VIEW_SALES_COST).values_list("id", flat=True)
        )
        return context


class SalesWriteView(InventoryWriteView):
    """
    Every sales create and edit screen.

    Supplies `form_base_template` so an htmx GET answers with the form alone.
    Without it a panel swap would receive a whole document — two `<html>`
    elements, two navigation rails — which renders correctly enough to be
    missed in review and wrong in every accessibility tree.
    """

    module_key = "sales"
    template_name = "sales/master_form.html"

    def is_htmx(self) -> bool:
        return self.request.headers.get("HX-Request") == "true"

    def context(self, instance: Any, form: Any) -> dict[str, Any]:
        context = super().context(instance, form)
        context["form_base_template"] = (
            "settings/_form_fragment.html" if self.is_htmx() else "shell.html"
        )
        return context


class SalesActionView(InventoryActionView):
    module_key = "sales"


# ---------------------------------------------------------------------------
# Menu categories
# ---------------------------------------------------------------------------


class MenuCategoryListView(SalesListView):
    template_name = "sales/menu_category_list.html"
    context_object_name = "categories"
    page_title = _("مجموعات المنيو")
    page_hint = _(
        "تجميع للعرض فقط. لا تحمل المجموعة أي أثر محاسبي، ولا يقرأها أي قيد — "
        "ترتيب لوحة المنيو قرار تصميم، لا قرار مالي."
    )
    search_fields = ("code", "name_ar", "name_en")
    manage_permission = MANAGE_MENU
    create_url_name = "sales:menu_category_create"
    create_label = _("مجموعة جديدة")

    def scoped_queryset(self) -> QuerySet[Any]:
        return visible_menu_categories(self.actor).order_by("display_order", "code")


class MenuCategoryWriteView(SalesWriteView):
    form_class = MenuCategoryForm
    required_permission = MANAGE_MENU
    success_url_name = "sales:menu_category_list"


class MenuCategoryCreateView(MenuCategoryWriteView):
    page_title = _("مجموعة منيو جديدة")
    page_hint = _("الرمز يُخزَّن بأحرف كبيرة ولا يمكن تغييره بعد الحفظ.")
    success_message = _("تمت إضافة المجموعة.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_MENU, form.selected_organization()
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_menu_category(
            organization=form.selected_organization(),
            code=form.cleaned_data["code"],
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            display_order=form.cleaned_data["display_order"],
        )


class MenuCategoryUpdateView(MenuCategoryWriteView):
    page_title = _("تعديل مجموعة المنيو")
    success_message = _("تم حفظ المجموعة.")

    def load(self) -> Any:
        from apps.organizations.authorization import OutOfScope

        category = visible_menu_categories(self.actor).filter(pk=self.kwargs["pk"]).first()
        if category is None:
            raise OutOfScope(_("Menu category does not exist."))
        return category

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "display_order": instance.display_order,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(self.actor, MANAGE_MENU, instance.organization)

    def perform(self, instance: Any, form: Any) -> None:
        update_menu_category(
            category=instance,
            name_ar=form.cleaned_data["name_ar"],
            name_en=form.cleaned_data["name_en"],
            display_order=form.cleaned_data["display_order"],
            is_active=instance.is_active,
        )


# ---------------------------------------------------------------------------
# Menu items
# ---------------------------------------------------------------------------


class MenuItemListView(SalesListView):
    template_name = "sales/menu_item_list.html"
    context_object_name = "menu_items"
    page_title = _("أصناف المنيو")
    page_hint = _(
        "صنف المنيو يشير إلى وصفة ورمز حصة، لا إلى حصة نسخة بعينها: النسخة "
        "السارية في تاريخ البيع هي التي تُحلّ، وكل سطر مبيعات يحتفظ بنسختها إلى الأبد."
    )
    search_fields = ("code", "name_ar", "name_en", "recipe__code")
    manage_permission = MANAGE_MENU
    create_url_name = "sales:menu_item_create"
    create_label = _("صنف منيو جديد")
    search_placeholder = _("ابحث بالرمز أو الاسم أو رمز الوصفة…")
    result_label = _("صنف")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_menu_items(self.actor)
        category = self.request.GET.get("category", "").strip()
        if category.isdigit():
            queryset = queryset.filter(category_id=int(category))
        state = self.request.GET.get("state", "").strip()
        if state == "active":
            queryset = queryset.filter(is_active=True)
        elif state == "archived":
            queryset = queryset.filter(is_active=False)
        return queryset.order_by("display_order", "code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["categories"] = visible_menu_categories(self.actor).order_by(
            "display_order", "code"
        )
        context["selected_category"] = self.request.GET.get("category", "")
        context["selected_state"] = self.request.GET.get("state", "")
        return context


class MenuItemWriteView(SalesWriteView):
    form_class = MenuItemForm
    required_permission = MANAGE_MENU
    success_url_name = "sales:menu_item_list"

    def _fields(self, form: Any) -> dict[str, Any]:
        data = form.cleaned_data
        return {
            "name_ar": data["name_ar"],
            "name_en": data.get("name_en", ""),
            "category": data.get("category"),
            "recipe": data["recipe"],
            "serving_code": data["serving_code"],
            "description_ar": data.get("description_ar", ""),
            "display_order": data["display_order"],
            "notes": data.get("notes", ""),
        }


class MenuItemCreateView(MenuItemWriteView):
    page_title = _("صنف منيو جديد")
    page_hint = _(
        "رمز الحصة يُطابَق مع حصص نسخ الوصفة كلها، لا مع النسخة السارية اليوم فقط — "
        "حتى يمكن تجهيز صنف لوصفة تبدأ نسختها الأحد القادم."
    )
    success_message = _("تمت إضافة الصنف.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_MENU, form.selected_organization()
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_menu_item(
            organization=form.selected_organization(),
            code=form.cleaned_data["code"],
            **self._fields(form),
        )


class MenuItemUpdateView(MenuItemWriteView):
    page_title = _("تعديل صنف المنيو")
    page_hint = _(
        "تغيير الوصفة أو الحصة يسري على المبيعات الجديدة فقط. السطور المرحّلة "
        "تحمل نسخة الوصفة والحصة والسعر التي استُخدمت فعلاً."
    )
    success_message = _("تم حفظ الصنف.")

    def load(self) -> Any:
        return resolve_menu_item(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "category": instance.category,
            "recipe": instance.recipe,
            "serving_code": instance.serving_code,
            "description_ar": instance.description_ar,
            "display_order": instance.display_order,
            "notes": instance.notes,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(self.actor, MANAGE_MENU, instance.organization)

    def perform(self, instance: Any, form: Any) -> None:
        update_menu_item(item=instance, is_active=instance.is_active, **self._fields(form))


class MenuItemActionView(SalesActionView):
    required_permission = MANAGE_MENU
    success_url_name = "sales:menu_item_list"

    def load(self) -> Any:
        return resolve_menu_item(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(self.actor, MANAGE_MENU, instance.organization)

    def perform(self, instance: Any) -> None:
        update_menu_item(
            item=instance,
            name_ar=instance.name_ar,
            name_en=instance.name_en,
            category=instance.category,
            recipe=instance.recipe,
            serving_code=instance.serving_code,
            description_ar=instance.description_ar,
            display_order=instance.display_order,
            notes=instance.notes,
            is_active=self.activate,
        )


class MenuItemDetailView(InventoryViewMixin, View):
    """
    One item: where it is sold, and what it costs there.

    A detail screen rather than another list, because the two questions an
    operator actually has about a menu item — "is this on at the airport
    branch" and "what does it cost there today" — are about the *combination*,
    and a price list filtered by item answers only half of it.

    Three availability states are rendered, not two. A branch with no setting
    row has never been offered the item; a branch with a row and
    `is_available=False` is offered it and switched off. Both mean "not on sale
    today", and the difference is what tells an operator whether to add the
    offer or turn it back on.
    """

    module_key = "sales"
    required_permission = VIEW_SALES

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        item = resolve_menu_item(self.actor, kwargs["pk"])
        today = timezone.localdate()

        branches = (
            accessible_branches(self.actor)
            .filter(organization_id=item.organization_id)
            .order_by("code")
        )
        settings_by_branch = {
            row.branch_id: row for row in item.branch_settings.select_related("branch")
        }
        rows = [
            {
                "branch": branch,
                "setting": settings_by_branch.get(branch.pk),
                "offered": branch.pk in settings_by_branch,
                "available": (
                    branch.pk in settings_by_branch and settings_by_branch[branch.pk].is_available
                ),
                "prices": effective_prices(item, branch, today),
            }
            for branch in branches
        ]

        may_manage = has_organization_permission(self.actor, MANAGE_MENU, item.organization)
        context = {
            "item": item,
            "rows": rows,
            "today": today,
            "may_manage": may_manage,
            "availability_form": (
                BranchAvailabilityForm(actor=self.actor, menu_item=item) if may_manage else None
            ),
            "page_title": item.name_ar,
            "page_hint": _("توفّر الصنف في الفروع، والأسعار السارية اليوم في كل فرع."),
            "list_base_template": (
                "settings/_list_fragment.html"
                if request.headers.get("HX-Request") == "true"
                else "shell.html"
            ),
        }
        return render(request, "sales/menu_item_detail.html", context)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        item = resolve_menu_item(self.actor, kwargs["pk"])
        require_reachable_organization_permission(self.actor, MANAGE_MENU, item.organization)
        form = BranchAvailabilityForm(request.POST, actor=self.actor, menu_item=item)
        if form.is_valid():
            try:
                set_branch_availability(
                    item=item,
                    branch=form.cleaned_data["branch"],
                    is_available=form.cleaned_data["is_available"],
                    local_name_ar=form.cleaned_data.get("local_name_ar", ""),
                    notes=form.cleaned_data.get("notes", ""),
                )
            except ValidationError as error:
                messages.error(request, "؛ ".join(str(message) for message in error.messages))
            else:
                messages.success(request, _("تم تحديث توفّر الصنف."))
        else:
            messages.error(request, _("تعذّر حفظ التوفّر. راجع الحقول."))
        return HttpResponseRedirect(reverse("sales:menu_item_detail", args=[item.pk]))


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


class MenuPriceListView(SalesListView):
    template_name = "sales/menu_price_list.html"
    context_object_name = "prices"
    page_title = _("أسعار المنيو")
    page_hint = _(
        "الأسعار مؤرّخة السريان ولا تُعدَّل في مكانها: تصحيح السعر هو إنهاؤه "
        "وإصدار سعر بديل، لأن السعر الذي باع شيئاً صار مستنداً."
    )
    search_fields = ("menu_item__code", "menu_item__name_ar", "branch__code")
    manage_permission = MANAGE_MENU
    manage_scope = "organization"
    create_url_name = "sales:menu_price_create"
    create_label = _("سعر جديد")
    result_label = _("سعر")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_menu_prices(self.actor)
        if self.request.GET.get("current", "").strip() == "1":
            today = timezone.localdate()
            queryset = queryset.filter(is_active=True, effective_from__lte=today).filter(
                Q(effective_to__isnull=True) | Q(effective_to__gte=today)
            )
        return queryset.order_by("menu_item__code", "branch__code", "-effective_from")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["only_current"] = self.request.GET.get("current", "")
        context["today"] = timezone.localdate()
        return context


class MenuPriceCreateView(SalesWriteView):
    form_class = MenuPriceForm
    required_permission = MANAGE_MENU
    success_url_name = "sales:menu_price_list"
    page_title = _("سعر منيو جديد")
    page_hint = _(
        "التداخل الزمني داخل النطاق الواحد ترفضه قاعدة البيانات نفسها، لا الخدمة: "
        "طلبان متزامنان يقرآن جدولاً نظيفاً قبل أن يكتب أيّهما."
    )
    success_message = _("تمت إضافة السعر.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_MENU, form.cleaned_data["menu_item"].organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        create_menu_price(
            menu_item=data["menu_item"],
            branch=data["branch"],
            unit_price=data["unit_price"],
            effective_from=data["effective_from"],
            scope=data["scope"],
            channel=data.get("channel"),
            effective_to=data.get("effective_to"),
            evidence_reference=data.get("evidence_reference", ""),
            notes=data.get("notes", ""),
        )


class MenuPriceCloseView(SalesWriteView):
    form_class = MenuPriceCloseForm
    required_permission = MANAGE_MENU
    success_url_name = "sales:menu_price_list"
    page_title = _("إنهاء سعر")
    page_hint = _("المبلغ لا يُعدَّل. يُنهى السعر بتاريخ، ويُصدر سعر بديل.")
    success_message = _("تم إنهاء السعر.")

    def build_form(self, instance: Any, data: Any = None) -> Any:
        kwargs: dict[str, Any] = {}
        if data is not None:
            kwargs["data"] = data
        return self.form_class(**kwargs)

    def load(self) -> Any:
        from apps.organizations.authorization import OutOfScope

        price = visible_menu_prices(self.actor).filter(pk=self.kwargs["pk"]).first()
        if price is None:
            raise OutOfScope(_("Menu price does not exist."))
        return price

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_MENU, instance.menu_item.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        close_menu_price(
            price=instance,
            effective_to=form.cleaned_data["effective_to"],
            reason=form.cleaned_data["reason"],
        )


class MenuPriceArchiveView(SalesActionView):
    required_permission = MANAGE_MENU
    success_url_name = "sales:menu_price_list"

    def load(self) -> Any:
        from apps.organizations.authorization import OutOfScope

        price = visible_menu_prices(self.actor).filter(pk=self.kwargs["pk"]).first()
        if price is None:
            raise OutOfScope(_("Menu price does not exist."))
        return price

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_MENU, instance.menu_item.organization
        )

    def perform(self, instance: Any) -> None:
        archive_menu_price(price=instance, reason=str(_("سحب سعر غير صحيح.")))


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class SalesChannelListView(SalesListView):
    template_name = "sales/sales_channel_list.html"
    context_object_name = "channels"
    page_title = _("قنوات البيع")
    page_hint = _(
        "القناة تقرّر أين يذهب المال ومَن يعدّه. تطبيقات التوصيل كلها قناة واحدة "
        "من نوع «تطبيق توصيل»، والشركة نفسها بيانات أساسية منفصلة."
    )
    search_fields = ("code", "name_ar", "name_en")
    manage_permission = MANAGE_SALES_CHANNELS
    create_url_name = "sales:channel_create"
    create_label = _("قناة جديدة")
    result_label = _("قناة")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_sales_channels(self.actor)
        category = self.request.GET.get("category", "").strip()
        if category in SalesChannelCategory.values:
            queryset = queryset.filter(category=category)
        return queryset.order_by("display_order", "code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["categories"] = SalesChannelCategory.choices
        context["selected_category"] = self.request.GET.get("category", "")
        return context


class SalesChannelWriteView(SalesWriteView):
    form_class = SalesChannelForm
    required_permission = MANAGE_SALES_CHANNELS
    success_url_name = "sales:channel_list"


class SalesChannelCreateView(SalesChannelWriteView):
    page_title = _("قناة بيع جديدة")
    page_hint = _(
        "مركز الكلفة إلزامي: حسابات الإيراد والخصم والعمولة تتطلبه، والقناة هي "
        "المستوى الذي يحمل المعنى."
    )
    success_message = _("تمت إضافة القناة.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_SALES_CHANNELS, form.selected_organization()
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        create_sales_channel(
            organization=form.selected_organization(),
            code=data["code"],
            name_ar=data["name_ar"],
            name_en=data.get("name_en", ""),
            category=data["category"],
            cost_center=data["cost_center"],
            default_tender=data["default_tender"],
            revenue_account=data.get("revenue_account"),
            requires_cashier=data.get("requires_cashier", True),
            display_order=data["display_order"],
            notes=data.get("notes", ""),
        )


class SalesChannelUpdateView(SalesChannelWriteView):
    page_title = _("تعديل قناة البيع")
    page_hint = _(
        "نوع القناة لا يُعدَّل: هو الذي قرّر كيف رُحّلت أيام المبيعات السابقة، "
        "وتغييره يعيد تفسير تاريخ وصل إلى الدفاتر فعلاً. القناة الخاطئة تُؤرشف وتُستبدل."
    )
    success_message = _("تم حفظ القناة.")

    def load(self) -> Any:
        from apps.organizations.authorization import OutOfScope

        channel = visible_sales_channels(self.actor).filter(pk=self.kwargs["pk"]).first()
        if channel is None:
            raise OutOfScope(_("Sales channel does not exist."))
        return channel

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "code": instance.code,
            "name_ar": instance.name_ar,
            "name_en": instance.name_en,
            "category": instance.category,
            "default_tender": instance.default_tender,
            "cost_center": instance.cost_center,
            "revenue_account": instance.revenue_account,
            "requires_cashier": instance.requires_cashier,
            "display_order": instance.display_order,
            "notes": instance.notes,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_SALES_CHANNELS, instance.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        update_sales_channel(
            channel=instance,
            name_ar=data["name_ar"],
            name_en=data.get("name_en", ""),
            cost_center=data["cost_center"],
            default_tender=data["default_tender"],
            revenue_account=data.get("revenue_account"),
            requires_cashier=data.get("requires_cashier", True),
            display_order=data["display_order"],
            notes=data.get("notes", ""),
            is_active=instance.is_active,
        )


class SalesChannelActionView(SalesActionView):
    required_permission = MANAGE_SALES_CHANNELS
    success_url_name = "sales:channel_list"

    def load(self) -> Any:
        from apps.organizations.authorization import OutOfScope

        channel = visible_sales_channels(self.actor).filter(pk=self.kwargs["pk"]).first()
        if channel is None:
            raise OutOfScope(_("Sales channel does not exist."))
        return channel

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_SALES_CHANNELS, instance.organization
        )

    def perform(self, instance: Any) -> None:
        update_sales_channel(
            channel=instance,
            name_ar=instance.name_ar,
            name_en=instance.name_en,
            cost_center=instance.cost_center,
            default_tender=instance.default_tender,
            revenue_account=instance.revenue_account,
            requires_cashier=instance.requires_cashier,
            display_order=instance.display_order,
            notes=instance.notes,
            is_active=self.activate,
        )


__all__ = [
    "MenuCategoryCreateView",
    "MenuCategoryListView",
    "MenuCategoryUpdateView",
    "MenuItemActionView",
    "MenuItemCreateView",
    "MenuItemDetailView",
    "MenuItemListView",
    "MenuItemUpdateView",
    "MenuPriceArchiveView",
    "MenuPriceCloseView",
    "MenuPriceCreateView",
    "MenuPriceListView",
    "SalesActionView",
    "SalesChannelActionView",
    "SalesChannelCreateView",
    "SalesChannelListView",
    "SalesChannelUpdateView",
    "SalesListView",
    "SalesWriteView",
]
