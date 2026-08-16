"""
Recipes, their draft versions, and the structure hanging off a version.

Task 3.1 delivers **master data and draft structure only**. Every version this
module can create is `DRAFT`, and that is enforced at the database
(`recipe_version_task_3_1_draft_only`) rather than merely intended: Task 3.2
owns submission, approval, effective dating, activation and supersession, and
alters that constraint when it arrives.

Nothing here moves stock, touches a ledger, or knows a price. A recipe is an
intention; the production batch of Task 3.5 is the event (RCP-002).

See `docs/tasks/task-3-0-recipes-production-domain-spec.md` for the approved
design — §3 the recipe, §4 versions, §5 lines, §5A steps, §5C servings — and
`docs/invariants/kitchen-invariants.md` for the rules these must satisfy.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TimeStampedModel
from apps.core.quantity import CALCULATION_PLACES, FACTOR_PLACES, QUANTITY_PLACES

#: Recipe and category codes. The shape inventory items and suppliers use, and
#: canonicalised to uppercase before storage so uniqueness is case-insensitive
#: in effect without a functional index.
CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._-]*$"

#: A recipe quantity is an intermediate that later multiplies by a batch
#: multiplier and divides by a serving factor, so it persists at
#: `CALCULATION_PLACES` — the precision ADR-006 reserved for exactly this.
QUANTITY_MAX_DIGITS = CALCULATION_PLACES + 15
#: A serving factor is a technical identity at the same precision inventory
#: uses for package conversions.
FACTOR_MAX_DIGITS = FACTOR_PLACES + 12
#: Rounding increments are counted in sellable units, so they use the ordinary
#: stored-quantity precision rather than the calculation precision.
INCREMENT_MAX_DIGITS = QUANTITY_PLACES + 15


class RecipeType(models.TextChoices):
    """
    The charter's two recipe kinds, as a closed type rather than two models.

    `BATCH` produces a stored `InventoryItem` — a pot of mandi rice, a batch of
    sauce, a tray of bread. `PORTION` describes a plated dish assembled to
    order, whose output is deliberately **not** an `InventoryItem` (RCP-007).
    """

    BATCH = "BATCH", _("وصفة دفعة")
    PORTION = "PORTION", _("وصفة حصة")


class RecipeVersionStatus(models.TextChoices):
    """
    Release 1, Task 3.1 offers one status; the field marks the boundary.

    `CostingMethod` in `apps.inventory` uses the same shape for the same
    reason. Task 3.2 adds `APPROVED`, `SUPERSEDED` and `DISCARDED` together
    with the lifecycle services that reach them, and alters the check
    constraint that currently pins every row to `DRAFT`.
    """

    DRAFT = "DRAFT", _("مسودة")


class MeasurementBasis(models.TextChoices):
    """
    Where in the process a quantity was measured (RCP-120).

    The recipe book's 350 g is meat carved **after** cooking; the plate cards'
    500 g is a piece weighed **before** it. Without this field the two look
    like a contradiction about the same meat, and whoever imported last would
    silently win. No report may sum or compare quantities across bases.
    """

    RAW = "RAW", _("خام قبل الطبخ")
    PREPARED = "PREPARED", _("محضّر")
    COOKED = "COOKED", _("مطبوخ")
    PLATED = "PLATED", _("في الطبق")


class RecipeLineCostClass(models.TextChoices):
    """
    The reporting dimension `KM-RCP-004`'s cost summary needs (RCP-061).

    The workbook splits `كلفة الغذاء` from `كلفة التغليف` while carrying both
    in one ingredient list, so the split lives on the line. `ACCOMPANIMENT`
    separates the `لبن سطل` / `طرشي مشكل` rows, which are food but are not the
    dish. The class changes no posting, no account and no valuation.
    """

    FOOD = "FOOD", _("غذاء")
    PACKAGING = "PACKAGING", _("تغليف")
    ACCOMPANIMENT = "ACCOMPANIMENT", _("مرافقات")


class PreparationStage(models.TextChoices):
    """The stage of the method a line or step belongs to (spec §5A)."""

    PREP = "PREP", _("تحضير")
    MARINATE = "MARINATE", _("تتبيل")
    COOK = "COOK", _("طبخ")
    REST = "REST", _("راحة")
    PORTION = "PORTION", _("تقطيع وتقسيم")
    PACK = "PACK", _("تغليف وتقديم")


class ServingRoundingPolicy(models.TextChoices):
    """
    How a planned portion count is rounded (RCP-085).

    Governs **counts only**. Rounding 40.7 portions down to 40 is sensible;
    letting that rounding move money would make the sum of serving costs
    disagree with the batch, which RCP-087 forbids outright.
    """

    NONE = "NONE", _("بدون تقريب")
    DOWN = "DOWN", _("للأسفل")
    NEAREST = "NEAREST", _("لأقرب وحدة")


class SourceProvenance(models.Model):
    """
    Where a row came from, when it came from a document (RCP-119).

    Both `source_document` and `source_page` are set, or neither is. A
    half-filled provenance is worse than none: it looks like an answer to
    "who says so" and is not one.

    Deliberately **portable**. The document is named, not pathed: the recipe
    book lives outside the repository on one machine, and a developer's
    absolute Windows path is not a business fact. `source_sha256` identifies
    which revision was transcribed, so a later reader can tell whether the
    document has since changed.
    """

    source_document = models.CharField(_("source document"), max_length=200, blank=True)
    source_page = models.PositiveIntegerField(_("source page"), null=True, blank=True)
    source_sha256 = models.CharField(_("source SHA-256"), max_length=64, blank=True)
    #: A card number, sheet name or row reference inside the document.
    source_reference = models.CharField(_("source reference"), max_length=120, blank=True)
    source_note = models.TextField(_("source note"), blank=True)

    class Meta:
        abstract = True

    @property
    def has_source(self) -> bool:
        return bool(self.source_document)


def _provenance_constraint(name: str) -> models.CheckConstraint:
    """Both halves of a provenance, or neither (invariant 47)."""
    return models.CheckConstraint(
        condition=(
            Q(source_document="", source_page__isnull=True)
            | (~Q(source_document="") & Q(source_page__isnull=False))
        ),
        name=name,
    )


class RecipeCategory(TimeStampedModel, SourceProvenance):
    """
    How a kitchen groups its dishes — `دجاج`, `لحم`, `طبق مشترك` in the
    workbook's own register.

    Organization-owned and flat. A hierarchy was not specified, and a tree
    nobody asked for is a tree somebody has to maintain.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="recipe_categories",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    notes = models.TextField(_("notes"), blank=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe category")
        verbose_name_plural = _("recipe categories")
        ordering = ["organization__code", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="recipe_category_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="recipe_category_code_format"
            ),
            models.CheckConstraint(
                condition=~Q(name_ar=""), name="recipe_category_name_ar_not_empty"
            ),
            _provenance_constraint("recipe_category_provenance_is_complete"),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


class Recipe(TimeStampedModel, SourceProvenance):
    """
    A dish the kitchen knows how to make.

    Organization master data, shared across branches like an item or a
    supplier: the dish is one dish, and where it is cooked varies (RCP-006).
    Which branches may use it is `RecipeBranch`; **which version applies on a
    date is Task 3.2's**, and nothing here answers it.

    **Carries no cost field** (RCP-009). Every cost is derived from a version's
    lines against the ledger's moving averages, or read from a dated snapshot.
    A stored "current cost" is a second source of truth, and the one that
    drifts is always the stored one.

    **Carries no price, margin, commission or fee** (KD-13, RCP-089). Selling
    prices are Phase 4's, set by the business. A recipe knows its cost; it does
    not know what the dish sells for.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="recipes",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)
    description_ar = models.TextField(_("description (Arabic)"), blank=True)
    description_en = models.TextField(_("description (English)"), blank=True)

    recipe_type = models.CharField(_("type"), max_length=8, choices=RecipeType.choices)
    category = models.ForeignKey(
        RecipeCategory,
        on_delete=models.PROTECT,
        related_name="recipes",
        null=True,
        blank=True,
        verbose_name=_("category"),
    )

    #: Required for `BATCH`, forbidden for `PORTION` — enforced below and in
    #: the service. A batch recipe producing a `RAW_MATERIAL` is a data-entry
    #: error with accounting consequences (RCP-008).
    output_item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="produced_by_recipes",
        null=True,
        blank=True,
        verbose_name=_("output item"),
    )

    branches = models.ManyToManyField(
        "organizations.Branch",
        through="RecipeBranch",
        related_name="recipes",
        blank=True,
        verbose_name=_("branches"),
    )

    is_active = models.BooleanField(_("active"), default=True)
    notes = models.TextField(_("notes"), blank=True)

    #: The highest version number ever allocated for this recipe, including
    #: numbers whose drafts were later discarded. A monotonic allocator rather
    #: than `max(existing) + 1`, because a version number is a name people
    #: quote in conversation — "we changed it in v2" — and `v2` meaning two
    #: different things is worse than a gap in the sequence. The same reasoning
    #: the document sequences use, at a smaller scale.
    last_version_number = models.PositiveIntegerField(_("last version number"), default=0)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recipes_created",
        null=True,
        blank=True,
        verbose_name=_("created by"),
    )

    #: The identity a later production batch will point at. Immutable, and
    #: deliberately not the primary key or the code: a code can be corrected,
    #: and a posted batch has to still point at something in five years.
    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe")
        verbose_name_plural = _("recipes")
        ordering = ["organization__code", "code"]
        permissions = [
            ("manage_recipe", _("Can create, edit and archive recipes")),
            ("view_recipe_cost", _("Can view recipe cost columns")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="recipe_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="recipe_code_format"
            ),
            models.CheckConstraint(condition=~Q(name_ar=""), name="recipe_name_ar_not_empty"),
            # A portion recipe's output is a plated dish that deliberately is
            # not an InventoryItem (RCP-007); a batch recipe must produce
            # something stock can hold.
            models.CheckConstraint(
                condition=(
                    Q(recipe_type=RecipeType.BATCH, output_item__isnull=False)
                    | Q(recipe_type=RecipeType.PORTION, output_item__isnull=True)
                ),
                name="recipe_output_item_matches_type",
            ),
            _provenance_constraint("recipe_provenance_is_complete"),
        ]
        indexes = [
            models.Index(fields=["organization", "is_active"], name="recipe_org_active_idx"),
            models.Index(fields=["recipe_type"], name="recipe_type_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


class RecipeBranch(TimeStampedModel):
    """
    A branch this recipe applies at.

    An explicit through model rather than a plain `ManyToManyField` so the row
    can be audited and dated, and emphatically not a comma-separated field: a
    mutable string of ids cannot be constrained, joined, or explained.

    **No rows means the recipe applies organization-wide.** That is the common
    case, and making it the default costs nothing; requiring every branch to be
    listed would mean a new branch silently loses the whole menu.
    """

    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name="branch_applicability",
        verbose_name=_("recipe"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="recipe_applicability",
        verbose_name=_("branch"),
    )
    notes = models.TextField(_("notes"), blank=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe branch")
        verbose_name_plural = _("recipe branches")
        ordering = ["recipe__code", "branch__code"]
        constraints = [
            models.UniqueConstraint(
                fields=["recipe", "branch"], name="recipe_branch_unique_per_recipe"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.recipe.code} @ {self.branch.code}"


class RecipeVersion(TimeStampedModel, SourceProvenance):
    """
    One draft of a recipe's composition and method.

    **Task 3.1 reaches `DRAFT` and nothing else.** There is no submit, approve,
    activate or supersede service, no effective dating, and no resolver that
    picks a version for a date — all of that is Task 3.2, and the check
    constraint below is what stops a half-built lifecycle from appearing by
    accident in the meantime.

    `batch_size` and `expected_output_quantity` are the recipe book's own
    scale: the مندي pit takes 40 chickens and 50 kg of rice, and that is what a
    version records. It is not a menu quantity, and dividing it into servings
    is `RecipeServing`'s job (§5C).
    """

    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name="versions",
        verbose_name=_("recipe"),
    )
    version_number = models.PositiveIntegerField(_("version"))
    status = models.CharField(
        _("status"),
        max_length=12,
        choices=RecipeVersionStatus.choices,
        default=RecipeVersionStatus.DRAFT,
    )

    #: How much of the output one batch of these line quantities makes.
    batch_size = models.DecimalField(
        _("batch size"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
        default=Decimal("1"),
    )
    expected_output_quantity = models.DecimalField(
        _("expected output"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    output_unit = models.ForeignKey(
        "units.UnitOfMeasure",
        on_delete=models.PROTECT,
        related_name="recipe_version_outputs",
        verbose_name=_("output unit"),
    )

    #: Informational rates the charter names. Never summed with the per-line
    #: `loss_rate`, and never an input to costing (RCP-018, RCP-060).
    preparation_loss = models.DecimalField(
        _("preparation loss"),
        max_digits=CALCULATION_PLACES + 4,
        decimal_places=CALCULATION_PLACES,
        null=True,
        blank=True,
    )
    cooking_yield = models.DecimalField(
        _("cooking yield"),
        max_digits=CALCULATION_PLACES + 4,
        decimal_places=CALCULATION_PLACES,
        null=True,
        blank=True,
    )

    #: The one-paragraph summary a chef reads before starting. It may not be
    #: the only record of the method — that is what `RecipeStep` is for
    #: (RCP-063).
    instructions = models.TextField(_("overview"), blank=True)
    notes = models.TextField(_("notes"), blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recipe_versions_created",
        null=True,
        blank=True,
        verbose_name=_("created by"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe version")
        verbose_name_plural = _("recipe versions")
        ordering = ["recipe__code", "-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["recipe", "version_number"],
                name="recipe_version_number_unique_per_recipe",
            ),
            # Task 3.1's boundary, held by the database rather than by
            # intention. Task 3.2 alters this when the lifecycle arrives.
            models.CheckConstraint(
                condition=Q(status=RecipeVersionStatus.DRAFT),
                name="recipe_version_task_3_1_draft_only",
            ),
            # One open draft per recipe while there is no approval lifecycle to
            # tell two of them apart.
            models.UniqueConstraint(
                fields=["recipe"],
                condition=Q(status=RecipeVersionStatus.DRAFT),
                name="recipe_version_one_draft_per_recipe",
            ),
            models.CheckConstraint(
                condition=Q(batch_size__gt=Decimal("0")),
                name="recipe_version_batch_size_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(expected_output_quantity__gt=Decimal("0")),
                name="recipe_version_output_is_positive",
            ),
            _provenance_constraint("recipe_version_provenance_is_complete"),
        ]

    def __str__(self) -> str:
        return f"{self.recipe.code} v{self.version_number}"

    @property
    def is_draft(self) -> bool:
        return self.status == RecipeVersionStatus.DRAFT


class RecipeLine(TimeStampedModel, SourceProvenance):
    """
    One ingredient — or one piece of packaging — in a version.

    Quantities are **gross**: what leaves stores for one `batch_size`, before
    preparation loss and cooking shrinkage (RCP-018). `measured_quantity` and
    `quantity` are two different facts and stay apart, because the workbook
    keeps them apart: `كمية القياس` is what the chef put on the scale, and
    `الكمية المعتمدة` is what chef, accountant and manager agreed to cost
    (RCP-062).

    **No cost field.** Task 3.3 owns costing, and a unit cost stored here would
    be a copy of the ledger's moving average that starts drifting the moment
    the next receipt posts.
    """

    version = models.ForeignKey(
        RecipeVersion,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("version"),
    )
    line_order = models.PositiveIntegerField(_("line order"))

    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="recipe_lines",
        verbose_name=_("item"),
    )

    #: The unit the quantity was entered in, when it was entered as a unit of
    #: measure rather than as a package. Converted to the item's base unit
    #: once, at entry, with both figures kept (RCP-019). Exactly one of
    #: `entered_unit` and `package_unit` is set.
    entered_unit = models.ForeignKey(
        "units.UnitOfMeasure",
        on_delete=models.PROTECT,
        related_name="recipe_lines",
        null=True,
        blank=True,
        verbose_name=_("entered unit"),
    )
    entered_quantity = models.DecimalField(
        _("entered quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    #: `الكمية المعتمدة` — the approved figure, in the item's base unit. The
    #: only quantity costing will read.
    base_quantity = models.DecimalField(
        _("approved base quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    #: `كمية القياس` — what the scale said, in the entered unit. Null for a
    #: recipe that arrived already agreed and has no measurement to record.
    measured_quantity = models.DecimalField(
        _("measured quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
        null=True,
        blank=True,
    )

    #: The package this quantity was expressed in, when it was a package rather
    #: than a bare unit, with the conversion snapshotted so a later correction
    #: to the factor cannot restate what this line meant.
    package_unit = models.ForeignKey(
        "inventory.PackageUnit",
        on_delete=models.PROTECT,
        related_name="recipe_lines",
        null=True,
        blank=True,
        verbose_name=_("package unit"),
    )
    conversion_factor = models.DecimalField(
        _("conversion factor"),
        max_digits=FACTOR_MAX_DIGITS,
        decimal_places=FACTOR_PLACES,
        null=True,
        blank=True,
    )
    conversion_version = models.PositiveIntegerField(_("conversion version"), null=True, blank=True)

    #: `فاقد %` — cleaning, bone, evaporation, cutting or cooking difference.
    #: Informational: the costing input stays the gross approved quantity
    #: (RCP-060). Held as a rate in [0, 1), not a percentage.
    loss_rate = models.DecimalField(
        _("loss rate"),
        max_digits=CALCULATION_PLACES + 4,
        decimal_places=CALCULATION_PLACES,
        null=True,
        blank=True,
    )

    cost_class = models.CharField(
        _("cost class"),
        max_length=16,
        choices=RecipeLineCostClass.choices,
        default=RecipeLineCostClass.FOOD,
    )
    preparation_stage = models.CharField(
        _("stage"),
        max_length=12,
        choices=PreparationStage.choices,
        blank=True,
    )
    measurement_basis = models.CharField(
        _("measurement basis"),
        max_length=12,
        choices=MeasurementBasis.choices,
        default=MeasurementBasis.RAW,
    )

    is_optional = models.BooleanField(_("optional"), default=False)
    note = models.TextField(_("note"), blank=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe line")
        verbose_name_plural = _("recipe lines")
        ordering = ["version", "line_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "line_order"],
                name="recipe_line_order_unique_per_version",
            ),
            models.CheckConstraint(
                condition=Q(entered_quantity__gt=Decimal("0")),
                name="recipe_line_entered_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(base_quantity__gt=Decimal("0")),
                name="recipe_line_base_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(measured_quantity__isnull=True) | Q(measured_quantity__gt=Decimal("0")),
                name="recipe_line_measured_quantity_is_positive",
            ),
            # A loss rate is a proportion, not a percentage, and a line that
            # loses everything is not a line.
            models.CheckConstraint(
                condition=Q(loss_rate__isnull=True)
                | Q(loss_rate__gte=Decimal("0"), loss_rate__lt=Decimal("1")),
                name="recipe_line_loss_rate_in_range",
            ),
            # A package quantity without its snapshotted factor could not be
            # re-derived later, and a factor without a package means nothing.
            models.CheckConstraint(
                condition=(
                    Q(package_unit__isnull=True, conversion_factor__isnull=True)
                    | Q(package_unit__isnull=False, conversion_factor__isnull=False)
                ),
                name="recipe_line_package_carries_its_factor",
            ),
            # A quantity was typed in *something*, and in exactly one thing:
            # eight hundred grams, or two sacks, never both at once.
            models.CheckConstraint(
                condition=(
                    Q(entered_unit__isnull=False, package_unit__isnull=True)
                    | Q(entered_unit__isnull=True, package_unit__isnull=False)
                ),
                name="recipe_line_entered_in_a_unit_or_a_package",
            ),
            models.CheckConstraint(
                condition=Q(conversion_factor__isnull=True) | Q(conversion_factor__gt=Decimal("0")),
                name="recipe_line_conversion_factor_is_positive",
            ),
            _provenance_constraint("recipe_line_provenance_is_complete"),
        ]
        indexes = [
            models.Index(fields=["item"], name="recipe_line_item_idx"),
            models.Index(fields=["version", "cost_class"], name="recipe_line_class_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.version} #{self.line_order} {self.item.code}"


class RecipeLineSubstitute(TimeStampedModel, SourceProvenance):
    """
    An acceptable stand-in when the primary item is short.

    **Guidance, never automation** (RCP-022). Nothing substitutes on its own;
    the batch screen offers the list, and a production batch records what was
    *actually* consumed. The substitute table never enters cost arithmetic,
    which is why it carries no quantity relationship in Task 3.1: a
    substitution's real quantity is measured at the batch, by whoever made it.
    """

    line = models.ForeignKey(
        RecipeLine,
        on_delete=models.CASCADE,
        related_name="substitutes",
        verbose_name=_("line"),
    )
    substitute_item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="recipe_line_substitutes",
        verbose_name=_("substitute item"),
    )
    priority = models.PositiveIntegerField(_("priority"), default=1)
    reason = models.CharField(_("reason"), max_length=200, blank=True)
    note = models.TextField(_("note"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe line substitute")
        verbose_name_plural = _("recipe line substitutes")
        ordering = ["line", "priority"]
        constraints = [
            # One active substitute per item per line. Archived rows are left
            # alone so the history of what was once acceptable survives.
            models.UniqueConstraint(
                fields=["line", "substitute_item"],
                condition=Q(is_active=True),
                name="recipe_substitute_unique_active_per_line",
            ),
            # Ranked alternatives, and the ranking has to be an order rather
            # than a suggestion: two substitutes both at priority 1 leave the
            # batch screen choosing by primary key, which is not a business
            # decision. Scoped to active rows so an archived substitute does
            # not hold a rank nothing uses.
            models.UniqueConstraint(
                fields=["line", "priority"],
                condition=Q(is_active=True),
                name="recipe_substitute_priority_unique_per_line",
            ),
            models.CheckConstraint(
                condition=Q(priority__gt=0), name="recipe_substitute_priority_is_positive"
            ),
            _provenance_constraint("recipe_substitute_provenance_is_complete"),
        ]

    def __str__(self) -> str:
        return f"{self.line} → {self.substitute_item.code}"


class RecipeStep(TimeStampedModel, SourceProvenance):
    """
    One numbered step of the method.

    A recipe that records only ingredients answers the accountant and abandons
    the cook. Steps are rows, not prose, so they can be sequenced, timed,
    checked off and diffed between versions (RCP-063).

    **Steps carry no arithmetic** (RCP-066). No step affects cost, consumption,
    theoretical quantity, yield or any posting. Adding or deleting one cannot
    change a line's quantity.

    Two fields are deliberately null far more often than they are set:
    `expected_duration` is populated only where the recipe book gives a
    duration, and `temperature_c` only where a source gives a **number**. The
    book gives نار هادئة, جمر, قدر الضغط and تنور — heat instructions, never
    degrees — so those land in `heat_instruction_ar` and the Celsius column
    stays empty. A blank temperature asks a question; an invented one becomes
    food-safety guidance nobody approved (RCP-068).

    There is no `station` field. `KitchenStation` is not created in Task 3.1
    (KD-07), and §5A.2 explicitly rejected a free-text station string because
    free text meant for grouping ends up with four spellings of one station.
    """

    version = models.ForeignKey(
        RecipeVersion,
        on_delete=models.CASCADE,
        related_name="steps",
        verbose_name=_("version"),
    )
    sequence = models.PositiveIntegerField(_("sequence"))

    instruction_ar = models.TextField(_("instruction (Arabic)"))
    instruction_en = models.TextField(_("instruction (English)"), blank=True)

    stage = models.CharField(
        _("stage"), max_length=12, choices=PreparationStage.choices, blank=True
    )
    expected_duration = models.DurationField(_("expected duration"), null=True, blank=True)
    temperature_c = models.DecimalField(
        _("temperature (°C)"),
        max_digits=CALCULATION_PLACES + 4,
        decimal_places=CALCULATION_PLACES,
        null=True,
        blank=True,
    )
    #: What the source actually said about heat when it did not say a number.
    heat_instruction_ar = models.CharField(_("heat instruction"), max_length=200, blank=True)

    checkpoint_ar = models.TextField(_("quality checkpoint"), blank=True)
    is_critical = models.BooleanField(_("critical checkpoint"), default=False)
    #: A reference, not a file. Uploading photographs means the Task 1.7
    #: file-upload security rules, deferred to Task 3.10 (§5A.2).
    media_reference = models.CharField(_("media reference"), max_length=200, blank=True)
    note = models.TextField(_("note"), blank=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe step")
        verbose_name_plural = _("recipe steps")
        ordering = ["version", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "sequence"],
                name="recipe_step_sequence_unique_per_version",
            ),
            models.CheckConstraint(
                condition=~Q(instruction_ar=""), name="recipe_step_instruction_ar_not_empty"
            ),
            models.CheckConstraint(
                condition=Q(sequence__gt=0), name="recipe_step_sequence_is_positive"
            ),
            models.CheckConstraint(
                condition=Q(temperature_c__isnull=True) | Q(temperature_c__gt=Decimal("-273.15")),
                name="recipe_step_temperature_above_absolute_zero",
            ),
            _provenance_constraint("recipe_step_provenance_is_complete"),
        ]

    def __str__(self) -> str:
        return f"{self.version} step {self.sequence}"


class RecipeStepIngredient(TimeStampedModel):
    """
    Documentation that an ingredient enters at this step.

    It says **when**, never **how much exists** (RCP-066). The line's quantity
    is the whole quantity regardless of how many steps mention it, and the
    costing formulas do not read this table at all. `share` may sum to less
    than 1 across a line's steps — that means the method has not described
    where the rest goes — and may never exceed 1, which would claim to add more
    of an ingredient than the recipe contains (RCP-067).
    """

    step = models.ForeignKey(
        RecipeStep,
        on_delete=models.CASCADE,
        related_name="ingredient_links",
        verbose_name=_("step"),
    )
    recipe_line = models.ForeignKey(
        RecipeLine,
        on_delete=models.CASCADE,
        related_name="step_links",
        verbose_name=_("line"),
    )
    share = models.DecimalField(
        _("share"),
        max_digits=CALCULATION_PLACES + 4,
        decimal_places=CALCULATION_PLACES,
        default=Decimal("1"),
    )
    note = models.TextField(_("note"), blank=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("step ingredient")
        verbose_name_plural = _("step ingredients")
        ordering = ["step", "recipe_line"]
        constraints = [
            models.UniqueConstraint(
                fields=["step", "recipe_line"], name="recipe_step_ingredient_unique"
            ),
            models.CheckConstraint(
                condition=Q(share__gt=Decimal("0"), share__lte=Decimal("1")),
                name="recipe_step_ingredient_share_in_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.step} ← {self.recipe_line.item.code}"


class RecipeServing(TimeStampedModel, SourceProvenance):
    """
    A way of dividing this version's output into something sellable.

    The generic answer to `حبة كاملة`, `نصف حبة`, `حصة`, `فخذ`, and to the
    350 g and 500 g portions the recipe book states. **Nothing here names a
    dish, an animal or a cut**: `0.500` is a row, not a branch in a service
    (RCP-082).

    Servings convert the **output**; they do not scale the **plate** (RCP-123).
    One whole chicken is exactly two halves, and the meat cost divides by two.
    What accompanies that half is a separate approved composition whose rice
    does not halve — 700 g against 1,300 g on the two mandi cards — which is
    why no code may derive one sellable plate from another (RCP-124).

    **No cost and no price** in Task 3.1. `cost_per_serving` is Task 3.3's, and
    a selling price is Phase 4's and belongs to the business.
    """

    version = models.ForeignKey(
        RecipeVersion,
        on_delete=models.CASCADE,
        related_name="servings",
        verbose_name=_("version"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)

    serving_quantity = models.DecimalField(
        _("serving quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    serving_unit = models.ForeignKey(
        "units.UnitOfMeasure",
        on_delete=models.PROTECT,
        related_name="recipe_servings",
        verbose_name=_("serving unit"),
    )
    #: `serving_quantity` converted into the version's output unit, once, at
    #: entry. A serving whose unit is not convertible to the output basis is
    #: refused at the unit layer, not left as a puzzle for costing (RCP-083).
    base_quantity = models.DecimalField(
        _("serving quantity in output unit"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    #: `base_quantity ÷ the version's expected output`. Twelve places, because
    #: this is a technical identity and a factor of 20 error here is a costing
    #: error everywhere downstream.
    factor_of_batch = models.DecimalField(
        _("factor of batch"),
        max_digits=FACTOR_MAX_DIGITS,
        decimal_places=FACTOR_PLACES,
    )

    is_primary = models.BooleanField(_("primary"), default=False)
    rounding_increment = models.DecimalField(
        _("rounding increment"),
        max_digits=INCREMENT_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )
    rounding_policy = models.CharField(
        _("rounding policy"),
        max_length=8,
        choices=ServingRoundingPolicy.choices,
        default=ServingRoundingPolicy.NONE,
    )
    measurement_basis = models.CharField(
        _("measurement basis"),
        max_length=12,
        choices=MeasurementBasis.choices,
        default=MeasurementBasis.COOKED,
    )

    display_order = models.PositiveIntegerField(_("display order"), default=1)
    is_active = models.BooleanField(_("active"), default=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe serving")
        verbose_name_plural = _("recipe servings")
        ordering = ["version", "display_order", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "code"], name="recipe_serving_code_unique_per_version"
            ),
            # Exactly one default answer to "what does one cost".
            models.UniqueConstraint(
                fields=["version"],
                condition=Q(is_primary=True),
                name="recipe_serving_one_primary_per_version",
            ),
            models.CheckConstraint(
                condition=Q(serving_quantity__gt=Decimal("0")),
                name="recipe_serving_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(base_quantity__gt=Decimal("0")),
                name="recipe_serving_base_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(factor_of_batch__gt=Decimal("0")),
                name="recipe_serving_factor_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(rounding_increment__isnull=True)
                | Q(rounding_increment__gt=Decimal("0")),
                name="recipe_serving_increment_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="recipe_serving_code_format"
            ),
            _provenance_constraint("recipe_serving_provenance_is_complete"),
        ]

    def __str__(self) -> str:
        return f"{self.version} · {self.code}"

    @property
    def factor_display(self) -> str:
        """
        The factor as a technical identity — always a period, never a comma,
        and always at full stored precision.

        Django localises Decimals, so under Arabic this would otherwise render
        `0,033333333333`. A comma there is ambiguous and invites a mis-typed
        re-entry (`CLAUDE.md`, locale-independence rule), and this is the exact
        case that rule names: a conversion factor.

        Quantized rather than formatted as-is so the rendering does not depend
        on whether the value has been round-tripped through the database: a
        freshly assigned `Decimal("0.5")` and the same value re-read must not
        display differently. Mirrors `ItemPackageConversion.factor_display`.
        """
        quantum = Decimal(1).scaleb(-FACTOR_PLACES)
        return f"{self.factor_of_batch.quantize(quantum):f}"
