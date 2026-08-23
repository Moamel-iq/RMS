"""
The permission catalogue: what each permission is called, and where it sits.

The owner configures a role by ticking permissions (ADR-034 §2). A codename
is the contract every service checks and never changes; what this file adds
is presentation — an Arabic label an owner can read in a checkbox, the sidebar
section the act belongs to, its kind, and whether it is sensitive enough to
deserve a second look. Nothing here decides what a permission *does*.

A permission absent from the catalogue is still a permission. It is listed
under its module with its Django name, so a newly added act is configurable
the day it is migrated and merely unlabelled until somebody names it here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from django.contrib.auth.models import Permission
from django.utils.translation import gettext_lazy as _

from apps.organizations.roles import CONFIGURABLE_APP_LABELS, configurable_permissions

#: Module labels as the rail shows them; kept here rather than imported from
#: the navigation so the catalogue has no dependency on screen structure.
MODULE_LABELS: dict[str, str] = {
    "inventory": "المخزون",
    "procurement": "المشتريات",
    "kitchen": "المطبخ والوصفات",
    "sales": "المبيعات",
    "accounting": "المحاسبة",
    "hr": "الموارد البشرية",
}

KIND_LABELS: dict[str, str] = {
    "view": "عرض",
    "create": "إنشاء",
    "edit": "تعديل",
    "post": "ترحيل",
    "approve": "اعتماد",
    "reverse": "عكس",
    "manage": "إدارة",
    "import": "استيراد",
    "export": "تصدير",
    "other": "أخرى",
}

#: The order sections are shown in when more than one exists in a module:
#: reading first, then the acts that change things, then the structural ones.
KIND_ORDER: tuple[str, ...] = tuple(KIND_LABELS)


@dataclass(frozen=True)
class Entry:
    code: str
    label_ar: str
    section_ar: str = ""
    kind: str = "other"
    sensitive: bool = False
    note_ar: str = ""

    @property
    def app_label(self) -> str:
        return self.code.partition(".")[0]


#: Filled from the six modules' own documentation; see `catalogue_data.py`.
def _entries() -> tuple[Entry, ...]:
    from apps.organizations.catalogue_data import CATALOGUE

    return CATALOGUE


@dataclass
class Row:
    code: str
    label: str
    kind: str
    kind_label: str
    sensitive: bool
    note: str
    checked: bool
    catalogued: bool


@dataclass
class SectionRows:
    label: str
    rows: list[Row] = field(default_factory=list)


@dataclass
class ModuleRows:
    app_label: str
    label: str
    sections: list[SectionRows] = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(len(section.rows) for section in self.sections)

    @property
    def checked(self) -> int:
        return sum(1 for section in self.sections for row in section.rows if row.checked)


def entries_by_code() -> dict[str, Entry]:
    return {entry.code: entry for entry in _entries()}


def matrix(
    selected: Iterable[str], permissions: Iterable[Permission] | None = None
) -> list[ModuleRows]:
    """
    Every configurable permission, grouped module → section → act, with the
    given codes ticked. The grouping follows the catalogue; a permission the
    catalogue does not know is still listed, under its module, by its Django
    name, because a role must be able to carry it the day it exists.
    """
    chosen = set(selected)
    known = entries_by_code()
    rows_by_module: dict[str, dict[str, list[Row]]] = {}
    for permission in permissions if permissions is not None else configurable_permissions():
        app_label = permission.content_type.app_label
        code = f"{app_label}.{permission.codename}"
        entry = known.get(code)
        row = Row(
            code=code,
            label=entry.label_ar if entry else str(permission.name),
            kind=entry.kind if entry else "other",
            kind_label=KIND_LABELS.get(entry.kind if entry else "other", KIND_LABELS["other"]),
            sensitive=entry.sensitive if entry else False,
            note=entry.note_ar if entry else "",
            checked=code in chosen,
            catalogued=entry is not None,
        )
        section = entry.section_ar if entry else str(_("غير مصنّفة"))
        rows_by_module.setdefault(app_label, {}).setdefault(section, []).append(row)

    modules: list[ModuleRows] = []
    for app_label in CONFIGURABLE_APP_LABELS:
        sections = rows_by_module.get(app_label)
        if not sections:
            continue
        module = ModuleRows(app_label=app_label, label=MODULE_LABELS.get(app_label, app_label))
        for section_label, rows in sections.items():
            rows.sort(key=lambda row: (KIND_ORDER.index(row.kind), row.label))
            module.sections.append(SectionRows(label=section_label, rows=rows))
        # Sections keep catalogue order (first appearance); the unlabelled
        # remainder goes last so a new permission is noticed, not buried.
        module.sections.sort(key=lambda section: section.label == str(_("غير مصنّفة")))
        modules.append(module)
    return modules


__all__ = ["Entry", "ModuleRows", "Row", "SectionRows", "entries_by_code", "matrix"]
