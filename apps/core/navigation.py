"""
Application navigation.

Declared as data rather than scattered through templates so that the modules
and their sections stay in one reviewable place, and so a section that has no
implementation yet is *shown as unavailable* rather than linking to a 404.

`available=False` items are rendered muted and inert. They are deliberately
visible: the shell should show the shape of the finished system, and hiding
unbuilt modules would make the navigation change under users as phases land.

Modules follow the approved build order in the architecture charter:
Foundations, Inventory, Procurement, Recipes, Sales, Accounting, HR, Reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.utils.functional import Promise
from django.utils.safestring import SafeString, mark_safe
from django.utils.translation import gettext_lazy as _

#: Labels are lazily translated, so they are promises until a template
#: renders them under an active language.
Label = str | Promise

# --------------------------------------------------------------------------
# Icons
# --------------------------------------------------------------------------
# Inline SVG bodies. Author-written constants, never user input, so marking
# them safe is sound. Stroke-based so they inherit the current text colour.


def _icon(paths: str) -> SafeString:
    return mark_safe(  # noqa: S308 - author-authored constant, not user input
        '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.7" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        f"{paths}</svg>"
    )


ICONS = {
    "home": _icon('<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/>'),
    "box": _icon(
        '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5"/><path d="M12 13v8"/>'
    ),
    "cart": _icon(
        '<circle cx="9" cy="20" r="1.4"/><circle cx="18" cy="20" r="1.4"/>'
        '<path d="M2 3h3l2.4 12.2a1.5 1.5 0 0 0 1.5 1.2h8.6a1.5 1.5 0 0 0 1.5-1.2L21 7H6"/>'
    ),
    "chef": _icon(
        '<path d="M7 21h10"/><path d="M6 17h12v-2H6z"/>'
        '<path d="M7.5 15a4.5 4.5 0 1 1 1.6-8.7 3.6 3.6 0 0 1 5.8 0A4.5 4.5 0 1 1 16.5 15"/>'
    ),
    "receipt": _icon(
        '<path d="M6 2h12v20l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6"/><path d="M9 12h6"/>'
    ),
    "ledger": _icon(
        '<path d="M4 4h13a3 3 0 0 1 3 3v13H7a3 3 0 0 1-3-3z"/>'
        '<path d="M4 17h16"/><path d="M9 8h7"/>'
    ),
    "people": _icon(
        '<circle cx="9" cy="8" r="3.2"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/>'
        '<path d="M16 5.5a3.2 3.2 0 0 1 0 6"/><path d="M17.5 14.2A6.5 6.5 0 0 1 21.5 20"/>'
    ),
    "chart": _icon(
        '<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M21 20H3"/>'
    ),
    "settings": _icon(
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1'
        "a2 2 0 1 1-4 0 1.6 1.6 0 0 0-2.7-1.1l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 3 15"
        "a2 2 0 1 1 0-4 1.6 1.6 0 0 0 1.1-2.7l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 9 4.6"
        "a2 2 0 1 1 4 0 1.6 1.6 0 0 0 2.7 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.6 1.6 0 0 0 19.4 11"
        'a2 2 0 1 1 0 4z"/>'
    ),
}


@dataclass(frozen=True)
class Section:
    """One entry inside a module's sidebar."""

    label: Label
    url_name: str | None = None
    available: bool = False
    group: Label = ""
    active_prefixes: tuple[str, ...] = field(default_factory=tuple)
    icon_name: str | None = None

    @property
    def icon(self) -> SafeString | str:
        """Return an optional, author-owned navigation icon."""
        if not self.icon_name:
            return ""
        return ICONS.get(self.icon_name, "")


@dataclass(frozen=True)
class Module:
    """One entry in the module rail, with its own sidebar of sections."""

    key: str
    label: Label
    icon_name: str
    phase: Label
    url_name: str | None = None
    available: bool = False
    sections: tuple[Section, ...] = field(default_factory=tuple)

    @property
    def icon(self) -> SafeString:
        return ICONS[self.icon_name]


def _sections(*labels: Label) -> tuple[Section, ...]:
    """Sections that have no implementation yet."""
    return tuple(Section(label=label) for label in labels)


MODULES: tuple[Module, ...] = (
    Module(
        key="home",
        label=_("الرئيسية"),
        icon_name="home",
        phase=_("الأساس"),
        url_name="users:home",
        available=True,
        sections=(Section(label=_("نظرة عامة"), url_name="users:home", available=True),),
    ),
    Module(
        key="inventory",
        label=_("المخزون"),
        icon_name="box",
        phase=_("المرحلة ١"),
        url_name="inventory:overview",
        available=True,
        sections=(
            # The module opens on its summary; every deeper screen is reached
            # from it. Matched by exact url name, so it never steals the active
            # state from the list it links to.
            Section(
                label=_("نظرة عامة"),
                url_name="inventory:overview",
                available=True,
                group=_("نظرة عامة"),
            ),
            # Navigation follows the operator's workflow instead of the order
            # in which Phase 1 happened to be implemented. `active_prefixes`
            # keeps create, detail and action screens anchored to their list.
            Section(
                label=_("الأصناف"),
                url_name="inventory:item_list",
                available=True,
                group=_("البيانات الأساسية"),
                active_prefixes=("inventory:item_",),
            ),
            Section(
                label=_("مجموعات الأصناف"),
                url_name="inventory:category_list",
                available=True,
                group=_("البيانات الأساسية"),
                active_prefixes=("inventory:category_",),
            ),
            Section(
                label=_("وحدات التعبئة"),
                url_name="inventory:package_unit_list",
                available=True,
                group=_("البيانات الأساسية"),
                active_prefixes=("inventory:package_unit_",),
            ),
            Section(
                label=_("تحويلات وحدات الصنف"),
                url_name="inventory:conversion_list",
                available=True,
                group=_("البيانات الأساسية"),
                active_prefixes=("inventory:conversion_",),
            ),
            Section(
                label=_("المخازن"),
                url_name="inventory:warehouse_list",
                available=True,
                group=_("البيانات الأساسية"),
                active_prefixes=("inventory:warehouse_",),
            ),
            # The account mapping is the last survivor of what used to be
            # its own "الضبط والمطابقة" group. It sits with the master data
            # because `regroup` only merges *adjacent* sections: left where
            # it was, it would open a second "البيانات الأساسية" block
            # further down the sidebar.
            Section(
                label=_("ربط حسابات المخزون"),
                url_name="inventory:mapping_list",
                available=True,
                group=_("البيانات الأساسية"),
                active_prefixes=("inventory:mapping_",),
            ),
            Section(
                label=_("المخزون المتوفر"),
                url_name="inventory:stock_list",
                available=True,
                group=_("الرصيد والحركة"),
            ),
            Section(
                label=_("حركة المخزون"),
                url_name="inventory:movement_list",
                available=True,
                group=_("الرصيد والحركة"),
                active_prefixes=("inventory:movement_",),
            ),
            Section(
                label=_("الأرصدة الافتتاحية"),
                url_name="inventory:opening_list",
                available=True,
                group=_("الحركات المخزنية"),
                active_prefixes=("inventory:opening_",),
            ),
            Section(
                label=_("صرف مخزني للاستهلاك"),
                url_name="inventory:inventory_issue_list",
                available=True,
                group=_("الحركات المخزنية"),
                active_prefixes=("inventory:inventory_issue_",),
            ),
            Section(
                label=_("التحويلات المخزنية"),
                url_name="inventory:transfer_list",
                available=True,
                group=_("الحركات المخزنية"),
                active_prefixes=("inventory:transfer_",),
            ),
            Section(
                label=_("إتلاف مخزني"),
                url_name="inventory:inventory_waste_list",
                available=True,
                group=_("الحركات المخزنية"),
                active_prefixes=("inventory:inventory_waste_",),
            ),
            Section(
                label=_("الجرد الفعلي"),
                url_name="inventory:count_list",
                available=True,
                group=_("الجرد والتسويات"),
                active_prefixes=("inventory:count_",),
            ),
            Section(
                label=_("التسويات المخزنية"),
                url_name="inventory:adjustment_list",
                available=True,
                group=_("الجرد والتسويات"),
                active_prefixes=("inventory:adjustment_",),
            ),
            Section(
                label=_("تقييم المخزون"),
                url_name="inventory:report_valuation",
                available=True,
                group=_("التقارير"),
            ),
            Section(
                label=_("بطاقة الصنف"),
                url_name="inventory:report_stock_card",
                available=True,
                group=_("التقارير"),
            ),
            Section(
                label=_("الصلاحية"),
                url_name="inventory:report_expiry",
                available=True,
                group=_("التقارير"),
            ),
            Section(
                label=_("حدود إعادة الطلب"),
                url_name="inventory:report_reorder",
                available=True,
                group=_("التقارير"),
            ),
            Section(
                label=_("ملخص الإتلاف"),
                url_name="inventory:report_waste",
                available=True,
                group=_("التقارير"),
            ),
            Section(
                label=_("فروقات الجرد"),
                url_name="inventory:report_count_variance",
                available=True,
                group=_("التقارير"),
            ),
            Section(
                label=_("تقرير التسويات"),
                url_name="inventory:report_adjustments",
                available=True,
                group=_("التقارير"),
            ),
            Section(
                label=_("أرصدة المواقع"),
                url_name="inventory:report_locations",
                available=True,
                group=_("التقارير"),
            ),
        ),
    ),
    Module(
        key="procurement",
        label=_("المشتريات"),
        icon_name="cart",
        phase=_("المرحلة ٢"),
        url_name="procurement:overview",
        available=True,
        sections=(
            # The module opens on its summary; every deeper screen is reached
            # from it. Matched by exact url name, so it never steals the active
            # state from the list it links to.
            Section(
                label=_("نظرة عامة"),
                url_name="procurement:overview",
                available=True,
                group=_("نظرة عامة"),
                icon_name="home",
            ),
            # Task 2.1 — the supplier master. Built and reachable.
            Section(
                label=_("المشتريات"),
                url_name="procurement:supplier_list",
                available=True,
                icon_name="people",
            ),
            Section(
                label=_("عروض الموردين المستقلة"),
                url_name="supplier_quotes:list",
                available=True,
                active_prefixes=("supplier_quotes:",),
                icon_name="chart",
            ),
            # The supplier invoice. Its comment used to say the backing
            # documents "do not exist" — they had existed since Task 2.12 and
            # the entry stayed inert anyway, which is the failure mode where a
            # stale note outlives the thing it described. `SupplierInvoice`,
            # `SupplierInvoiceLine`, `SupplierInvoicePosting`, `PurchaseMatch`,
            # matching, GRNI, PPV, payments and credit notes are all built.
            Section(
                label=_("فواتير المشتريات"),
                url_name="procurement:supplier_invoice_list",
                available=True,
                active_prefixes=("/procurement/invoices/",),
                icon_name="receipt",
            ),
            # Task 2.13 — supplier returns. Built and reachable; the entry the
            # inventory module gave up ("returns belong to Procurement, where
            # they reconcile against an invoice and a credit note") lands here.
            Section(
                label=_("مرتجعات الموردين"),
                url_name="procurement:supplier_return_list",
                available=True,
                icon_name="box",
            ),
            # تسديد الموردين — deciding what to pay, which is a different act
            # from reading what was paid, and comes before it. It drafts an
            # ordinary payment; the list below is where that payment is posted.
            Section(
                label=_("تسديد الموردين"),
                url_name="procurement:settlement_workspace",
                available=True,
                active_prefixes=("procurement:settlement_workspace",),
                icon_name="cart",
            ),
            # Task 2.15 — money out. Allocation lives on the payment's own
            # detail screen, so "تخصيص الدفعات" needs no separate route.
            Section(
                label=_("دفعات الموردين"),
                url_name="procurement:supplier_payment_list",
                available=True,
                icon_name="ledger",
            ),
        ),
    ),
    Module(
        key="kitchen",
        label=_("المطبخ والوصفات"),
        icon_name="chef",
        phase=_("المرحلة ٣"),
        url_name="kitchen:overview",
        available=True,
        sections=(
            # The module opens on its summary; every deeper screen is reached
            # from it. Matched by exact url name, so it never steals the active
            # state from the list it links to.
            Section(
                label=_("نظرة عامة"),
                url_name="kitchen:overview",
                available=True,
                group=_("نظرة عامة"),
            ),
            # Task 3.1 — the recipe master and its draft structure. Built and
            # reachable. Everything below stays inert until its own task
            # lands: showing the shape of the finished module is deliberate,
            # linking to a screen that does not exist is not.
            Section(label=_("الوصفات"), url_name="kitchen:recipe_list", available=True),
            Section(
                label=_("مجموعات الوصفات"),
                url_name="kitchen:category_list",
                available=True,
            ),
            # Task 3.2A — the version lifecycle. Reachable now that approval,
            # effective dating and immutability all exist behind it.
            Section(
                label=_("نسخ الوصفات"),
                url_name="kitchen:version_list",
                available=True,
            ),
            # Task 3.3 - costing. Exactly one previously inert entry is
            # promoted, and its label names both figures the screen carries:
            # the full recipe cost card, and the plate cost derived from the
            # version's primary serving. `كلفة الطبق` alone would undersell a
            # screen that shows the whole card as its evidence.
            #
            # The entry renders for everyone; the screens behind it refuse
            # anybody without `view_recipe_cost`. That is deliberate and matches
            # every other module here - navigation describes the system, and
            # authorization is decided where the data is.
            Section(
                label=_("كلفة الوصفة والطبق"),
                url_name="kitchen:cost_snapshot_list",
                available=True,
            ),
            # Task 3.4 - production drafting. Exactly one previously inert entry
            # is promoted, and it keeps the label it always had: `أوامر الإنتاج`
            # is what the kitchen calls the document, and renaming it to
            # "production drafts" would describe the current task rather than the
            # thing the entry leads to.
            #
            # What is behind it is a **draft** and nothing more: no posting, no
            # stock movement, no journal. The screens say so plainly rather than
            # offering a disabled control, because a greyed-out "post" button
            # tells an operator that posting is one permission away when in fact
            # the service does not exist.
            Section(
                label=_("أوامر الإنتاج"),
                url_name="kitchen:production_list",
                available=True,
            ),
            # Task 3.6 - four more entries promoted, and only four. Each one
            # leads to a screen that exists and renders; the rest stay inert
            # until their own task builds them, because an active entry that
            # 404s is worse than an obviously unfinished one.
            #
            # The two custody entries keep the kitchen's own words. الصرف للمطبخ
            # is what the storekeeper calls handing goods over, and it is a
            # **custody** movement: nothing has been consumed until a batch
            # cooks it or somebody issues it out.
            Section(
                label=_("الإنتاجية والفاقد"),
                url_name="kitchen:report_productivity",
                available=True,
            ),
            Section(
                label=_("الصرف للمطبخ"),
                url_name="kitchen:report_kitchen_issue",
                available=True,
            ),
            Section(
                label=_("المرتجع من المطبخ"),
                url_name="kitchen:report_kitchen_return",
                available=True,
            ),
            Section(
                label=_("الهالك"),
                url_name="kitchen:report_kitchen_waste",
                available=True,
            ),
            # Task 3.7. Both meal entries promoted together, because they are
            # the same screen with a different reason on it. Nothing behind
            # either one moves stock or writes a journal, and each page says so
            # in a sentence rather than leaving the reader to notice.
            Section(
                label=_("وجبات الموظفين"),
                url_name="kitchen:meal_staff_list",
                available=True,
            ),
            Section(
                label=_("الوجبات المجانية"),
                url_name="kitchen:meal_complimentary_list",
                available=True,
            ),
            # Task 3.8 - the last three entries promoted, and the module's
            # section list is now complete. Each one leads to a screen that
            # exists, renders, and is populated by the demo seed.
            #
            # Two of the three carry a **coverage limitation** rather than a
            # finished answer, and that is deliberate rather than unfinished.
            # `الاستهلاك النظري` reports staff and complimentary meals and says
            # in a sentence that approved sales quantities arrive in Phase 4;
            # `انحراف الاستهلاك` shows a complete production standard variance
            # and a partial diagnostic labelled `PARTIAL_COVERAGE` /
            # `NOT_FINAL_USAGE_VARIANCE`. Neither pretends to be sales-complete.
            #
            # Activating them with the limitation stated beats leaving them
            # inert: an operator who needs to know what the kitchen consumed can
            # now read it, and can see exactly which part of the picture is
            # missing. An inert entry answers neither question.
            Section(
                label=_("الاستهلاك الفعلي"),
                url_name="kitchen:report_actual_consumption",
                available=True,
            ),
            Section(
                label=_("الاستهلاك النظري"),
                url_name="kitchen:report_theoretical_consumption",
                available=True,
            ),
            Section(
                label=_("انحراف الاستهلاك"),
                url_name="kitchen:report_usage_variance",
                available=True,
            ),
            # Two further screens Task 3.8 built. They are not in the approved
            # thirteen, so they are additions rather than promotions: the
            # partition is what proves every consumption figure above it, and
            # the standard requirement report is where a production deviation
            # is actually diagnosed.
            Section(
                label=_("تدفق مخزن المطبخ"),
                url_name="kitchen:report_warehouse_flow",
                available=True,
            ),
            Section(
                label=_("متطلبات الإنتاج القياسية"),
                url_name="kitchen:report_production_standard",
                available=True,
            ),
        ),
    ),
    Module(
        key="sales",
        label=_("المبيعات"),
        icon_name="receipt",
        phase=_("المرحلة ٤"),
        # Checkpoint 7 — the module now opens on its own dashboard rather than
        # on the menu. Until لوحة المبيعات existed the menu was the honest
        # landing page, being the master everything else in Phase 4 is built
        # on; a module whose landing page is a summary of its own documents is
        # the shape every other module here has.
        url_name="sales:dashboard",
        available=True,
        sections=(
            # Checkpoint 7 — the twelfth and last entry. `_sections(...)` is
            # empty for this module now: every section in the sidebar leads to
            # a screen that exists, renders as a full page and as an htmx
            # fragment, and is populated. There is no قريباً badge left in
            # Sales.
            Section(
                label=_("لوحة المبيعات"),
                url_name="sales:dashboard",
                available=True,
                active_prefixes=("/sales/dashboard/",),
            ),
            # Checkpoint 3 — the module's operational centre. One document per
            # branch per business date, and the first Sales screen that reaches
            # the ledger.
            Section(
                label=_("المبيعات اليومية"),
                url_name="sales:day_list",
                available=True,
                active_prefixes=("/sales/days/", "/sales/day-lines/"),
            ),
            Section(
                label=_("استيراد مبيعات POS"),
                url_name="sales:pos_import_list",
                available=True,
                active_prefixes=("/sales/pos-imports/",),
            ),
            Section(
                label=_("أصناف المنيو"),
                url_name="sales:menu_item_list",
                available=True,
                active_prefixes=(
                    "/sales/menu-items/",
                    "/sales/menu-categories/",
                    "/sales/menu-prices/",
                ),
            ),
            Section(
                label=_("قنوات البيع"),
                url_name="sales:channel_list",
                available=True,
                active_prefixes=("/sales/channels/",),
            ),
            # Checkpoint 2 — three more entries promoted. The delivery master
            # and the two contract screens travel together because they are
            # useless apart: an application with no agreement refuses every
            # sale it takes, and a discount that names no application cannot
            # state who funds it.
            Section(
                label=_("تطبيقات التوصيل"),
                url_name="sales:application_list",
                available=True,
                active_prefixes=("/sales/applications/",),
            ),
            Section(
                label=_("العمولات والاتفاقيات"),
                url_name="sales:agreement_list",
                available=True,
                active_prefixes=("/sales/agreements/",),
            ),
            Section(
                label=_("الخصومات"),
                url_name="sales:discount_list",
                available=True,
                active_prefixes=("/sales/discounts/",),
            ),
            # Checkpoint 4 — corrections against posted days. Promoted after
            # the list and the detail both answered as a full page and as an
            # htmx fragment; the line-delete prefix is listed so the sidebar
            # still highlights the section while a draft is being edited.
            Section(
                label=_("المرتجعات والإلغاءات"),
                url_name="sales:adjustment_list",
                available=True,
                active_prefixes=("/sales/adjustments/", "/sales/adjustment-lines/"),
            ),
            # Checkpoint 5 — the receivable ledger and the settlement that
            # clears it. Two entries rather than one, because reading what a
            # delivery company owes and agreeing its statement are different
            # acts held by different permissions: the read is
            # `view_application_receivables` and reaches a viewer, the
            # settlement is `manage_application_settlements` and reaches
            # neither a branch manager nor an accountant.
            Section(
                label=_("ذمم التطبيقات"),
                url_name="sales:receivable_list",
                available=True,
                active_prefixes=("/sales/receivables/",),
            ),
            Section(
                label=_("تسويات التطبيقات"),
                url_name="sales:settlement_list",
                available=True,
                active_prefixes=(
                    "/sales/settlements/",
                    "/sales/settlement-allocations/",
                    "/sales/settlement-adjustments/",
                ),
            ),
            # Checkpoint 6 — the till and the report that reads everything the
            # module has produced. Two entries rather than one, because closing
            # a drawer and reading a reconciliation are different acts held by
            # different permissions: the closing is `close_cashier_shift` /
            # `approve_cashier_closing` at the branch, and the report is
            # `view_sales_reports` and records nothing at all.
            Section(
                label=_("إقفال الكاشير"),
                url_name="sales:shift_list",
                available=True,
                active_prefixes=("/sales/cashier-shifts/",),
            ),
            Section(
                label=_("المطابقة اليومية"),
                url_name="sales:report_daily_reconciliation",
                available=True,
                active_prefixes=("/sales/reports/daily-reconciliation/",),
            ),
        ),
    ),
    Module(
        key="accounting",
        label=_("المحاسبة"),
        icon_name="ledger",
        phase=_("المرحلة ٥"),
        # The module landing page. Phase 5 gives Accounting a dashboard of its
        # own rather than pointing the module at whichever screen happened to
        # exist first.
        url_name="accounting:dashboard",
        available=True,
        sections=(
            Section(
                label=_("لوحة المحاسبة"),
                url_name="accounting:dashboard",
                available=True,
                group=_("نظرة عامة"),
                icon_name="chart",
            ),
            Section(
                label=_("الشجرة المحاسبية"),
                url_name="accounting:chart_tree",
                available=True,
                group=_("الهيكل المحاسبي"),
                active_prefixes=("/accounting/accounts/",),
                icon_name="ledger",
            ),
            Section(
                label=_("الأصول"),
                url_name="accounting:asset_overview",
                available=True,
                group=_("الهيكل المحاسبي"),
                active_prefixes=("/accounting/assets/",),
                icon_name="box",
            ),
            Section(
                label=_("مراكز التكلفة"),
                url_name="accounting:cost_center_list",
                available=True,
                group=_("الهيكل المحاسبي"),
                active_prefixes=("/accounting/cost-centers/",),
                icon_name="chart",
            ),
            Section(
                label=_("المصروفات"),
                url_name="accounting:expense_list",
                available=True,
                group=_("العمليات"),
                active_prefixes=("/accounting/expenses/",),
                icon_name="receipt",
            ),
            Section(
                label=_("التكاليف الإضافية"),
                url_name="accounting:additional_cost_list",
                available=True,
                group=_("العمليات"),
                active_prefixes=("/accounting/additional-costs/",),
                icon_name="receipt",
            ),
            Section(
                label=_("سندات القيود"),
                url_name="accounting:journal_list",
                available=True,
                group=_("العمليات"),
                active_prefixes=("/accounting/journals/",),
                icon_name="ledger",
            ),
            Section(
                label=_("ذمم الموردين"),
                url_name="accounting:supplier_liability_list",
                available=True,
                group=_("الذمم"),
                active_prefixes=("/accounting/supplier-liabilities/",),
                icon_name="cart",
            ),
            Section(
                label=_("ذمم التطبيقات"),
                url_name="accounting:application_receivable_list",
                available=True,
                group=_("الذمم"),
                active_prefixes=("/accounting/application-receivables/",),
                icon_name="receipt",
            ),
            Section(
                label=_("الفترات المحاسبية"),
                url_name="accounting:period_list",
                available=True,
                group=_("الفترات والقوائم"),
                active_prefixes=("/accounting/periods/",),
                icon_name="ledger",
            ),
            Section(
                label=_("ميزان المراجعة"),
                url_name="accounting:trial_balance",
                available=True,
                group=_("الفترات والقوائم"),
                active_prefixes=("/accounting/reports/trial-balance/",),
                icon_name="chart",
            ),
            Section(
                label=_("قائمة الدخل"),
                url_name="accounting:income_statement",
                available=True,
                group=_("الفترات والقوائم"),
                active_prefixes=("/accounting/reports/income-statement/",),
                icon_name="chart",
            ),
            Section(
                label=_("الميزانية العمومية"),
                url_name="accounting:balance_sheet",
                available=True,
                group=_("الفترات والقوائم"),
                active_prefixes=("/accounting/reports/balance-sheet/",),
                icon_name="chart",
            ),
            Section(
                label=_("دفتر الأستاذ"),
                url_name="accounting:general_ledger",
                available=True,
                group=_("أدوات متقدمة"),
                active_prefixes=("/accounting/reports/general-ledger/",),
            ),
            Section(
                label=_("الصناديق"),
                url_name="accounting:cashbox_list",
                available=True,
                group=_("أدوات متقدمة"),
                active_prefixes=("/accounting/cashboxes/",),
            ),
            Section(
                label=_("الحسابات البنكية"),
                url_name="accounting:bank_account_list",
                available=True,
                group=_("أدوات متقدمة"),
                active_prefixes=("/accounting/bank-accounts/",),
            ),
            Section(
                label=_("المستحقات والمقدمات"),
                url_name="accounting:deferral_list",
                available=True,
                group=_("أدوات متقدمة"),
                active_prefixes=("/accounting/deferrals/",),
            ),
            Section(
                label=_("الأدوار المحاسبية"),
                url_name="accounting:role_list",
                available=True,
                group=_("أدوات متقدمة"),
            ),
            Section(
                label=_("ربط الحسابات"),
                url_name="accounting:mapping_list",
                available=True,
                group=_("أدوات متقدمة"),
            ),
        ),
    ),
    Module(
        key="hr",
        label=_("الموارد البشرية"),
        icon_name="people",
        phase=_("المرحلة ٦"),
        url_name="hr:overview",
        available=True,
        sections=(
            # The module opens on its summary; every deeper screen is reached
            # from it. Matched by exact url name, so it never steals the active
            # state from the list it links to.
            Section(
                label=_("نظرة عامة"),
                url_name="hr:overview",
                available=True,
                group=_("نظرة عامة"),
            ),
            Section(
                label=_("الموظفون"),
                url_name="hr:employee_list",
                available=True,
                active_prefixes=("hr:employee_",),
            ),
            Section(
                label=_("العقود والأجور"),
                url_name="hr:contract_list",
                available=True,
                active_prefixes=("hr:contract_",),
            ),
            Section(
                label=_("الورديات"),
                url_name="hr:shift_list",
                available=True,
                active_prefixes=("hr:shift_", "hr:employee_schedule", "hr:branch_schedule"),
            ),
            Section(
                label=_("الحضور والانصراف"),
                url_name="hr:attendance_list",
                available=True,
                active_prefixes=("hr:attendance_",),
            ),
            Section(
                label=_("الإجازات والغياب"),
                url_name="hr:leave_list",
                available=True,
                active_prefixes=("hr:leave_", "hr:absence_"),
            ),
            Section(
                label=_("العمل الإضافي"),
                url_name="hr:overtime_list",
                available=True,
                active_prefixes=("hr:overtime_",),
            ),
            Section(
                label=_("الاستقطاعات"),
                url_name="hr:deduction_list",
                available=True,
                active_prefixes=("hr:deduction_",),
            ),
            Section(
                label=_("السلف"),
                url_name="hr:advance_list",
                available=True,
                active_prefixes=("hr:advance_",),
            ),
            Section(
                label=_("احتساب الرواتب"),
                url_name="hr:payroll_list",
                available=True,
                active_prefixes=(
                    "hr:payroll_list",
                    "hr:payroll_create",
                    "hr:payroll_detail",
                    "hr:payroll_line",
                    "hr:payroll_command",
                ),
            ),
            Section(
                label=_("اعتماد الرواتب"),
                url_name="hr:payroll_approvals",
                available=True,
                active_prefixes=("hr:payroll_approvals",),
            ),
            Section(
                label=_("صرف الرواتب"),
                url_name="hr:payroll_payments",
                available=True,
                active_prefixes=("hr:payroll_payment", "hr:payroll_payments"),
            ),
            Section(
                label=_("كشوف الموظفين"),
                url_name="hr:statement_list",
                available=True,
                active_prefixes=("hr:statement_", "hr:employee_statement", "hr:payslip"),
            ),
        ),
    ),
    Module(
        key="reports",
        label=_("التقارير"),
        icon_name="chart",
        phase=_("المرحلة ٧"),
        url_name="kitchen:report_recipe_cost",
        available=True,
        sections=(
            Section(
                label=_("تقييم المخزون"),
                url_name="inventory:report_valuation",
                available=True,
                active_prefixes=("inventory:report_valuation",),
            ),
            Section(
                label=_("بطاقة وحركة الصنف"),
                url_name="inventory:report_stock_card",
                available=True,
                active_prefixes=("inventory:report_stock_card",),
            ),
            Section(
                label=_("فروقات الجرد"),
                url_name="inventory:report_count_variance",
                available=True,
                active_prefixes=("inventory:report_count_variance",),
            ),
            Section(
                label=_("فروقات أسعار الشراء"),
                url_name="procurement:report_price_variance",
                available=True,
                active_prefixes=("procurement:report_price_variance",),
            ),
            Section(
                label=_("كلفة الوصفات"),
                url_name="kitchen:report_recipe_cost",
                available=True,
                active_prefixes=("kitchen:report_recipe_cost",),
            ),
            Section(
                label=_("النظري مقابل الفعلي"),
                url_name="kitchen:report_usage_variance",
                available=True,
                active_prefixes=("kitchen:report_usage_variance",),
            ),
            Section(
                label=_("الإنتاجية والفاقد"),
                url_name="kitchen:report_productivity",
                available=True,
                active_prefixes=("kitchen:report_productivity",),
            ),
            Section(
                label=_("المبيعات ومطابقة الكاشير"),
                url_name="sales:report_daily_reconciliation",
                available=True,
                active_prefixes=("sales:report_daily_reconciliation",),
            ),
            Section(
                label=_("دورات الرواتب"),
                url_name="hr:payroll_list",
                available=True,
                active_prefixes=("hr:payroll_",),
            ),
            Section(
                label=_("أعمار الموردين"),
                url_name="procurement:report_supplier_aging",
                available=True,
                active_prefixes=("procurement:report_supplier_aging",),
            ),
            Section(
                label=_("تجاوز الحد الأدنى للسداد"),
                url_name="procurement:report_settlement_breaches",
                available=True,
                active_prefixes=("procurement:report_settlement_breaches",),
            ),
            Section(
                label=_("دورات السداد"),
                url_name="procurement:report_payment_cycles",
                available=True,
                active_prefixes=("procurement:report_payment_cycles",),
            ),
            Section(
                label=_("الإنفاق الشرائي"),
                url_name="procurement:report_purchase_spend",
                available=True,
                active_prefixes=("procurement:report_purchase_spend",),
            ),
            Section(
                label=_("التقرير المالي الشامل"),
                url_name="accounting:income_statement",
                available=True,
                active_prefixes=("accounting:income_statement",),
            ),
            Section(
                label=_("ميزان المراجعة"),
                url_name="accounting:trial_balance",
                available=True,
                active_prefixes=("accounting:trial_balance",),
            ),
            Section(
                label=_("الميزانية العمومية"),
                url_name="accounting:balance_sheet",
                available=True,
                active_prefixes=("accounting:balance_sheet",),
            ),
            Section(
                label=_("إقفال الشهر"),
                url_name="accounting:period_list",
                available=True,
                active_prefixes=("accounting:period_",),
            ),
        ),
    ),
    Module(
        key="settings",
        label=_("الإعدادات"),
        icon_name="settings",
        phase=_("الأساس"),
        url_name="organizations:organization_list",
        available=True,
        sections=(
            Section(
                label=_("المؤسسات"),
                url_name="organizations:organization_list",
                available=True,
            ),
            Section(label=_("الفروع"), url_name="organizations:branch_list", available=True),
            Section(
                label=_("صلاحيات الفروع"),
                url_name="organizations:access_list",
                available=True,
            ),
            # The posts an organization defines, and what each may do (ADR-034).
            Section(
                label=_("الأدوار والصلاحيات"),
                url_name="organizations:role_list",
                available=True,
                active_prefixes=("organizations:role_",),
            ),
            Section(label=_("المستخدمون"), url_name="users:user_list", available=True),
            Section(label=_("وحدات القياس"), url_name="units:unit_list", available=True),
            Section(
                label=_("صندوق المهام"),
                url_name="core:task_inbox",
                available=True,
                active_prefixes=("core:task_",),
            ),
            Section(
                label=_("مراقبة الأتمتة"),
                url_name="core:automation_monitoring",
                available=True,
            ),
            Section(label=_("سجل التدقيق"), url_name="core:audit_list", available=True),
            # The periods screen exists and is reachable from Accounting; a
            # "coming soon" badge beside it in Settings told the owner the
            # opposite of what the system does. It links to the built screen.
            Section(
                label=_("الفترات المالية"),
                url_name="accounting:period_list",
                available=True,
                active_prefixes=("accounting:period_",),
            ),
            Section(label=_("تسلسل المستندات")),
        ),
    ),
)

MODULES_BY_KEY = {module.key: module for module in MODULES}

DEFAULT_MODULE_KEY = "home"
