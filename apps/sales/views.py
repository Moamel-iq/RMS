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

from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, QuerySet
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
    require_organization_permission,
    require_reachable_organization_permission,
)
from apps.organizations.selectors import accessible_branches
from apps.sales.agreements import preview, resolve_agreement
from apps.sales.forms import (
    AgreementCloseForm,
    ApplicationBranchForm,
    BranchAvailabilityForm,
    DeliveryAgreementForm,
    DeliveryApplicationForm,
    DiscountCloseForm,
    DiscountProgramForm,
    MenuCategoryForm,
    MenuItemForm,
    MenuPriceCloseForm,
    MenuPriceForm,
    SalesChannelForm,
)
from apps.sales.models import SalesChannelCategory
from apps.sales.permissions import (
    MANAGE_DELIVERY_APPLICATIONS,
    MANAGE_MENU,
    MANAGE_SALES_AGREEMENTS,
    MANAGE_SALES_CHANNELS,
    MANAGE_SALES_DISCOUNTS,
    VIEW_SALES,
    VIEW_SALES_COST,
)
from apps.sales.selectors import (
    effective_prices,
    resolve_agreement_row,
    resolve_delivery_application,
    resolve_discount_program,
    resolve_menu_item,
    visible_agreements,
    visible_delivery_applications,
    visible_discount_programs,
    visible_menu_categories,
    visible_menu_items,
    visible_menu_prices,
    visible_sales_channels,
)
from apps.sales.services import (
    archive_menu_price,
    close_delivery_agreement,
    close_discount_program,
    close_menu_price,
    create_delivery_agreement,
    create_delivery_application,
    create_discount_program,
    create_menu_category,
    create_menu_item,
    create_menu_price,
    create_sales_channel,
    set_application_branch_setting,
    set_branch_availability,
    update_delivery_application,
    update_menu_category,
    update_menu_item,
    update_sales_channel,
)

#: A worked example for the agreement preview: one ordinary application
#: order. A round figure on purpose — the screen is showing what a *rate*
#: costs, and an odd base would make the reader do arithmetic to check it.
PREVIEW_AMOUNT = Decimal("25000")


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
    search_fields = ("code", "name")
    manage_permission = MANAGE_MENU
    create_url_name = "sales:menu_category_create"
    create_label = _("مجموعة جديدة")

    def scoped_queryset(self) -> QuerySet[Any]:
        return (
            visible_menu_categories(self.actor)
            .annotate(item_count=Count("items"))
            .order_by("display_order", "code")
        )


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
            name=form.cleaned_data["name"],
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
            "name": instance.name,
            "display_order": instance.display_order,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(self.actor, MANAGE_MENU, instance.organization)

    def perform(self, instance: Any, form: Any) -> None:
        update_menu_category(
            category=instance,
            name=form.cleaned_data["name"],
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
        "كل صنف يُنفّذ إمّا من حصة وصفة أو مباشرة من بضاعة إعادة البيع. "
        "التنفيذ المختار وبياناته تُحفظ على سطر البيع حتى لا تغيّر تعديلات المنيو التاريخ."
    )
    search_fields = (
        "code",
        "name",
        "recipe__code",
        "inventory_item__code",
        "inventory_item__name",
    )
    manage_permission = MANAGE_MENU
    create_url_name = "sales:menu_item_create"
    create_label = _("صنف منيو جديد")
    search_placeholder = _("ابحث بالرمز أو الاسم أو الوصفة أو الصنف المخزني…")
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
    template_name = "sales/menu_item_form.html"
    required_permission = MANAGE_MENU
    success_url_name = "sales:menu_item_list"

    def _fields(self, form: Any) -> dict[str, Any]:
        data = form.cleaned_data
        return {
            "name": data["name"],
            "category": data.get("category"),
            "fulfillment_source": data["fulfillment_source"],
            "recipe": data.get("recipe"),
            "serving_code": data.get("serving_code", ""),
            "inventory_item": data.get("inventory_item"),
            "direct_stock_base_quantity": data.get("direct_stock_base_quantity"),
            "description_ar": data.get("description_ar", ""),
            "display_order": data["display_order"],
            "notes": data.get("notes", ""),
        }


class MenuItemCreateView(MenuItemWriteView):
    page_title = _("صنف منيو جديد")
    page_hint = _(
        "اختر مسار تنفيذ واحداً. الأطباق ترتبط بوصفة ورمز حصة؛ الماء والمشروبات "
        "والبضاعة المشتراة لإعادة البيع ترتبط بصنف مخزني وكمية صرف لكل وحدة مباعة."
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
            "name": instance.name,
            "category": instance.category,
            "fulfillment_source": instance.fulfillment_source,
            "recipe": instance.recipe,
            "serving_code": instance.serving_code,
            "inventory_item": instance.inventory_item,
            "direct_stock_base_quantity": instance.direct_stock_base_quantity,
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
            name=instance.name,
            category=instance.category,
            fulfillment_source=instance.fulfillment_source,
            recipe=instance.recipe,
            serving_code=instance.serving_code,
            inventory_item=instance.inventory_item,
            direct_stock_base_quantity=instance.direct_stock_base_quantity,
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
            row.branch_id: row
            for row in item.branch_settings.select_related("branch", "source_warehouse")
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
            "page_title": item.name,
            "page_hint": _("توفّر الصنف في الفروع، والأسعار السارية اليوم في كل فرع."),
            # `_form_fragment.html`, not `_list_fragment.html`. This template
            # extends `list_base_template` **directly** rather than through
            # `settings/base_list.html`, so the block it defines is `page`;
            # `_list_fragment.html` contains only `results`, Django silently
            # drops a child block the parent does not declare, and the htmx
            # form of this screen answered 200 with an empty body.
            "list_base_template": (
                "settings/_form_fragment.html"
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
                    source_warehouse=form.cleaned_data.get("source_warehouse"),
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
    search_fields = ("menu_item__code", "menu_item__name", "branch__code")
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
    search_fields = ("code", "name")
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
            name=data["name"],
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
            "name": instance.name,
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
            name=data["name"],
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
            name=instance.name,
            cost_center=instance.cost_center,
            default_tender=instance.default_tender,
            revenue_account=instance.revenue_account,
            requires_cashier=instance.requires_cashier,
            display_order=instance.display_order,
            notes=instance.notes,
            is_active=self.activate,
        )


# ---------------------------------------------------------------------------
# Delivery applications — checkpoint 2
# ---------------------------------------------------------------------------


class DeliveryApplicationListView(SalesListView):
    template_name = "sales/delivery_application_list.html"
    context_object_name = "applications"
    page_title = _("تطبيقات التوصيل")
    page_hint = _(
        "شركات التوصيل التي يبيع المطعم عبرها. لا تحمل رصيداً — ما يدين به التطبيق "
        "يُحتسب من دفتر الذمم غير القابل للتعديل — ولا تحمل نسبة عمولة، فالنسب بنود "
        "تعاقدية مؤرّخة تعيش في الاتفاقيات."
    )
    search_fields = ("code", "name", "contact_name")
    manage_permission = MANAGE_DELIVERY_APPLICATIONS
    create_url_name = "sales:application_create"
    create_label = _("تطبيق جديد")
    result_label = _("تطبيق")

    def scoped_queryset(self) -> QuerySet[Any]:
        return (
            visible_delivery_applications(self.actor)
            .annotate(branch_setting_count=Count("branch_settings"))
            .order_by("code")
        )


class DeliveryApplicationWriteView(SalesWriteView):
    form_class = DeliveryApplicationForm
    required_permission = MANAGE_DELIVERY_APPLICATIONS
    success_url_name = "sales:application_list"

    def _fields(self, form: Any) -> dict[str, Any]:
        data = form.cleaned_data
        return {
            "name": data["name"],
            "settlement_cycle_days": data["settlement_cycle_days"],
            "receivable_account": data.get("receivable_account"),
            "contact_name": data.get("contact_name", ""),
            "phone": data.get("phone", ""),
            "notes": data.get("notes", ""),
        }


class DeliveryApplicationCreateView(DeliveryApplicationWriteView):
    page_title = _("تطبيق توصيل جديد")
    page_hint = _("الرمز يُخزَّن بأحرف كبيرة ولا يمكن تغييره بعد الحفظ.")
    success_message = _("تمت إضافة التطبيق.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_DELIVERY_APPLICATIONS, form.selected_organization()
        )

    def perform(self, instance: Any, form: Any) -> None:
        create_delivery_application(
            organization=form.selected_organization(),
            code=form.cleaned_data["code"],
            **self._fields(form),
        )


class DeliveryApplicationUpdateView(DeliveryApplicationWriteView):
    page_title = _("تعديل تطبيق التوصيل")
    page_hint = _(
        "تغيير حساب الذمة يسري على القيود الجديدة فقط. القيد المرحّل يحمل الحساب "
        "الذي استُخدم فعلاً، والرصيد لا يتبع الإعداد."
    )
    success_message = _("تم حفظ التطبيق.")

    def load(self) -> Any:
        return resolve_delivery_application(self.actor, self.kwargs["pk"])

    def initial_for(self, instance: Any) -> dict[str, Any]:
        return {
            "code": instance.code,
            "name": instance.name,
            "settlement_cycle_days": instance.settlement_cycle_days,
            "receivable_account": instance.receivable_account,
            "contact_name": instance.contact_name,
            "phone": instance.phone,
            "notes": instance.notes,
        }

    def authorize(self, instance: Any, form: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_DELIVERY_APPLICATIONS, instance.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        update_delivery_application(
            application=instance, is_active=instance.is_active, **self._fields(form)
        )


class DeliveryApplicationActionView(SalesActionView):
    required_permission = MANAGE_DELIVERY_APPLICATIONS
    success_url_name = "sales:application_list"

    def load(self) -> Any:
        return resolve_delivery_application(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any) -> None:
        require_reachable_organization_permission(
            self.actor, MANAGE_DELIVERY_APPLICATIONS, instance.organization
        )

    def perform(self, instance: Any) -> None:
        update_delivery_application(
            application=instance,
            name=instance.name,
            settlement_cycle_days=instance.settlement_cycle_days,
            receivable_account=instance.receivable_account,
            contact_name=instance.contact_name,
            phone=instance.phone,
            notes=instance.notes,
            is_active=self.activate,
        )


class DeliveryApplicationDetailView(InventoryViewMixin, View):
    """
    One application: which branches trade with it, and on what terms.

    Branch activation and the effective agreements are shown together because
    that is the pair somebody checks before going live: a branch that is
    switched on with no agreement will refuse every sale it takes, and the two
    facts live in different tables.
    """

    module_key = "sales"
    required_permission = VIEW_SALES

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        application = resolve_delivery_application(self.actor, kwargs["pk"])
        today = timezone.localdate()

        branches = (
            accessible_branches(self.actor)
            .filter(organization_id=application.organization_id)
            .order_by("code")
        )
        settings_by_branch = {
            row.branch_id: row for row in application.branch_settings.select_related("branch")
        }
        rows = [
            {
                "branch": branch,
                "setting": settings_by_branch.get(branch.pk),
                "live": (
                    branch.pk in settings_by_branch and settings_by_branch[branch.pk].is_active
                ),
                "agreement": resolve_agreement(
                    branch_id=branch.pk,
                    delivery_application_id=application.pk,
                    on_date=today,
                ),
            }
            for branch in branches
        ]

        may_manage = has_organization_permission(
            self.actor, MANAGE_DELIVERY_APPLICATIONS, application.organization
        )
        context = {
            "application": application,
            "rows": rows,
            "today": today,
            "may_manage": may_manage,
            "branch_form": (
                ApplicationBranchForm(actor=self.actor, application=application)
                if may_manage
                else None
            ),
            "page_title": application.name,
            "page_hint": _("الفروع المفعّلة مع هذا التطبيق، والاتفاقية السارية اليوم لكل فرع."),
            # `_form_fragment.html`, not `_list_fragment.html`. This template
            # extends `list_base_template` **directly** rather than through
            # `settings/base_list.html`, so the block it defines is `page`;
            # `_list_fragment.html` contains only `results`, Django silently
            # drops a child block the parent does not declare, and the htmx
            # form of this screen answered 200 with an empty body.
            "list_base_template": (
                "settings/_form_fragment.html"
                if request.headers.get("HX-Request") == "true"
                else "shell.html"
            ),
        }
        return render(request, "sales/delivery_application_detail.html", context)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        application = resolve_delivery_application(self.actor, kwargs["pk"])
        require_reachable_organization_permission(
            self.actor, MANAGE_DELIVERY_APPLICATIONS, application.organization
        )
        form = ApplicationBranchForm(request.POST, actor=self.actor, application=application)
        if form.is_valid():
            try:
                set_application_branch_setting(
                    application=application,
                    branch=form.cleaned_data["branch"],
                    is_active=form.cleaned_data["is_active"],
                    external_store_code=form.cleaned_data.get("external_store_code", ""),
                    notes=form.cleaned_data.get("notes", ""),
                )
            except ValidationError as error:
                messages.error(request, "؛ ".join(str(message) for message in error.messages))
            else:
                messages.success(request, _("تم تحديث تفعيل الفرع."))
        else:
            messages.error(request, _("تعذّر حفظ التفعيل. راجع الحقول."))
        return HttpResponseRedirect(reverse("sales:application_detail", args=[application.pk]))


# ---------------------------------------------------------------------------
# Agreements — checkpoint 2
# ---------------------------------------------------------------------------


class DeliveryAgreementListView(SalesListView):
    template_name = "sales/agreement_list.html"
    context_object_name = "agreements"
    page_title = _("العمولات والاتفاقيات")
    page_hint = _(
        "العمولة تُستحق عند البيع من الاتفاقية السارية، لا عند التسوية: النسبة معروفة "
        "يوم استلام الطلب، وانتظار الكشف يجعل هامش الشهر مجهولاً حتى الشهر التالي."
    )
    search_fields = (
        "delivery_application__code",
        "delivery_application__name",
        "branch__code",
        "evidence_reference",
    )
    manage_permission = MANAGE_SALES_AGREEMENTS
    manage_scope = "branch"
    create_url_name = "sales:agreement_create"
    create_label = _("اتفاقية جديدة")
    result_label = _("اتفاقية")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_agreements(self.actor)
        if self.request.GET.get("current", "").strip() == "1":
            today = timezone.localdate()
            queryset = queryset.filter(is_active=True, effective_from__lte=today).filter(
                Q(effective_to__isnull=True) | Q(effective_to__gte=today)
            )
        return queryset.order_by("delivery_application__code", "branch__code", "-effective_from")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["only_current"] = self.request.GET.get("current", "")
        context["today"] = timezone.localdate()
        # A worked example beside every agreement. A percentage and a fixed fee
        # read as harmless separately; the money does not. Attached to the row
        # rather than passed as a parallel mapping, so the template reads
        # `agreement.preview` instead of looking a key up inside a loop.
        context["preview_amount"] = PREVIEW_AMOUNT
        for row in context.get("agreements", []):
            row.preview = preview(row, gross_amount=PREVIEW_AMOUNT, order_count=1)
        return context


class DeliveryAgreementCreateView(SalesWriteView):
    form_class = DeliveryAgreementForm
    required_permission = MANAGE_SALES_AGREEMENTS
    success_url_name = "sales:agreement_list"
    page_title = _("اتفاقية عمولة جديدة")
    page_hint = _(
        "لا يوجد تعديل لاتفاقية: النسبة التي استُحقت عليها عمولة صارت مستنداً، "
        "وتصحيحها في مكانها يعيد صياغة مصروف مرحّل. التصحيح هو إنهاؤها وتسجيل البديلة."
    )
    success_message = _("تمت إضافة الاتفاقية.")

    def authorize(self, instance: Any, form: Any) -> None:
        # `require_organization_permission`, matching the scope the permission
        # table declares. `manage_sales_agreements` is `ORGANIZATION_AUTHORITY`
        # precisely because an agreement decides what every future order at that
        # branch is worth, and ADR-016 defines that scope as an
        # `OrganizationMembership` — a branch post is not one. Enforcing it at
        # branch scope let a bare branch manager set the commission rate their
        # own branch trades at, which is the case the declared scope exists to
        # refuse.
        require_organization_permission(
            self.actor, MANAGE_SALES_AGREEMENTS, form.cleaned_data["branch"].organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        create_delivery_agreement(
            branch=data["branch"],
            delivery_application=data["delivery_application"],
            effective_from=data["effective_from"],
            commission_percent=data["commission_percent"],
            fixed_fee_per_order=data["fixed_fee_per_order"],
            commission_basis=data["commission_basis"],
            settlement_lag_days=data["settlement_lag_days"],
            effective_to=data.get("effective_to"),
            evidence_reference=data["evidence_reference"],
            notes=data.get("notes", ""),
        )


class DeliveryAgreementCloseView(SalesWriteView):
    form_class = AgreementCloseForm
    required_permission = MANAGE_SALES_AGREEMENTS
    success_url_name = "sales:agreement_list"
    page_title = _("إنهاء اتفاقية")
    page_hint = _("النسبة والرسم لا يُعدَّلان. تُنهى الاتفاقية بتاريخ، وتُسجَّل البديلة.")
    success_message = _("تم إنهاء الاتفاقية.")

    def build_form(self, instance: Any, data: Any = None) -> Any:
        return self.form_class(**({"data": data} if data is not None else {}))

    def load(self) -> Any:
        return resolve_agreement_row(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any, form: Any) -> None:
        # The same declared scope as creating one. Ending an agreement is the
        # only way its rate ever changes, so it is exactly as consequential.
        require_organization_permission(
            self.actor, MANAGE_SALES_AGREEMENTS, instance.branch.organization
        )

    def perform(self, instance: Any, form: Any) -> None:
        close_delivery_agreement(
            agreement=instance,
            effective_to=form.cleaned_data["effective_to"],
            reason=form.cleaned_data["reason"],
        )


# ---------------------------------------------------------------------------
# Discounts — checkpoint 2
# ---------------------------------------------------------------------------


class DiscountProgramListView(SalesListView):
    template_name = "sales/discount_list.html"
    context_object_name = "programs"
    page_title = _("الخصومات")
    page_hint = _(
        "الخصم مال لم يُحصَّل، والتصميم كله يقوم على تسجيل مَن تحمّله: حصة المطعم "
        "تخفض إيراده، وحصة التطبيق تُعوَّض ولا تخفض شيئاً — بل هي جزء مما يدين به التطبيق."
    )
    search_fields = ("code", "name")
    manage_permission = MANAGE_SALES_DISCOUNTS
    create_url_name = "sales:discount_create"
    create_label = _("خصم جديد")
    result_label = _("برنامج خصم")

    def scoped_queryset(self) -> QuerySet[Any]:
        queryset = visible_discount_programs(self.actor)
        if self.request.GET.get("current", "").strip() == "1":
            today = timezone.localdate()
            queryset = queryset.filter(is_active=True, effective_from__lte=today).filter(
                Q(effective_to__isnull=True) | Q(effective_to__gte=today)
            )
        funding = self.request.GET.get("funding", "").strip()
        if funding == "restaurant":
            queryset = queryset.filter(application_funded_share=0)
        elif funding == "application":
            queryset = queryset.filter(restaurant_funded_share=0)
        elif funding == "shared":
            queryset = queryset.filter(
                application_funded_share__gt=0, restaurant_funded_share__gt=0
            )
        return queryset.order_by("code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["only_current"] = self.request.GET.get("current", "")
        context["selected_funding"] = self.request.GET.get("funding", "")
        context["today"] = timezone.localdate()
        return context


class DiscountProgramCreateView(SalesWriteView):
    form_class = DiscountProgramForm
    required_permission = MANAGE_SALES_DISCOUNTS
    success_url_name = "sales:discount_list"
    page_title = _("برنامج خصم جديد")
    page_hint = _(
        "حصتا التمويل يجب أن تساويا الخصم كاملاً. خصم يموّله تطبيق يجب أن يسمّي "
        "التطبيق: وإلا فالذمة التي يفترضها على لا أحد."
    )
    success_message = _("تمت إضافة الخصم.")

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(
            self.actor, MANAGE_SALES_DISCOUNTS, form.selected_organization()
        )

    def perform(self, instance: Any, form: Any) -> None:
        data = form.cleaned_data
        create_discount_program(
            organization=form.selected_organization(),
            code=data["code"],
            name=data["name"],
            effective_from=data["effective_from"],
            discount_percent=data.get("discount_percent"),
            discount_amount=data.get("discount_amount"),
            maximum_amount=data.get("maximum_amount"),
            restaurant_funded_share=data["restaurant_funded_share"],
            application_funded_share=data["application_funded_share"],
            branch=data.get("branch"),
            channel=data.get("channel"),
            delivery_application=data.get("delivery_application"),
            menu_item=data.get("menu_item"),
            effective_to=data.get("effective_to"),
            evidence_reference=data.get("evidence_reference", ""),
            notes=data.get("notes", ""),
        )


class DiscountProgramCloseView(SalesWriteView):
    form_class = DiscountCloseForm
    required_permission = MANAGE_SALES_DISCOUNTS
    success_url_name = "sales:discount_list"
    page_title = _("إنهاء برنامج خصم")
    page_hint = _("المبالغ وحصص التمويل لا تُعدَّل. يُنهى البرنامج بتاريخ.")
    success_message = _("تم إنهاء البرنامج.")

    def build_form(self, instance: Any, data: Any = None) -> Any:
        return self.form_class(**({"data": data} if data is not None else {}))

    def load(self) -> Any:
        return resolve_discount_program(self.actor, self.kwargs["pk"])

    def authorize(self, instance: Any, form: Any) -> None:
        require_organization_permission(self.actor, MANAGE_SALES_DISCOUNTS, instance.organization)

    def perform(self, instance: Any, form: Any) -> None:
        close_discount_program(
            program=instance,
            effective_to=form.cleaned_data["effective_to"],
            reason=form.cleaned_data["reason"],
        )


__all__ = [
    "DeliveryAgreementCloseView",
    "DeliveryAgreementCreateView",
    "DeliveryAgreementListView",
    "DeliveryApplicationActionView",
    "DeliveryApplicationCreateView",
    "DeliveryApplicationDetailView",
    "DeliveryApplicationListView",
    "DeliveryApplicationUpdateView",
    "DiscountProgramCloseView",
    "DiscountProgramCreateView",
    "DiscountProgramListView",
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
