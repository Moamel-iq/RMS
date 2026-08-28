"""
What changed between two versions of one recipe.

The question a manager asks before signing: *"we changed it in v3 — what
exactly?"* Prose cannot answer that, which is one of the reasons the method is
structured rows rather than a paragraph (RCP-063). This module answers it for
every part of a version's structure at once.

Three rules, and each of them exists because the obvious implementation gets it
wrong:

* **Rows are matched on a business key, never on a primary key.** Two versions
  are separate row sets; `RecipeLine` 41 and `RecipeLine` 88 may be the same
  ingredient. Matching on `item.code`, `step.sequence` and `serving.code` says
  what a cook means by "the same line".
* **The order is explicit and stable.** Never queryset order and never the
  order the rows happened to be created in: a comparison whose row order moved
  between two loads is a comparison nobody trusts, and a reviewer scanning for
  the one changed number would have to read it twice.
* **Comparison mutates nothing and costs nothing.** No status moves, no cost is
  derived, no money appears. Task 3.3 owns costing, and a diff that quietly
  computed a price would be the first cost surface in the module — arriving
  through the back door.

**Task 3.2B added the components section.** It is keyed on the child *recipe*
code, and the child's **version number is one of the compared attributes** — so
a replacement parent that adopts a newer blend reads `CHANGED` on exactly the
line that changed, and a parent whose blend was superseded elsewhere reads
`UNCHANGED`, because its own exact `component_version` did not move. That
distinction is the whole point of RCP-072 and it has to be visible in the diff a
manager signs off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.kitchen.models import RecipeVersion

#: What happened to one business key between the two versions.
ADDED = "ADDED"
REMOVED = "REMOVED"
CHANGED = "CHANGED"
UNCHANGED = "UNCHANGED"


@dataclass(frozen=True)
class FieldDifference:
    """One attribute that differs, with both readings."""

    label: str
    left: Any
    right: Any


@dataclass(frozen=True)
class ComparisonRow:
    """One business key, and what became of it."""

    key: str
    label: str
    classification: str
    differences: tuple[FieldDifference, ...] = ()

    @property
    def is_change(self) -> bool:
        return self.classification != UNCHANGED


@dataclass(frozen=True)
class ComparisonSection:
    """One part of the structure — the header, the lines, the steps."""

    key: str
    label: str
    rows: tuple[ComparisonRow, ...] = field(default=())

    @property
    def changed_rows(self) -> tuple[ComparisonRow, ...]:
        return tuple(row for row in self.rows if row.is_change)

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_rows)


@dataclass(frozen=True)
class VersionComparison:
    """The whole diff, section by section, in a fixed order."""

    left: RecipeVersion
    right: RecipeVersion
    sections: tuple[ComparisonSection, ...]

    @property
    def has_changes(self) -> bool:
        return any(section.has_changes for section in self.sections)

    @property
    def change_count(self) -> int:
        return sum(len(section.changed_rows) for section in self.sections)


def compare_recipe_versions(*, left: RecipeVersion, right: RecipeVersion) -> VersionComparison:
    """
    Compare two versions of the **same** recipe, oldest as `left`.

    Comparing versions of two different recipes is refused rather than
    rendered: every row would be added or removed, which looks like a
    catastrophic edit and is really a mis-click.
    """
    if left.recipe_id != right.recipe_id:
        raise ValidationError(
            {
                "right": ValidationError(
                    _("لا يمكن مقارنة نسختين من وصفتين مختلفتين."),
                    code="recipe_version_comparison_across_recipes",
                )
            }
        )
    if left.pk == right.pk:
        raise ValidationError(
            {
                "right": ValidationError(
                    _("اختر نسخة أخرى للمقارنة."),
                    code="recipe_version_comparison_with_itself",
                )
            }
        )

    return VersionComparison(
        left=left,
        right=right,
        sections=(
            _header_section(left, right),
            _scope_section(left, right),
            _line_section(left, right),
            _component_section(left, right),
            _substitute_section(left, right),
            _step_section(left, right),
            _step_link_section(left, right),
            _serving_section(left, right),
        ),
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _header_section(left: RecipeVersion, right: RecipeVersion) -> ComparisonSection:
    """The version's own fields, one comparison row per attribute."""
    attributes: list[tuple[str, str, Any, Any]] = [
        ("batch_size", str(_("حجم الدفعة")), left.batch_size, right.batch_size),
        (
            "expected_output_quantity",
            str(_("الناتج المتوقع")),
            left.expected_output_quantity,
            right.expected_output_quantity,
        ),
        (
            "output_unit",
            str(_("وحدة الناتج")),
            left.output_unit.code,
            right.output_unit.code,
        ),
        (
            "preparation_loss",
            str(_("فاقد التحضير")),
            left.preparation_loss,
            right.preparation_loss,
        ),
        ("cooking_yield", str(_("إنتاجية الطبخ")), left.cooking_yield, right.cooking_yield),
        ("instructions", str(_("نظرة عامة")), left.instructions, right.instructions),
        ("notes", str(_("ملاحظات")), left.notes, right.notes),
        (
            "source",
            str(_("المصدر")),
            _provenance(left),
            _provenance(right),
        ),
    ]
    rows = tuple(
        ComparisonRow(
            key=key,
            label=label,
            classification=UNCHANGED if _same(before, after) else CHANGED,
            differences=(() if _same(before, after) else (FieldDifference(label, before, after),)),
        )
        for key, label, before, after in attributes
    )
    return ComparisonSection(key="header", label=str(_("بيانات النسخة")), rows=rows)


def _scope_section(left: RecipeVersion, right: RecipeVersion) -> ComparisonSection:
    """Which branches each version claims, and over which dates."""
    return _diff(
        key="scope",
        label=str(_("نطاق السريان")),
        left_rows={
            scope.branch.code: scope for scope in left.branch_scopes.select_related("branch")
        },
        right_rows={
            scope.branch.code: scope for scope in right.branch_scopes.select_related("branch")
        },
        attributes=(
            ("effective_from", str(_("من تاريخ"))),
            ("effective_to", str(_("إلى تاريخ"))),
            ("is_organization_wide", str(_("على مستوى المؤسسة"))),
        ),
    )


def _line_section(left: RecipeVersion, right: RecipeVersion) -> ComparisonSection:
    """The ingredient list, keyed by item code."""
    return _diff(
        key="lines",
        label=str(_("المكوّنات")),
        left_rows=_lines_by_item(left),
        right_rows=_lines_by_item(right),
        attributes=(
            ("entered_quantity", str(_("الكمية المُدخلة"))),
            ("entered_unit_code", str(_("وحدة الإدخال"))),
            ("package_code", str(_("العبوة"))),
            ("conversion_factor", str(_("معامل التحويل"))),
            ("conversion_version", str(_("إصدار التحويل"))),
            ("base_quantity", str(_("الكمية المعتمدة"))),
            ("measured_quantity", str(_("كمية القياس"))),
            ("loss_rate", str(_("نسبة الفاقد"))),
            ("cost_class", str(_("تصنيف الكلفة"))),
            ("measurement_basis", str(_("أساس القياس"))),
            ("preparation_stage", str(_("المرحلة"))),
            ("is_optional", str(_("اختياري"))),
            ("line_order", str(_("الترتيب"))),
            ("provenance", str(_("المصدر"))),
        ),
    )


def _component_section(left: RecipeVersion, right: RecipeVersion) -> ComparisonSection:
    """
    Nested sub-recipes, keyed by the child **recipe** code.

    Keyed by recipe rather than by child version, and the version number is a
    compared *attribute* instead. That is what makes the two cases a manager has
    to tell apart legible:

    * a parent that adopted a newer blend reads `CHANGED`, on the version-number
      row, which is exactly what happened;
    * keying by child version instead would show the same edit as one `REMOVED`
      and one `ADDED` — two lines for one decision, and neither of them saying
      "this is the same blend, one edition later".

    Never keyed by the component's primary key, like every other section here.
    """
    return _diff(
        key="components",
        label=str(_("الوصفات الفرعية")),
        left_rows=_components_by_child_recipe(left),
        right_rows=_components_by_child_recipe(right),
        attributes=(
            ("child_version", str(_("نسخة الوصفة الفرعية"))),
            ("multiplier", str(_("المعامل"))),
            ("line_order", str(_("الترتيب"))),
            ("note", str(_("ملاحظة"))),
            ("provenance", str(_("المصدر"))),
        ),
    )


def _substitute_section(left: RecipeVersion, right: RecipeVersion) -> ComparisonSection:
    """Ranked alternatives, keyed by the pair of item codes."""
    return _diff(
        key="substitutes",
        label=str(_("البدائل")),
        left_rows=_substitutes_by_pair(left),
        right_rows=_substitutes_by_pair(right),
        attributes=(
            ("priority", str(_("الأولوية"))),
            ("is_active", str(_("فعّال"))),
            ("reason", str(_("السبب"))),
        ),
    )


def _step_section(left: RecipeVersion, right: RecipeVersion) -> ComparisonSection:
    """The method, keyed by step sequence."""
    return _diff(
        key="steps",
        label=str(_("خطوات الطريقة")),
        left_rows={str(step.sequence): step for step in left.steps.all()},
        right_rows={str(step.sequence): step for step in right.steps.all()},
        attributes=(
            ("instruction_ar", str(_("الخطوة"))),
            ("stage", str(_("المرحلة"))),
            ("expected_duration", str(_("المدة"))),
            ("temperature_c", str(_("الحرارة"))),
            ("heat_instruction_ar", str(_("تعليمة الحرارة"))),
            ("checkpoint_ar", str(_("نقطة التحقق"))),
            ("is_critical", str(_("حرجة"))),
        ),
    )


def _step_link_section(left: RecipeVersion, right: RecipeVersion) -> ComparisonSection:
    """Which ingredient enters at which step, keyed by both."""
    return _diff(
        key="step_links",
        label=str(_("ارتباط المكوّنات بالخطوات")),
        left_rows=_links_by_step_and_item(left),
        right_rows=_links_by_step_and_item(right),
        attributes=(("share", str(_("الحصة"))),),
    )


def _serving_section(left: RecipeVersion, right: RecipeVersion) -> ComparisonSection:
    """Serving definitions, keyed by serving code."""
    return _diff(
        key="servings",
        label=str(_("تعريفات الحصص")),
        left_rows={
            serving.code: serving for serving in left.servings.select_related("serving_unit")
        },
        right_rows={
            serving.code: serving for serving in right.servings.select_related("serving_unit")
        },
        attributes=(
            ("name", str(_("الاسم"))),
            ("serving_quantity", str(_("كمية الحصة"))),
            ("base_quantity", str(_("بوحدة الناتج"))),
            ("factor_display", str(_("معامل الدفعة"))),
            ("is_primary", str(_("رئيسية"))),
            ("rounding_policy", str(_("سياسة التقريب"))),
            ("rounding_increment", str(_("وحدة التقريب"))),
            ("measurement_basis", str(_("أساس القياس"))),
            ("is_active", str(_("فعّال"))),
        ),
    )


# ---------------------------------------------------------------------------
# Business keys
# ---------------------------------------------------------------------------
#
# Each of these builds a lightweight object whose attribute names match the
# comparison's attribute list. Doing it here rather than reaching through
# `line.item.code` inside the differ keeps the differ ignorant of the models,
# which is what lets one differ serve seven sections.


class _Row:
    """A flat view of one row, addressed by the names the differ compares."""

    def __init__(self, label: str, **values: Any) -> None:
        self.label = label
        self._values = values

    def get(self, name: str) -> Any:
        return self._values.get(name)


def _lines_by_item(version: RecipeVersion) -> dict[str, _Row]:
    rows: dict[str, _Row] = {}
    for line in version.lines.select_related("item", "entered_unit", "package_unit").order_by(
        "line_order"
    ):
        rows[line.item.code] = _Row(
            label=f"{line.item.code} — {line.item.name}",
            entered_quantity=line.entered_quantity,
            entered_unit_code=line.entered_unit.code if line.entered_unit else None,
            package_code=line.package_unit.code if line.package_unit else None,
            conversion_factor=line.conversion_factor,
            conversion_version=line.conversion_version,
            base_quantity=line.base_quantity,
            measured_quantity=line.measured_quantity,
            loss_rate=line.loss_rate,
            cost_class=line.cost_class,
            measurement_basis=line.measurement_basis,
            preparation_stage=line.preparation_stage,
            is_optional=line.is_optional,
            line_order=line.line_order,
            provenance=_provenance(line),
        )
    return rows


def _components_by_child_recipe(version: RecipeVersion) -> dict[str, _Row]:
    rows: dict[str, _Row] = {}
    query = version.components.select_related("component_recipe", "component_version")
    for component in query.order_by("line_order"):
        child_recipe = component.component_recipe
        rows[child_recipe.code] = _Row(
            label=f"{child_recipe.code} — {child_recipe.name}",
            # The exact edition, compared as a value. `v1` -> `v2` on this row is
            # a replacement parent adopting a newer child; no row at all means
            # the child was superseded somewhere else and this parent, correctly,
            # did not notice.
            child_version=f"v{component.component_version.version_number}",
            multiplier=component.multiplier,
            line_order=component.line_order,
            note=component.note,
            provenance=_provenance(component),
        )
    return rows


def _substitutes_by_pair(version: RecipeVersion) -> dict[str, _Row]:
    rows: dict[str, _Row] = {}
    query = version.lines.select_related("item").prefetch_related("substitutes__substitute_item")
    for line in query.order_by("line_order"):
        for substitute in line.substitutes.all():
            key = f"{line.item.code}→{substitute.substitute_item.code}"
            rows[key] = _Row(
                label=key,
                priority=substitute.priority,
                is_active=substitute.is_active,
                reason=substitute.reason,
            )
    return rows


def _links_by_step_and_item(version: RecipeVersion) -> dict[str, _Row]:
    rows: dict[str, _Row] = {}
    query = version.steps.prefetch_related("ingredient_links__recipe_line__item")
    for step in query.order_by("sequence"):
        for link in step.ingredient_links.all():
            key = f"{step.sequence}:{link.recipe_line.item.code}"
            rows[key] = _Row(label=key, share=link.share)
    return rows


# ---------------------------------------------------------------------------
# The differ
# ---------------------------------------------------------------------------


def _diff(
    *,
    key: str,
    label: str,
    left_rows: dict[str, Any],
    right_rows: dict[str, Any],
    attributes: tuple[tuple[str, str], ...],
) -> ComparisonSection:
    """
    Classify every business key present on either side.

    Sorted by key, always — the union of two dictionaries has no order of its
    own, and `sorted` is the only order that produces the same screen twice.
    """
    rows: list[ComparisonRow] = []
    for business_key in sorted(set(left_rows) | set(right_rows)):
        before = left_rows.get(business_key)
        after = right_rows.get(business_key)
        if before is None:
            rows.append(
                ComparisonRow(
                    key=business_key,
                    label=_label_of(after, business_key),
                    classification=ADDED,
                )
            )
            continue
        if after is None:
            rows.append(
                ComparisonRow(
                    key=business_key,
                    label=_label_of(before, business_key),
                    classification=REMOVED,
                )
            )
            continue

        differences = tuple(
            FieldDifference(attribute_label, _value(before, attribute), _value(after, attribute))
            for attribute, attribute_label in attributes
            if not _same(_value(before, attribute), _value(after, attribute))
        )
        rows.append(
            ComparisonRow(
                key=business_key,
                label=_label_of(before, business_key),
                classification=CHANGED if differences else UNCHANGED,
                differences=differences,
            )
        )
    return ComparisonSection(key=key, label=label, rows=tuple(rows))


def _value(row: Any, attribute: str) -> Any:
    if isinstance(row, _Row):
        return row.get(attribute)
    return getattr(row, attribute, None)


def _label_of(row: Any, fallback: str) -> str:
    label = getattr(row, "label", None)
    if isinstance(label, str) and label:
        return label
    return fallback


def _same(left: Any, right: Any) -> bool:
    """
    Equality that treats two Decimals of different scale as one value.

    `Decimal("0.500")` and `Decimal("0.5")` are the same quantity and `==`
    already agrees. The explicit branch is here so the intent survives somebody
    later reaching for `str()` comparison, which would report a change every
    time a column's stored scale altered and none of the numbers did.
    """
    if isinstance(left, Decimal) and isinstance(right, Decimal):
        return left == right
    return bool(left == right)


def _provenance(row: Any) -> str:
    """`document · page · reference`, or an em dash for a hand-entered row."""
    document = getattr(row, "source_document", "")
    if not document:
        return "—"
    parts = [document, str(getattr(row, "source_page", "") or "")]
    reference = getattr(row, "source_reference", "")
    if reference:
        parts.append(reference)
    return " · ".join(part for part in parts if part)
