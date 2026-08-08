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
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
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
        sections=_sections(
            _("الأصناف"),
            _("مجموعات الأصناف"),
            _("المخازن ومواقع المطبخ"),
            _("الأرصدة الافتتاحية"),
            _("الإدخال المخزني"),
            _("الصرف المخزني"),
            _("التحويلات"),
            _("المرتجعات"),
            _("الهالك والتلف"),
            _("الجرد"),
            _("التسويات"),
            _("حركة المخزون"),
            _("تقييم المخزون"),
            _("حدود إعادة الطلب"),
        ),
    ),
    Module(
        key="procurement",
        label=_("المشتريات"),
        icon_name="cart",
        phase=_("المرحلة ٢"),
        sections=_sections(
            _("الموردون"),
            _("أوامر الشراء"),
            _("استلام البضاعة"),
            _("فواتير الموردين"),
            _("التكاليف الإضافية"),
            _("مرتجعات الموردين"),
            _("دفعات الموردين"),
            _("تخصيص الدفعات"),
            _("أرصدة الموردين"),
            _("شروط الائتمان"),
        ),
    ),
    Module(
        key="kitchen",
        label=_("المطبخ والوصفات"),
        icon_name="chef",
        phase=_("المرحلة ٣"),
        sections=_sections(
            _("الوصفات"),
            _("نسخ الوصفات"),
            _("أوامر الإنتاج"),
            _("الإنتاجية والفاقد"),
            _("الصرف للمطبخ"),
            _("المرتجع من المطبخ"),
            _("الهالك"),
            _("وجبات الموظفين"),
            _("الوجبات المجانية"),
            _("الاستهلاك النظري"),
            _("الاستهلاك الفعلي"),
            _("انحراف الاستهلاك"),
            _("كلفة الطبق"),
        ),
    ),
    Module(
        key="sales",
        label=_("المبيعات"),
        icon_name="receipt",
        phase=_("المرحلة ٤"),
        sections=_sections(
            _("لوحة المبيعات"),
            _("المبيعات اليومية"),
            _("أصناف المنيو"),
            _("قنوات البيع"),
            _("تطبيقات التوصيل"),
            _("العمولات والاتفاقيات"),
            _("الخصومات"),
            _("المرتجعات والإلغاءات"),
            _("ذمم التطبيقات"),
            _("تسويات التطبيقات"),
            _("إقفال الكاشير"),
            _("المطابقة اليومية"),
        ),
    ),
    Module(
        key="accounting",
        label=_("المحاسبة"),
        icon_name="ledger",
        phase=_("المرحلة ٥"),
        sections=_sections(
            _("دليل الحسابات"),
            _("قيود اليومية"),
            _("الصناديق"),
            _("الحسابات البنكية"),
            _("ذمم الموردين"),
            _("ذمم التطبيقات"),
            _("المصروفات"),
            _("المستحقات والمقدمات"),
            _("الفترات المحاسبية"),
            _("ميزان المراجعة"),
            _("دفتر الأستاذ"),
            _("قائمة الدخل"),
            _("الميزانية العمومية"),
        ),
    ),
    Module(
        key="hr",
        label=_("الموارد البشرية"),
        icon_name="people",
        phase=_("المرحلة ٦"),
        sections=_sections(
            _("الموظفون"),
            _("العقود والأجور"),
            _("الورديات"),
            _("الحضور والانصراف"),
            _("الإجازات والغياب"),
            _("العمل الإضافي"),
            _("الاستقطاعات"),
            _("السلف"),
            _("احتساب الرواتب"),
            _("اعتماد الرواتب"),
            _("صرف الرواتب"),
            _("كشوف الموظفين"),
        ),
    ),
    Module(
        key="reports",
        label=_("التقارير"),
        icon_name="chart",
        phase=_("المرحلة ٧"),
        sections=_sections(
            _("حركة وتقييم المخزون"),
            _("الأصناف الراكدة والسريعة"),
            _("فروقات الجرد"),
            _("اتجاهات أسعار الشراء"),
            _("كلفة الوصفات"),
            _("النظري مقابل الفعلي"),
            _("ربحية الأصناف والقنوات"),
            _("أعمار تسويات التطبيقات"),
            _("المبيعات ومطابقة الكاشير"),
            _("ملخص الرواتب"),
            _("أعمار الموردين"),
            _("القوائم المالية"),
            _("مؤشرات الفروع"),
            _("قائمة إقفال الشهر"),
        ),
    ),
    Module(
        key="settings",
        label=_("الإعدادات"),
        icon_name="settings",
        phase=_("الأساس"),
        # Settings hands off to the Django admin until dedicated screens exist.
        # Those pages render outside the shell, which is a known rough edge.
        url_name="admin:index",
        available=True,
        sections=(
            Section(
                label=_("المؤسسات"),
                url_name="admin:organizations_organization_changelist",
                available=True,
            ),
            Section(
                label=_("الفروع"), url_name="admin:organizations_branch_changelist", available=True
            ),
            Section(
                label=_("صلاحيات الفروع"),
                url_name="admin:organizations_branchmembership_changelist",
                available=True,
            ),
            Section(label=_("المستخدمون"), url_name="admin:users_user_changelist", available=True),
            Section(label=_("وحدات القياس")),
            Section(label=_("الفترات المالية")),
            Section(label=_("تسلسل المستندات")),
        ),
    ),
)

MODULES_BY_KEY = {module.key: module for module in MODULES}

DEFAULT_MODULE_KEY = "home"
