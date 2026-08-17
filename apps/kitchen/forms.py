"""
Forms for the kitchen screens.

Deliberately thin, and deliberately **not** the mutation path. A form here
collects input, narrows every selector to what the caller already reaches, and
renders errors in Arabic. The write itself is always a call to
`apps/kitchen/services.py`, which re-reads the authoritative row under a lock,
checks the invariants, saves, and records the audit event.

A bound `ModelForm` mutates `self.instance` during validation, so by the time
`form_valid` runs the in-memory object already holds the new values — an audit
`previous_state` taken from it would record before == after. These are plain
`Form`s taking primitives for exactly that reason.

Every queryset below is scoped by the acting user. A selector offering a
foreign organization's item would turn a dropdown into a way to write across a
tenancy boundary, with the service's refusal as the only thing in the way.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django import forms
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.core.quantity import FACTOR_PLACES
from apps.inventory.models import InventoryItem, PackageUnit, Warehouse
from apps.inventory.selectors import readable_warehouses, visible_items, visible_package_units
from apps.kitchen.lifecycle import applicable_branches
from apps.kitchen.models import (
    REQUIRED_REVIEW_TYPES,
    ApprovalEvidenceKind,
    MeasurementBasis,
    PreparationStage,
    Recipe,
    RecipeCategory,
    RecipeLineCostClass,
    RecipeLineSubstitute,
    RecipeReviewDecision,
    RecipeReviewType,
    RecipeType,
    RecipeVersion,
    RecipeVersionStatus,
    ServingRoundingPolicy,
)
from apps.kitchen.permissions import (
    ACTIVATE_RECIPE_VERSION,
    APPROVE_RECIPE_VERSION,
    CREATE_PRODUCTION_BATCH,
    MANAGE_RECIPE,
    REJECT_RECIPE_VERSION,
    REVIEW_RECIPE_VERSION,
    VIEW_PRODUCTION,
    VIEW_RECIPE,
    VIEW_RECIPE_COST,
)
from apps.kitchen.selectors import visible_categories, visible_lines
from apps.organizations.authorization import branches_with_permission, organizations_with_permission
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure
from apps.users.models import User


class ScopedForm(forms.Form):
    """
    A form that knows who is filling it in.

    The actor is required, not optional. A default of `None` would let a new
    screen forget to pass it and silently offer every organization in the
    database.
    """

    scope_permission: str = MANAGE_RECIPE

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        self.actor = actor
        super().__init__(*args, **kwargs)

    def organization_choices(self) -> QuerySet[Organization]:
        return organizations_with_permission(self.actor, self.scope_permission).order_by("code")

    def branch_choices(self) -> QuerySet[Branch]:
        return branches_with_permission(self.actor, self.scope_permission).order_by("code")


class SourceProvenanceMixin(forms.Form):
    """
    The provenance fields, on every form that captures transcribed data.

    Both halves or neither (RCP-119). Refused here as well as at the service
    and the database, because the message a user should read names the field
    rather than the constraint.
    """

    source_document = forms.CharField(
        label=_("المستند المصدر"),
        max_length=200,
        required=False,
        help_text=_("اسم المستند، لا مساره. مثال: كتاب وصفات المطبخ خان مندي"),
    )
    source_page = forms.IntegerField(label=_("رقم الصفحة"), min_value=1, required=False)
    source_reference = forms.CharField(label=_("مرجع داخل المستند"), max_length=120, required=False)
    source_note = forms.CharField(
        label=_("ملاحظة المصدر"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        document = (cleaned.get("source_document") or "").strip()
        page = cleaned.get("source_page")
        if bool(document) != (page is not None):
            raise forms.ValidationError(
                {"source_page": _("المصدر يحتاج اسم المستند ورقم الصفحة معاً، أو لا شيء منهما.")}
            )
        return cleaned


# ---------------------------------------------------------------------------
# Categories and recipes
# ---------------------------------------------------------------------------


class RecipeCategoryForm(ScopedForm, SourceProvenanceMixin):
    """Create or rename a way of grouping dishes."""

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(
        label=_("الرمز"),
        max_length=32,
        help_text=_("حروف إنجليزية كبيرة وأرقام. يُوحَّد تلقائياً ولا يمكن تغييره لاحقاً."),
    )
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنجليزية"), max_length=200, required=False)
    notes = forms.CharField(
        label=_("ملاحظات"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )
    is_active = forms.BooleanField(label=_("فعّالة"), required=False, initial=True)

    def __init__(
        self, *args: Any, actor: User, instance: RecipeCategory | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.instance = instance
        self.fields["organization"].queryset = self.organization_choices()  # type: ignore[attr-defined]
        if instance is not None:
            self.fields["organization"].disabled = True
            self.fields["code"].disabled = True


class RecipeForm(ScopedForm, SourceProvenanceMixin):
    """
    Create or correct a recipe.

    `recipe_type` is disabled when editing: a recipe whose version describes
    how to produce a stocked item cannot quietly become a plate assembled to
    order, because the lines and the output item would still be there meaning
    something else.
    """

    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label=_("المؤسسة"))
    code = forms.CharField(label=_("الرمز"), max_length=32)
    name_ar = forms.CharField(label=_("اسم الوصفة بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنجليزية"), max_length=200, required=False)
    recipe_type = forms.ChoiceField(label=_("نوع الوصفة"), choices=RecipeType.choices)
    category = forms.ModelChoiceField(
        queryset=RecipeCategory.objects.none(), label=_("المجموعة"), required=False
    )
    output_item = forms.ModelChoiceField(
        queryset=InventoryItem.objects.none(),
        label=_("الصنف الناتج"),
        required=False,
        help_text=_("مطلوب لوصفة الدفعة، وممنوع لوصفة الحصة."),
    )
    description_ar = forms.CharField(
        label=_("الوصف بالعربية"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )
    description_en = forms.CharField(
        label=_("الوصف بالإنجليزية"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )
    branches = forms.ModelMultipleChoiceField(
        queryset=Branch.objects.none(),
        label=_("الفروع"),
        required=False,
        help_text=_("اتركه فارغاً لتطبيق الوصفة على كل فروع المؤسسة."),
    )
    notes = forms.CharField(
        label=_("ملاحظات"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def __init__(
        self, *args: Any, actor: User, instance: Recipe | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.instance = instance

        organizations = self.organization_choices()
        self.fields["organization"].queryset = organizations  # type: ignore[attr-defined]
        self.fields["category"].queryset = visible_categories(actor).filter(is_active=True)  # type: ignore[attr-defined]
        self.fields["output_item"].queryset = visible_items(actor).filter(is_active=True)  # type: ignore[attr-defined]
        self.fields["branches"].queryset = self.branch_choices()  # type: ignore[attr-defined]

        if instance is not None:
            self.fields["organization"].disabled = True
            self.fields["code"].disabled = True
            self.fields["recipe_type"].disabled = True
            self.fields["category"].queryset = visible_categories(actor).filter(  # type: ignore[attr-defined]
                organization=instance.organization
            )
            self.fields["output_item"].queryset = visible_items(actor).filter(  # type: ignore[attr-defined]
                organization=instance.organization
            )
            self.fields["branches"].queryset = self.branch_choices().filter(  # type: ignore[attr-defined]
                organization=instance.organization
            )


# ---------------------------------------------------------------------------
# Draft versions
# ---------------------------------------------------------------------------


class RecipeVersionForm(ScopedForm, SourceProvenanceMixin):
    """
    Open or correct a draft version.

    `batch_size` and `expected_output_quantity` are the recipe's own scale —
    the pit takes forty chickens — and not a menu quantity. Dividing that
    output into something sellable is `RecipeServing`'s job.
    """

    batch_size = forms.DecimalField(label=_("حجم الدفعة"), min_value=0, decimal_places=6)
    expected_output_quantity = forms.DecimalField(
        label=_("الناتج المتوقع"), min_value=0, decimal_places=6
    )
    output_unit = forms.ModelChoiceField(
        queryset=UnitOfMeasure.objects.none(), label=_("وحدة الناتج")
    )
    preparation_loss = forms.DecimalField(
        label=_("فاقد التحضير"),
        required=False,
        min_value=0,
        decimal_places=6,
        help_text=_("نسبة بين صفر وواحد. للعرض فقط، لا تدخل في الكلفة."),
    )
    cooking_yield = forms.DecimalField(
        label=_("إنتاجية الطبخ"), required=False, min_value=0, decimal_places=6
    )
    instructions = forms.CharField(
        label=_("نظرة عامة على الطريقة"),
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text=_("فقرة تمهيدية. الطريقة التشغيلية تُسجَّل كخطوات مرقّمة."),
    )
    notes = forms.CharField(
        label=_("ملاحظات"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["output_unit"].queryset = UnitOfMeasure.objects.filter(is_active=True)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Lines, substitutes, steps, servings
# ---------------------------------------------------------------------------


class RecipeLineForm(ScopedForm, SourceProvenanceMixin):
    """
    Add or correct an ingredient or packaging line.

    A quantity is entered in **either** a unit of measure or a package, never
    both. A variable-weight package has no arithmetic answer — one meat
    container is whatever it weighed — so the measured base quantity becomes
    required, exactly as it is when posting a variable-weight receipt.
    """

    item = forms.ModelChoiceField(queryset=InventoryItem.objects.none(), label=_("الصنف"))
    entered_quantity = forms.DecimalField(label=_("الكمية"), min_value=0, decimal_places=6)
    entered_unit = forms.ModelChoiceField(
        queryset=UnitOfMeasure.objects.none(), label=_("وحدة القياس"), required=False
    )
    package_unit = forms.ModelChoiceField(
        queryset=PackageUnit.objects.none(), label=_("العبوة"), required=False
    )
    measured_base_quantity = forms.DecimalField(
        label=_("الكمية الأساس المقاسة"),
        required=False,
        min_value=0,
        decimal_places=6,
        help_text=_("مطلوبة للعبوات متغيرة الوزن."),
    )
    measured_quantity = forms.DecimalField(
        label=_("كمية القياس"),
        required=False,
        min_value=0,
        decimal_places=6,
        help_text=_("ما ظهر على الميزان، قبل الاعتماد."),
    )
    loss_rate = forms.DecimalField(
        label=_("نسبة الفاقد"),
        required=False,
        min_value=0,
        max_value=1,
        decimal_places=6,
        help_text=_("نسبة بين صفر وواحد: تنظيف، عظم، تبخر، تقطيع."),
    )
    cost_class = forms.ChoiceField(
        label=_("تصنيف الكلفة"),
        choices=RecipeLineCostClass.choices,
        initial=RecipeLineCostClass.FOOD,
    )
    preparation_stage = forms.ChoiceField(
        label=_("المرحلة"),
        choices=[("", "—"), *PreparationStage.choices],
        required=False,
    )
    measurement_basis = forms.ChoiceField(
        label=_("أساس القياس"),
        choices=MeasurementBasis.choices,
        initial=MeasurementBasis.RAW,
        help_text=_("خام أم مطبوخ: الكميات لا تُجمع عبر أُسس مختلفة."),
    )
    is_optional = forms.BooleanField(label=_("اختياري"), required=False)
    note = forms.CharField(
        label=_("ملاحظة"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def __init__(self, *args: Any, actor: User, organization: Organization, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["item"].queryset = visible_items(actor).filter(  # type: ignore[attr-defined]
            organization=organization, is_active=True
        )
        self.fields["entered_unit"].queryset = UnitOfMeasure.objects.filter(is_active=True)  # type: ignore[attr-defined]
        self.fields["package_unit"].queryset = visible_package_units(actor).filter(is_active=True)  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        unit = cleaned.get("entered_unit")
        package = cleaned.get("package_unit")
        if bool(unit) == bool(package):
            raise forms.ValidationError(
                {"entered_unit": _("أدخل الكمية بوحدة قياس أو بعبوة، وليس بالاثنين معاً.")}
            )
        return cleaned


class RecipeLineSubstituteForm(ScopedForm, SourceProvenanceMixin):
    """Record an acceptable stand-in. Guidance only — nothing substitutes on its own."""

    substitute_item = forms.ModelChoiceField(
        queryset=InventoryItem.objects.none(), label=_("الصنف البديل")
    )
    priority = forms.IntegerField(
        label=_("الأولوية"),
        min_value=1,
        required=False,
        help_text=_("اتركه فارغاً ليأخذ البديل الترتيب التالي على هذا السطر."),
    )
    reason = forms.CharField(label=_("السبب"), max_length=200, required=False)
    note = forms.CharField(
        label=_("ملاحظة"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )
    is_active = forms.BooleanField(label=_("فعّال"), required=False, initial=True)

    def __init__(self, *args: Any, actor: User, organization: Organization, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["substitute_item"].queryset = visible_items(actor).filter(  # type: ignore[attr-defined]
            organization=organization, is_active=True
        )


class RecipeStepForm(ScopedForm, SourceProvenanceMixin):
    """
    Add or correct one numbered step.

    `temperature_c` stays empty unless a source gives a **number**. The recipe
    book gives نار هادئة, جمر, قدر الضغط and تنور; those go in the heat
    instruction, and the Celsius box stays blank rather than acquiring an
    invented value (RCP-068).
    """

    sequence = forms.IntegerField(label=_("التسلسل"), min_value=1, required=False)
    instruction_ar = forms.CharField(
        label=_("نص الخطوة بالعربية"), widget=forms.Textarea(attrs={"rows": 3})
    )
    instruction_en = forms.CharField(
        label=_("النص بالإنجليزية"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )
    stage = forms.ChoiceField(
        label=_("المرحلة"), choices=[("", "—"), *PreparationStage.choices], required=False
    )
    expected_minutes = forms.IntegerField(
        label=_("المدة بالدقائق"),
        min_value=1,
        required=False,
        help_text=_("تُملأ فقط عندما يذكر المصدر مدة."),
    )
    temperature_c = forms.DecimalField(
        label=_("الحرارة °م"),
        required=False,
        decimal_places=6,
        help_text=_("تُملأ فقط عندما يذكر المصدر رقماً. تعليمات النار تُكتب أدناه."),
    )
    heat_instruction_ar = forms.CharField(
        label=_("تعليمات النار"),
        max_length=200,
        required=False,
        help_text=_("مثال: نار هادئة · جمر · قدر الضغط · تنور"),
    )
    checkpoint_ar = forms.CharField(
        label=_("نقطة التحقق"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )
    is_critical = forms.BooleanField(label=_("نقطة حرجة"), required=False)
    media_reference = forms.CharField(label=_("مرجع مرئي"), max_length=200, required=False)
    note = forms.CharField(
        label=_("ملاحظة"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    @property
    def expected_duration(self) -> datetime.timedelta | None:
        minutes = self.cleaned_data.get("expected_minutes")
        return datetime.timedelta(minutes=minutes) if minutes else None


class StepIngredientForm(ScopedForm):
    """Link a line to a step. Documentation of *when*, never of *how much exists*."""

    recipe_line = forms.ModelChoiceField(queryset=Recipe.objects.none(), label=_("المكوّن"))
    share = forms.DecimalField(
        label=_("الحصة"),
        min_value=0,
        max_value=1,
        decimal_places=6,
        initial=1,
        help_text=_("جزء من كمية السطر يُضاف في هذه الخطوة. مجموع الحصص لا يتجاوز واحداً."),
    )
    note = forms.CharField(label=_("ملاحظة"), max_length=200, required=False)

    def __init__(self, *args: Any, actor: User, version_id: int, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["recipe_line"].queryset = visible_lines(actor).filter(version_id=version_id)  # type: ignore[attr-defined]


class RecipeServingForm(ScopedForm, SourceProvenanceMixin):
    """
    Define a way of dividing this version's output.

    Nothing here names a dish or a cut. A half is `0.5` of a `حبة` basis; a
    350 g portion is `0.350 KG` against a `KG` basis. The factor is derived by
    the service and never typed, because a factor that disagreed with its own
    quantity would misprice everything downstream.
    """

    code = forms.CharField(label=_("رمز الحصة"), max_length=32)
    name_ar = forms.CharField(label=_("الاسم بالعربية"), max_length=200)
    name_en = forms.CharField(label=_("الاسم بالإنجليزية"), max_length=200, required=False)
    serving_quantity = forms.DecimalField(label=_("كمية الحصة"), min_value=0, decimal_places=6)
    serving_unit = forms.ModelChoiceField(
        queryset=UnitOfMeasure.objects.none(), label=_("وحدة الحصة")
    )
    is_primary = forms.BooleanField(label=_("الحصة الأساسية"), required=False)
    rounding_increment = forms.DecimalField(
        label=_("وحدة التقريب"), required=False, min_value=0, decimal_places=3
    )
    rounding_policy = forms.ChoiceField(
        label=_("سياسة التقريب"),
        choices=ServingRoundingPolicy.choices,
        initial=ServingRoundingPolicy.NONE,
        help_text=_("تُطبَّق على عدد الحصص المخطط فقط، ولا تمس أي مبلغ."),
    )
    measurement_basis = forms.ChoiceField(
        label=_("أساس القياس"),
        choices=MeasurementBasis.choices,
        initial=MeasurementBasis.COOKED,
    )
    display_order = forms.IntegerField(label=_("ترتيب العرض"), min_value=1, required=False)
    is_active = forms.BooleanField(label=_("فعّالة"), required=False, initial=True)

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["serving_unit"].queryset = UnitOfMeasure.objects.filter(is_active=True)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------
#
# These four collect a decision, not a record. Each one is scoped by a
# different permission, because the whole point of the control is that four
# different people can hold four different halves of it.


class ReviewSignoffForm(ScopedForm):
    """
    One party's signature.

    The evidence fields are optional on the form and required by the service
    for the costing review, so the screen can offer one shape to three
    reviewers and the rule still lives in exactly one place.
    """

    scope_permission = REVIEW_RECIPE_VERSION

    review_type = forms.ChoiceField(
        label=_("نوع المراجعة"),
        choices=[(value, RecipeReviewType(value).label) for value in REQUIRED_REVIEW_TYPES],
    )
    decision = forms.ChoiceField(label=_("القرار"), choices=RecipeReviewDecision.choices)
    reason = forms.CharField(
        label=_("السبب"),
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
        help_text=_("مطلوب عند الرفض."),
    )
    evidence_reference = forms.CharField(
        label=_("مرجع الدليل"),
        max_length=120,
        required=False,
        help_text=_("مطلوب لمراجعة الكلفة: أي نموذج اعتماد اطّلع عليه المحاسب."),
    )
    evidence_kind = forms.ChoiceField(
        label=_("نوع الدليل"),
        choices=[("", "—"), *ApprovalEvidenceKind.choices],
        required=False,
    )
    note = forms.CharField(
        label=_("ملاحظة"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )


class ApproveVersionForm(ScopedForm):
    """
    The final signature, and the evidence it stands on.

    `approval_reference` is required here as well as at the service: an
    approval that cannot name what it approved against is a status column, and
    the screen should say so before the round trip.
    """

    scope_permission = APPROVE_RECIPE_VERSION

    approval_reference = forms.CharField(
        label=_("مرجع الاعتماد"),
        max_length=120,
        help_text=_("رقم نموذج اعتماد مكونات وكلفة الأصناف الموقّع."),
    )
    approval_evidence_kind = forms.ChoiceField(
        label=_("نوع الدليل"),
        choices=ApprovalEvidenceKind.choices,
        initial=ApprovalEvidenceKind.SIGNED_FORM,
    )
    note = forms.CharField(
        label=_("ملاحظة"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )


class RejectVersionForm(ScopedForm):
    """A refusal, which is only a record if it carries its reason."""

    scope_permission = REJECT_RECIPE_VERSION

    reason = forms.CharField(
        label=_("سبب الرفض"),
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("يبقى مع النسخة. التصحيح يكون بنسخة جديدة، لا بتعديل هذه."),
    )


class ActivateVersionForm(ScopedForm):
    """
    The claim on a date range and a set of branches.

    Leaving `branches` empty means organization-wide, which activation
    materialises into one scope row per applicable branch — the screen says so
    in the help text, because "empty means everywhere" is exactly the
    convention the *data model* refuses and the *form* still needs.
    """

    scope_permission = ACTIVATE_RECIPE_VERSION

    effective_from = forms.DateField(
        label=_("يسري من تاريخ"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("اليوم الأول الذي تحكم فيه هذه النسخة، ضمناً."),
    )
    effective_to = forms.DateField(
        label=_("حتى تاريخ"),
        widget=forms.DateInput(attrs={"type": "date"}),
        required=False,
        help_text=_("اليوم الأخير، ضمناً. اتركه فارغاً لنطاق مفتوح."),
    )
    branches = forms.ModelMultipleChoiceField(
        queryset=Branch.objects.none(),
        label=_("الفروع"),
        required=False,
        help_text=_("اتركه فارغاً لتفعيلها على كل فرع تنطبق عليه الوصفة."),
    )
    supersedes = forms.ModelChoiceField(
        queryset=RecipeVersion.objects.none(),
        label=_("تستبدل النسخة"),
        required=False,
        help_text=_("تُغلق النسخة السابقة في اليوم السابق لتاريخ السريان، في المعاملة نفسها."),
    )
    reason = forms.CharField(
        label=_("ملاحظة"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def __init__(self, *args: Any, actor: User, recipe: Recipe, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["branches"].queryset = applicable_branches(recipe)  # type: ignore[attr-defined]
        self.fields["supersedes"].queryset = recipe.versions.filter(  # type: ignore[attr-defined]
            status=RecipeVersionStatus.ACTIVE
        ).order_by("-version_number")


class SupersedeVersionForm(ScopedForm):
    """Close an active version because a named later one takes over."""

    scope_permission = ACTIVATE_RECIPE_VERSION

    replacement = forms.ModelChoiceField(
        queryset=RecipeVersion.objects.none(), label=_("النسخة البديلة")
    )
    reason = forms.CharField(
        label=_("ملاحظة"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def __init__(self, *args: Any, actor: User, version: RecipeVersion, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["replacement"].queryset = (  # type: ignore[attr-defined]
            version.recipe.versions.filter(
                status__in=[RecipeVersionStatus.APPROVED, RecipeVersionStatus.ACTIVE]
            )
            .exclude(pk=version.pk)
            .order_by("-version_number")
        )


class ResolverPreviewForm(ScopedForm):
    """
    "Which version governs this branch on this date?", asked from a screen.

    Read-only, and the date has no default for the same reason the resolver's
    argument has none: a preview that quietly meant *today* would teach the
    operator that the question does not need a date.
    """

    scope_permission = VIEW_RECIPE

    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    on_date = forms.DateField(
        label=_("بتاريخ"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("تاريخ العمل المطلوب، لا تاريخ اليوم."),
    )

    def __init__(self, *args: Any, actor: User, recipe: Recipe, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["branch"].queryset = Branch.objects.filter(  # type: ignore[attr-defined]
            organization_id=recipe.organization_id, is_active=True
        ).order_by("code")


# ---------------------------------------------------------------------------
# Nested components
# ---------------------------------------------------------------------------


class RecipeComponentForm(ScopedForm, SourceProvenanceMixin):
    """
    Add or correct one non-stocked sub-recipe on a draft.

    The candidate queryset is narrowed to what `component_candidates` offers —
    same organization, not this recipe, not a stocked recipe, frozen and
    approved, not already named. **That narrowing is a courtesy, not the
    control**: the service re-checks every one of those rules under the graph
    lock, and a hand-made POST naming any other version is refused there.

    Cycles deeper than one hop are deliberately not filtered out of the list.
    Deciding them needs the whole graph walked per candidate, and a recipe that
    silently vanished from a dropdown teaches nobody anything — the refusal
    names the path instead.
    """

    scope_permission = MANAGE_RECIPE

    component_version = forms.ModelChoiceField(
        queryset=RecipeVersion.objects.none(),
        label=_("الوصفة الفرعية"),
        help_text=_("نسخة معتمدة بعينها. لا تتغير تلقائياً عند صدور نسخة أحدث."),
    )
    multiplier = forms.DecimalField(
        label=_("المعامل"),
        min_value=Decimal("0.000000000001"),
        decimal_places=FACTOR_PLACES,
        max_digits=FACTOR_PLACES + 12,
        help_text=_("كم دفعة من الوصفة الفرعية تدخل في دفعة واحدة من هذه الوصفة."),
    )
    note = forms.CharField(
        label=_("ملاحظة"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def __init__(
        self,
        *args: Any,
        actor: User,
        version: RecipeVersion,
        component: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        from apps.kitchen.selectors import component_candidates

        candidates = component_candidates(actor, version)
        if component is not None:
            # An edit keeps its own current child in the list, which the
            # candidate filter excludes because the version already names it.
            candidates = candidates | RecipeVersion.objects.filter(
                pk=component.component_version_id
            )
        self.fields["component_version"].queryset = candidates.distinct()  # type: ignore[attr-defined]


class RecipeComponentReorderForm(ScopedForm):
    """Move one component to a position; the siblings renumber around it."""

    scope_permission = MANAGE_RECIPE

    line_order = forms.IntegerField(
        label=_("الترتيب"),
        min_value=1,
        help_text=_("الموضع المطلوب. تُعاد ترقيم البقية بلا فجوات."),
    )


# ---------------------------------------------------------------------------
# Task 3.3 - costing
# ---------------------------------------------------------------------------


class CostCardForm(ScopedForm):
    """
    Which warehouse, and as of when.

    **Neither field has an initial value**, and that is the design rather than
    an oversight. A date pre-filled with today teaches the operator that the
    question does not need a date, and the first time somebody re-runs a July
    card in September they get September's stock against July's recipe and no
    warning. A warehouse pre-filled with whichever sorts first prices a Baghdad
    recipe off a store it never draws on.

    The warehouse list is narrowed to the recipe's own organization **and** to
    what this caller reaches. That narrowing is a courtesy: the view resolves
    the submitted id through `resolve_manageable_warehouse` and the costing
    service refuses a foreign organization outright, so a hand-made POST naming
    another warehouse is refused twice more.
    """

    scope_permission = VIEW_RECIPE_COST

    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(),
        label=_("المخزن"),
        help_text=_("الكلفة تُقرأ من متوسط هذا المخزن تحديداً، لا من متوسط المؤسسة."),
    )
    as_of_date = forms.DateField(
        label=_("بتاريخ"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("تاريخ التقييم المطلوب، لا تاريخ اليوم."),
    )

    def __init__(self, *args: Any, actor: User, recipe: Recipe, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["warehouse"].queryset = (  # type: ignore[attr-defined]
            readable_warehouses(actor)
            .filter(branch__organization_id=recipe.organization_id)
            .order_by("branch__code", "code")
        )


class HistoricalCostForm(CostCardForm):
    """
    The same two questions, plus the branch whose version governed that day.

    The branch is required because `resolve_recipe_version` is a **per-branch**
    question: two branches may legitimately run different versions of one dish
    on one date, and a resolver that guessed would answer confidently for the
    wrong kitchen.
    """

    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))

    field_order = ["branch", "warehouse", "as_of_date"]

    def __init__(self, *args: Any, actor: User, recipe: Recipe, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, recipe=recipe, **kwargs)
        self.fields["branch"].queryset = Branch.objects.filter(  # type: ignore[attr-defined]
            organization_id=recipe.organization_id, is_active=True
        ).order_by("code")

    def clean(self) -> dict[str, Any]:
        super().clean()
        cleaned = self.cleaned_data
        branch = cleaned.get("branch")
        warehouse = cleaned.get("warehouse")
        # A warehouse in a different branch holds different stock. Refused here
        # so the message names the field, and again in `cost_recipe_on_date` so
        # a hand-made request is refused too.
        if branch is not None and warehouse is not None and warehouse.branch_id != branch.pk:
            self.add_error(
                "warehouse",
                forms.ValidationError(
                    _("المخزن لا يتبع هذا الفرع."), code="recipe_cost_wrong_warehouse"
                ),
            )
        return cleaned


class CostSnapshotForm(CostCardForm):
    """
    Freeze this card. A command form, and the only write Task 3.3 offers.

    `idempotency_key` is required and not generated here. A key the server
    invented would make every double-click a second decision, which is exactly
    what the key exists to prevent; the caller owns it because the caller is the
    one who knows whether this is a retry.
    """

    idempotency_key = forms.CharField(
        label=_("مفتاح التكرار"),
        max_length=128,
        help_text=_("نفس المفتاح لنفس الطلب يعيد اللقطة الأصلية ولا ينشئ ثانية."),
    )
    reference = forms.CharField(label=_("المرجع"), max_length=120, required=False)
    reason = forms.CharField(
        label=_("السبب"),
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
    )
    note = forms.CharField(
        label=_("ملاحظة"),
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
    )

    field_order = ["warehouse", "as_of_date", "idempotency_key", "reference", "reason", "note"]


class CostSnapshotFilterForm(ScopedForm):
    """Narrowing an already-scoped snapshot list. No field here can widen it."""

    scope_permission = VIEW_RECIPE_COST

    recipe = forms.ModelChoiceField(
        queryset=Recipe.objects.none(), label=_("الوصفة"), required=False
    )
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(), label=_("المخزن"), required=False
    )
    as_of_date = forms.DateField(
        label=_("بتاريخ"),
        widget=forms.DateInput(attrs={"type": "date"}),
        required=False,
    )

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        organization_ids = list(
            organizations_with_permission(actor, VIEW_RECIPE_COST).values_list("pk", flat=True)
        )
        self.fields["recipe"].queryset = Recipe.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids
        ).order_by("code")
        self.fields["warehouse"].queryset = (  # type: ignore[attr-defined]
            readable_warehouses(actor)
            .filter(branch__organization_id__in=organization_ids)
            .order_by("branch__code", "code")
        )


# ---------------------------------------------------------------------------
# Task 3.4 - production drafting
# ---------------------------------------------------------------------------
#
# Production is scoped to a **warehouse**, so every selector below is narrowed by
# `readable_production_warehouses` or `draftable_production_warehouses` rather
# than by the organization. Somebody who reads every recipe card sees an empty
# warehouse list here unless they hold production authority at a store.
#
# **No form here carries money.** There is no cost field, no valuation, no
# selling price and no margin, because a production draft has none of those
# things until Task 3.5 values what was consumed.


class ProductionBatchCreateForm(ScopedForm):
    """
    Which recipe, at which branch and warehouse, for which business date.

    `planned_business_date` has **no initial value**, deliberately. A field
    pre-filled with today teaches the operator that the question does not need a
    date, and a batch drafted on Monday for Sunday's production must use Sunday's
    recipe — the resolver answers per branch per date, and defaulting would answer
    confidently for the wrong day.

    The recipe list is narrowed to batch recipes with an output item, because
    producing a portion recipe would create stock of an item that deliberately
    does not exist (RCP-032). That narrowing is a courtesy: the service refuses
    the same shape by name.
    """

    scope_permission = CREATE_PRODUCTION_BATCH

    recipe = forms.ModelChoiceField(queryset=Recipe.objects.none(), label=_("الوصفة"))
    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(),
        label=_("المخزن"),
        help_text=_("المواد تُصرف من هذا المخزن والناتج يدخله — مخزن واحد للدفعة."),
    )
    planned_business_date = forms.DateField(
        label=_("تاريخ الإنتاج"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("النسخة السارية تُحدَّد بهذا التاريخ وبهذا الفرع، لا بتاريخ اليوم."),
    )
    multiplier = forms.DecimalField(
        label=_("المعامل"),
        min_value=Decimal("0.000001"),
        decimal_places=6,
        help_text=_("كم مرة تُنفَّذ الوصفة. يقبل الكسور — نصف قِدر شيء حقيقي."),
    )
    idempotency_key = forms.CharField(
        label=_("مفتاح التكرار"),
        max_length=128,
        help_text=_("نفس المفتاح لنفس الطلب يعيد الدفعة الأصلية ولا يُنشئ ثانية."),
    )
    notes = forms.CharField(
        label=_("ملاحظات"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    field_order = [
        "recipe",
        "branch",
        "warehouse",
        "planned_business_date",
        "multiplier",
        "idempotency_key",
        "notes",
    ]

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        from apps.kitchen.selectors import draftable_production_warehouses

        warehouses = draftable_production_warehouses(actor).select_related(
            "branch", "branch__organization"
        )
        self.fields["warehouse"].queryset = warehouses.order_by(  # type: ignore[attr-defined]
            "branch__code", "code"
        )
        organization_ids = list(
            warehouses.values_list("branch__organization_id", flat=True).distinct()
        )
        branch_ids = list(warehouses.values_list("branch_id", flat=True).distinct())
        self.fields["branch"].queryset = Branch.objects.filter(  # type: ignore[attr-defined]
            pk__in=branch_ids
        ).order_by("code")
        self.fields["recipe"].queryset = (  # type: ignore[attr-defined]
            Recipe.objects.filter(
                organization_id__in=organization_ids,
                is_active=True,
                output_item__isnull=False,
            )
            .select_related("output_item")
            .order_by("code")
        )

    def clean(self) -> dict[str, Any]:
        super().clean()
        cleaned = self.cleaned_data
        branch = cleaned.get("branch")
        warehouse = cleaned.get("warehouse")
        recipe = cleaned.get("recipe")
        # Each of these is refused again by `_validate_shape` and once more by a
        # trigger. Named here so the operator reads which field is wrong rather
        # than a constraint name.
        if branch is not None and warehouse is not None and warehouse.branch_id != branch.pk:
            self.add_error(
                "warehouse",
                forms.ValidationError(
                    _("المخزن لا يتبع هذا الفرع."), code="production_batch_wrong_warehouse"
                ),
            )
        if (
            recipe is not None
            and branch is not None
            and branch.organization_id != (recipe.organization_id)
        ):
            self.add_error(
                "branch",
                forms.ValidationError(
                    _("الفرع يتبع مؤسسة أخرى."), code="production_batch_foreign_branch"
                ),
            )
        return cleaned


class ProductionPreviewForm(ScopedForm):
    """
    The same question as creation, without the commitment.

    Deliberately shares the create form's fields minus the idempotency key and
    the notes: a preview that asked a *different* question would be previewing
    something other than the batch it claims to.
    """

    scope_permission = VIEW_PRODUCTION

    recipe = forms.ModelChoiceField(queryset=Recipe.objects.none(), label=_("الوصفة"))
    branch = forms.ModelChoiceField(queryset=Branch.objects.none(), label=_("الفرع"))
    planned_business_date = forms.DateField(
        label=_("تاريخ الإنتاج"), widget=forms.DateInput(attrs={"type": "date"})
    )
    multiplier = forms.DecimalField(
        label=_("المعامل"), min_value=Decimal("0.000001"), decimal_places=6
    )

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        from apps.kitchen.selectors import readable_production_warehouses

        warehouses = readable_production_warehouses(actor).select_related("branch")
        organization_ids = list(
            warehouses.values_list("branch__organization_id", flat=True).distinct()
        )
        self.fields["branch"].queryset = Branch.objects.filter(  # type: ignore[attr-defined]
            pk__in=list(warehouses.values_list("branch_id", flat=True).distinct())
        ).order_by("code")
        self.fields["recipe"].queryset = Recipe.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=organization_ids, is_active=True, output_item__isnull=False
        ).order_by("code")


class ProductionBatchFilterForm(ScopedForm):
    """Narrowing an already-scoped batch list. No field here can widen it."""

    scope_permission = VIEW_PRODUCTION

    recipe = forms.ModelChoiceField(
        queryset=Recipe.objects.none(), label=_("الوصفة"), required=False
    )
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(), label=_("المخزن"), required=False
    )
    planned_business_date = forms.DateField(
        label=_("تاريخ الإنتاج"),
        widget=forms.DateInput(attrs={"type": "date"}),
        required=False,
    )

    def __init__(self, *args: Any, actor: User, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        from apps.kitchen.selectors import readable_production_warehouses

        warehouses = readable_production_warehouses(actor).select_related("branch")
        self.fields["warehouse"].queryset = warehouses.order_by(  # type: ignore[attr-defined]
            "branch__code", "code"
        )
        self.fields["recipe"].queryset = Recipe.objects.filter(  # type: ignore[attr-defined]
            organization_id__in=list(
                warehouses.values_list("branch__organization_id", flat=True).distinct()
            )
        ).order_by("code")


class ActualEntryMixin(forms.Form):
    """
    The two ways to say how much was consumed, and the rule that it is one.

    Either a unit of measure or a package, never both and never neither — the
    same exclusivity `RecipeLine` uses, refused here so the message names a field
    and refused again by `_actual_basis` and by a check constraint.

    A **VARIABLE** package needs a measured base quantity because one meat
    container is whatever it weighed; there is no arithmetic answer, and
    inventing one would put a weight in the database that no scale produced.
    """

    entered_quantity = forms.DecimalField(
        label=_("الكمية"),
        min_value=Decimal("0"),
        decimal_places=6,
        help_text=_("أكثر من المخطط أو أقل أو صفر — الانحراف حقيقة تُسجَّل ولا تُرفض."),
    )
    entered_unit = forms.ModelChoiceField(
        queryset=UnitOfMeasure.objects.none(), label=_("الوحدة"), required=False
    )
    package_unit = forms.ModelChoiceField(
        queryset=PackageUnit.objects.none(), label=_("العبوة"), required=False
    )
    measured_base_quantity = forms.DecimalField(
        label=_("الكمية الأساس المقاسة"),
        min_value=Decimal("0"),
        decimal_places=6,
        required=False,
        help_text=_("للعبوات متغيرة الوزن فقط — ما قاله الميزان."),
    )

    def _narrow_entry_fields(self, actor: User, item: InventoryItem) -> None:
        self.fields["entered_unit"].queryset = UnitOfMeasure.objects.filter(  # type: ignore[attr-defined]
            dimension=item.base_unit.dimension, is_active=True
        ).order_by("code")
        self.fields["package_unit"].queryset = (  # type: ignore[attr-defined]
            visible_package_units(actor)
            .filter(organization_id=item.organization_id)
            .order_by("code")
        )

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        unit = cleaned.get("entered_unit")
        package = cleaned.get("package_unit")
        if (unit is None) == (package is None):
            self.add_error(
                "entered_unit",
                forms.ValidationError(
                    _("أدخل الكمية بوحدة قياس أو بعبوة، وليس بالاثنين معاً."),
                    code="production_actual_one_entry_mode",
                ),
            )
        return cleaned


class ProductionActualForm(ScopedForm, ActualEntryMixin):
    """What the kitchen actually consumed of this requirement's own item."""

    scope_permission = CREATE_PRODUCTION_BATCH

    note = forms.CharField(
        label=_("ملاحظة"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    field_order = [
        "entered_quantity",
        "entered_unit",
        "package_unit",
        "measured_base_quantity",
        "note",
    ]

    def __init__(self, *args: Any, actor: User, item: InventoryItem, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self._narrow_entry_fields(actor, item)


class ProductionSubstituteForm(ScopedForm, ActualEntryMixin):
    """
    Which approved stand-in, and how much of it.

    The choice list comes from `substitute_candidates`, which reads the
    requirement's **own source line**: a substitute approved for the rice line is
    not approved for the oil line even when both name rice. A filtered dropdown is
    a courtesy; `_approved_item` refuses the same thing by name, and a trigger
    refuses it again.
    """

    scope_permission = CREATE_PRODUCTION_BATCH

    substitute = forms.ModelChoiceField(
        queryset=RecipeLineSubstitute.objects.none(),
        label=_("البديل المعتمد"),
        help_text=_("البدائل المعتمدة لهذا السطر تحديداً، بترتيب الوصفة."),
    )
    reason = forms.CharField(label=_("سبب الاستبدال"), max_length=200, required=False)

    field_order = [
        "substitute",
        "entered_quantity",
        "entered_unit",
        "package_unit",
        "measured_base_quantity",
        "reason",
    ]

    def __init__(self, *args: Any, actor: User, line: Any, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        from apps.kitchen.selectors import substitute_candidates

        self.line = line
        self.fields["substitute"].queryset = substitute_candidates(line)  # type: ignore[attr-defined]
        # Units are narrowed once a substitute is chosen; before that the widest
        # honest list is every active unit of measure, because an approved
        # stand-in may legitimately live in another dimension entirely.
        self.fields["entered_unit"].queryset = UnitOfMeasure.objects.filter(  # type: ignore[attr-defined]
            is_active=True
        ).order_by("code")
        self.fields["package_unit"].queryset = visible_package_units(actor).order_by("code")  # type: ignore[attr-defined]


class ProductionRescaleForm(ScopedForm):
    """
    A new multiplier, and — when it would discard somebody's figures — a reason.

    `reset_actuals` is a deliberate checkbox rather than something the screen
    decides. Recomputing over an operator's measurements is the consequence of
    the command, so it is stated rather than discovered, and the reason is
    required when the box is ticked because discarding a measurement without a
    word is the kind of thing a system should make you say out loud.
    """

    scope_permission = CREATE_PRODUCTION_BATCH

    multiplier = forms.DecimalField(
        label=_("المعامل الجديد"), min_value=Decimal("0.000001"), decimal_places=6
    )
    reset_actuals = forms.BooleanField(
        label=_("إعادة ضبط الكميات الفعلية"),
        required=False,
        help_text=_("يحذف كل ما أُدخل من كميات وبدائل ويعيد الافتراضات من الخطة الجديدة."),
    )
    reason = forms.CharField(
        label=_("السبب"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        if cleaned.get("reset_actuals") and not (cleaned.get("reason") or "").strip():
            self.add_error(
                "reason",
                forms.ValidationError(
                    _("إعادة الضبط تتطلب سبباً."),
                    code="production_batch_reset_requires_reason",
                ),
            )
        return cleaned


class ProductionOutputForm(ScopedForm):
    """
    What actually came out. Entered by the operator, never derived (RCP-031).

    Deriving it from the inputs would assume a yield the kitchen did not measure,
    and the difference between expected and actual output is precisely what the
    yield report exists to show.
    """

    scope_permission = CREATE_PRODUCTION_BATCH

    entered_quantity = forms.DecimalField(
        label=_("الناتج الفعلي"), min_value=Decimal("0"), decimal_places=6
    )
    entered_unit = forms.ModelChoiceField(queryset=UnitOfMeasure.objects.none(), label=_("الوحدة"))

    def __init__(self, *args: Any, actor: User, output_item: InventoryItem, **kwargs: Any) -> None:
        super().__init__(*args, actor=actor, **kwargs)
        self.fields["entered_unit"].queryset = UnitOfMeasure.objects.filter(  # type: ignore[attr-defined]
            dimension=output_item.base_unit.dimension, is_active=True
        ).order_by("code")


class ProductionNotesForm(ScopedForm):
    """The operator's own note. Clearing it is a real edit and is permitted."""

    scope_permission = CREATE_PRODUCTION_BATCH

    notes = forms.CharField(
        label=_("ملاحظات"), widget=forms.Textarea(attrs={"rows": 3}), required=False
    )


class ProductionDiscardForm(ScopedForm):
    """
    Throwing a draft away, and the reason once anything has been entered.

    Required at the service too, and for the same reason it is asked here rather
    than assumed: the draft may hold measurements somebody took.
    """

    scope_permission = CREATE_PRODUCTION_BATCH

    reason = forms.CharField(
        label=_("سبب الحذف"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )
