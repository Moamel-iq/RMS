"""
Recipes, their versions, the structure hanging off a version, and the approval
lifecycle that freezes it.

Task 3.1 delivered **master data and draft structure only**, and pinned every
version to `DRAFT` at the database so a half-built lifecycle could not appear
by accident. **Task 3.2A removes that pin and supplies the whole boundary at
once**: submission, four-party review evidence, maker-checker approval,
explicit activation, effective-dated branch scope, database overlap
enforcement, an authoritative resolver, supersession, and whole-row
immutability. They arrive together because any one of them alone is a
false promise — an approval screen over mutable rows is worse than no approval
screen.

**Task 3.2B adds `RecipeComponent`**, the non-stocked nested-recipe graph:
exact parent-version to child-version links, the mutual exclusion that makes
double counting unrepresentable, cycle and depth bounds, and effective-coverage
validation at activation. With it Task 3.2 is complete.

**Costing is still absent.** Roll-up is Task 3.3, flattening into a production
batch is Task 3.4, and production itself is Task 3.5. Nothing here moves stock,
touches a ledger, or knows a price. A recipe is an intention; the production
batch is the event (RCP-002).

See `docs/tasks/task-3-0-recipes-production-domain-spec.md` for the approved
design — §3 the recipe, §4 versions, §5 lines, §5A steps, §5B components,
§5C servings — plus
`docs/decisions/ADR-024-recipe-versioning-and-the-effective-dated-cost-basis.md`
for why the lifecycle is shaped this way, and
`docs/invariants/kitchen-invariants.md` for the rules these must satisfy.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TimeStampedModel
from apps.core.money import MONEY_PLACES, UNIT_PRICE_PLACES
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
#: A cost snapshot stores money at the posted-line precision (ADR-012) and unit
#: rates at the unit-price precision, with the same head-room every other money
#: column in this repository carries.
MONEY_MAX_DIGITS = MONEY_PLACES + 15
UNIT_PRICE_MAX_DIGITS = UNIT_PRICE_PLACES + 15
#: A raw line extension is `quantity(6 dp) x unit cost(6 dp)`, so it has at most
#: twelve decimal places and is stored **exactly**. Nothing rounds on the way to
#: the total; only the total rounds, once (§K).
EXTENSION_MAX_DIGITS = FACTOR_PLACES + 20


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
    The approved lifecycle of one version (spec §4, ADR-024).

    Six states, and each one exists because something different is true of the
    version while it sits there:

    * `DRAFT` — somebody is typing. Freely editable, freely discarded, and the
      only state in which any owned child row may be written at all.
    * `SUBMITTED` — the structure is frozen so reviewers all read the same
      thing. Reviews are recorded here and nowhere else.
    * `APPROVED` — the four-party control is satisfied and the version is
      immutable, but it is **not yet effective**: nothing may resolve it for a
      business date until somebody activates it.
    * `ACTIVE` — approved *and* claimed for at least one branch over an
      explicit date range.
    * `REJECTED` — refused, with an actor, a timestamp and a reason. Kept, not
      deleted: a refusal is evidence.
    * `SUPERSEDED` — replaced by a named later version, its range closed the
      day before the replacement's begins. **Still resolvable for its own
      historical dates**, which is the whole point of effective dating.

    **There is no `EXPIRED`, deliberately.** Task 3.0 §4 names one terminal
    state and this follows the specification. Expiry is not a state a version
    is *in*; it is a fact about a date — a version whose `effective_to` has
    passed simply does not cover today, which the resolver already answers from
    the range. Storing it would need a clock-driven job, and on any day that
    job did not run the stored status and the stored range would disagree about
    the same version. `is_expired_on()` derives it instead.

    **There is no `DISCARDED` either.** Discarding a draft deletes the row —
    nothing outside a draft may reference one — and the audit event carries the
    identity forward. A status nothing ever reaches would be a lie in an enum.
    """

    DRAFT = "DRAFT", _("مسودة")
    SUBMITTED = "SUBMITTED", _("مُرسلة للمراجعة")
    APPROVED = "APPROVED", _("معتمدة")
    ACTIVE = "ACTIVE", _("سارية")
    REJECTED = "REJECTED", _("مرفوضة")
    SUPERSEDED = "SUPERSEDED", _("مستبدلة")


#: Still being written, or still under review. At most one of these may exist
#: per recipe at a time — two versions in flight would race for the same
#: effective range and one of them would lose after all the review work.
OPEN_VERSION_STATUSES: frozenset[str] = frozenset(
    {RecipeVersionStatus.DRAFT, RecipeVersionStatus.SUBMITTED}
)

#: Left `DRAFT` and therefore frozen: no owned child row may be inserted,
#: updated or deleted, and the header moves only along a permitted transition.
FROZEN_VERSION_STATUSES: frozenset[str] = frozenset(
    {
        RecipeVersionStatus.SUBMITTED,
        RecipeVersionStatus.APPROVED,
        RecipeVersionStatus.ACTIVE,
        RecipeVersionStatus.REJECTED,
        RecipeVersionStatus.SUPERSEDED,
    }
)

#: The only statuses `resolve_recipe_version` will ever return. `APPROVED` is
#: absent on purpose: approval is agreement, activation is the claim on a date.
RESOLVABLE_VERSION_STATUSES: frozenset[str] = frozenset(
    {RecipeVersionStatus.ACTIVE, RecipeVersionStatus.SUPERSEDED}
)

#: Carrying an approval, and therefore requiring complete evidence.
APPROVED_VERSION_STATUSES: frozenset[str] = frozenset(
    {
        RecipeVersionStatus.APPROVED,
        RecipeVersionStatus.ACTIVE,
        RecipeVersionStatus.SUPERSEDED,
    }
)

#: What a `RecipeComponent` may point at: a version that has passed the
#: four-party control and is frozen. `DRAFT` and `SUBMITTED` are refused because
#: a parent may not be built on something still being written or still being
#: argued about; `REJECTED` because somebody refused to sign it.
#:
#: Deliberately the same set as `APPROVED_VERSION_STATUSES` rather than a
#: narrower one. Drafting a parent against an `APPROVED` child is permitted —
#: spec §5B says the child must be approved, and RCP-074 puts the *effective*
#: test at the parent's **activation**, not at draft time. Requiring `ACTIVE` to
#: draft would deadlock the ordinary case where a blend and the dish that uses
#: it are prepared in one sitting.
COMPONENT_ELIGIBLE_STATUSES: frozenset[str] = APPROVED_VERSION_STATUSES

#: The most component edges permitted on any root-to-leaf path (KD-08,
#: RCP-077). A parent with one component is at depth 1.
#:
#: "An ingredient inside a blend inside a marinade inside a dish is a real
#: kitchen; four levels is almost always a modelling error." Enforced as a named
#: constant with a named error and a reported path — never an assertion, and
#: never a silent truncation of the tree below it.
MAX_COMPONENT_DEPTH = 3


class RecipeReviewType(models.TextChoices):
    """
    The four responsibilities `KM-RCP-004`'s signature page names.

    **No global `CHEF` role is invented for this.** The workbook assigns the
    approved quantity to *"الشيف + المحاسب + المدير"* — three parties — and its
    signature page carries a fourth for the store. Those are *responsibilities
    exercised on one document*, not posts in the organization chart, so they
    live here as review types and are carried by whichever role the deployment
    already grants `review_recipe_version` to. Adding a `CHEF` to `Role` would
    change the global access model of the whole ERP to record one column of one
    kitchen form.

    `FINAL` is the manager's signature, written by `approve_recipe_version` (or
    `reject_recipe_version`) rather than by the review command, because it is
    the act that moves the version rather than an opinion about it.
    """

    KITCHEN = "KITCHEN", _("مراجعة المطبخ")
    STOREKEEPER = "STOREKEEPER", _("مراجعة المخزن")
    ACCOUNTING = "ACCOUNTING", _("مراجعة الكلفة")
    FINAL = "FINAL", _("الاعتماد النهائي")


#: The three that must be recorded, and approved, before a final approval is
#: even offered. A conjunction: each removes a different way for an unchecked
#: recipe to reach production.
REQUIRED_REVIEW_TYPES: tuple[str, ...] = (
    RecipeReviewType.KITCHEN,
    RecipeReviewType.STOREKEEPER,
    RecipeReviewType.ACCOUNTING,
)


class RecipeReviewDecision(models.TextChoices):
    """A reviewer either agrees or refuses. There is no 'seen'."""

    APPROVED = "APPROVED", _("موافقة")
    REJECTED = "REJECTED", _("رفض")


class ApprovalEvidenceKind(models.TextChoices):
    """
    What the approval is evidenced *by* (KD-02, RCP-058, RCP-126).

    The owner's KD-02 answer moved the data gate to the approval boundary: a
    real branch recipe may be captured as a draft, and may not be approved or
    activated until its `KM-RCP-004` costing and approval data is complete. So
    an approval must say which kind of evidence stands behind it, and
    `DEMO_FICTIONAL` is refused for anything outside the demo namespace — by a
    trigger as well as by the service, because a demo signoff that looked like
    a signed Khan Mandi record is exactly how unapproved figures acquire
    authority.
    """

    SIGNED_FORM = "SIGNED_FORM", _("نموذج اعتماد موقّع")
    DEMO_FICTIONAL = "DEMO_FICTIONAL", _("دليل تجريبي — غير حقيقي")


#: The prefix that marks the demo namespace. `DEMO_FICTIONAL` evidence is
#: permitted only for recipes whose code starts with it.
DEMO_CODE_PREFIX = "DEMO-"


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
            # The lifecycle authorities, separated because they are separated
            # in the kitchen: preparing a version, attesting one column of it,
            # signing it off and putting it into effect are four acts and the
            # control only works if they can be held by four people.
            ("submit_recipe_version", _("Can submit a draft version for review")),
            ("review_recipe_version", _("Can record a review signoff on a submitted version")),
            ("approve_recipe_version", _("Can give the final approval of a version")),
            ("reject_recipe_version", _("Can refuse a submitted version")),
            ("activate_recipe_version", _("Can put an approved version into effect")),
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
    One version of a recipe's composition and method, and its whole lifecycle.

    `batch_size` and `expected_output_quantity` are the recipe book's own
    scale: the مندي pit takes 40 chickens and 50 kg of rice, and that is what a
    version records. It is not a menu quantity, and dividing it into servings
    is `RecipeServing`'s job (§5C).

    **The effective range is `[effective_from, effective_to]`, inclusive at
    both ends**, with a null `effective_to` meaning open-ended. That is the
    repository's standing convention — `ItemPackageConversion` has used
    `daterange(effective_from, effective_to, '[]')` since Task 1.0 — and RCP-016
    depends on it: supersession closes the predecessor at *the day before* the
    replacement begins, which is only a seam with no gap and no overlap if the
    upper bound is included. The database constraint, the services, the
    resolver, the API and the screens all read it that way; nothing in this
    module treats `effective_to` as exclusive.

    **The range lives here and is materialised per branch** on
    `RecipeVersionBranchScope`, which is what the overlap constraint can
    actually see. The two are held equal by a trigger, so a scope row cannot
    quietly claim a different period from the version that owns it.

    **Immutable once it leaves `DRAFT`**, header and every owned child row,
    enforced by whole-row allowlist triggers rather than by this docstring.
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

    # --- The lifecycle -----------------------------------------------------
    #
    # Every actor and timestamp is recorded separately rather than inferred
    # from the audit trail. The audit trail answers "what happened"; these
    # columns answer "who is accountable", which a report has to be able to
    # join on and a constraint has to be able to see.

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recipe_versions_submitted",
        null=True,
        blank=True,
        verbose_name=_("submitted by"),
    )
    submitted_at = models.DateTimeField(_("submitted at"), null=True, blank=True)

    #: The checker. Never the author — a `CheckConstraint` as well as the
    #: service, exactly as the purchase request holds it (RCP-013, PRC-010).
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recipe_versions_approved",
        null=True,
        blank=True,
        verbose_name=_("approved by"),
    )
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)

    #: `يعتمد من تاريخ` on the workbook's costing card: which signed approval
    #: form stands behind this version. Required from `APPROVED` onwards.
    approval_reference = models.CharField(_("approval reference"), max_length=120, blank=True)
    approval_evidence_kind = models.CharField(
        _("approval evidence"),
        max_length=16,
        choices=ApprovalEvidenceKind.choices,
        blank=True,
    )

    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recipe_versions_activated",
        null=True,
        blank=True,
        verbose_name=_("activated by"),
    )
    activated_at = models.DateTimeField(_("activated at"), null=True, blank=True)

    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recipe_versions_rejected",
        null=True,
        blank=True,
        verbose_name=_("rejected by"),
    )
    rejected_at = models.DateTimeField(_("rejected at"), null=True, blank=True)
    rejection_reason = models.TextField(_("rejection reason"), blank=True)

    #: Inclusive at both ends; null `effective_to` is open-ended. Set at
    #: activation, never at approval: an approved version is agreed, not yet
    #: claimed for a date.
    effective_from = models.DateField(_("effective from"), null=True, blank=True)
    effective_to = models.DateField(_("effective to"), null=True, blank=True)

    superseded_at = models.DateTimeField(_("superseded at"), null=True, blank=True)
    #: The named replacement. Supersession without one would leave a version
    #: closed by nobody, which is indistinguishable on a screen from a version
    #: whose range simply ran out.
    superseded_by_version = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="supersedes",
        null=True,
        blank=True,
        verbose_name=_("superseded by"),
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
            # One version in flight per recipe. Task 3.1 held this for drafts
            # alone; the lifecycle widens it to `SUBMITTED` as well, because
            # two versions under review would race for one effective range and
            # the loser would discover it only after every reviewer had signed.
            # An `ACTIVE` version and a new `DRAFT` still coexist happily, which
            # is the relaxation Task 3.1 promised.
            models.UniqueConstraint(
                fields=["recipe"],
                condition=Q(status__in=sorted(OPEN_VERSION_STATUSES)),
                name="recipe_version_one_open_per_recipe",
            ),
            models.CheckConstraint(
                condition=Q(batch_size__gt=Decimal("0")),
                name="recipe_version_batch_size_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(expected_output_quantity__gt=Decimal("0")),
                name="recipe_version_output_is_positive",
            ),
            # Maker-checker, in the database. The service refuses first and
            # says why; this is what holds when something bypasses it.
            models.CheckConstraint(
                condition=(
                    Q(approved_by__isnull=True)
                    | Q(created_by__isnull=True)
                    | ~Q(approved_by=models.F("created_by"))
                ),
                name="recipe_version_approver_is_not_the_author",
            ),
            models.CheckConstraint(
                condition=(
                    Q(approved_by__isnull=True)
                    | Q(submitted_by__isnull=True)
                    | ~Q(approved_by=models.F("submitted_by"))
                ),
                name="recipe_version_approver_is_not_the_submitter",
            ),
            # An approval that cannot name its actor, its moment or its
            # evidence is not an approval; it is a status column.
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=sorted(APPROVED_VERSION_STATUSES))
                    | (
                        Q(approved_by__isnull=False)
                        & Q(approved_at__isnull=False)
                        & ~Q(approval_reference="")
                        & ~Q(approval_evidence_kind="")
                    )
                ),
                name="recipe_version_approval_carries_its_evidence",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status=RecipeVersionStatus.SUBMITTED)
                    | (Q(submitted_by__isnull=False) & Q(submitted_at__isnull=False))
                ),
                name="recipe_version_submission_names_its_actor",
            ),
            # A refusal with no reason teaches nobody anything.
            models.CheckConstraint(
                condition=(
                    ~Q(status=RecipeVersionStatus.REJECTED)
                    | (
                        Q(rejected_by__isnull=False)
                        & Q(rejected_at__isnull=False)
                        & ~Q(rejection_reason="")
                    )
                ),
                name="recipe_version_rejection_carries_its_reason",
            ),
            # Effective only from activation, and only with a range.
            models.CheckConstraint(
                condition=(
                    ~Q(
                        status__in=[
                            RecipeVersionStatus.ACTIVE,
                            RecipeVersionStatus.SUPERSEDED,
                        ]
                    )
                    | (
                        Q(effective_from__isnull=False)
                        & Q(activated_by__isnull=False)
                        & Q(activated_at__isnull=False)
                    )
                ),
                name="recipe_version_effective_range_starts_at_activation",
            ),
            models.CheckConstraint(
                condition=(
                    Q(effective_to__isnull=True)
                    | (
                        Q(effective_from__isnull=False)
                        & Q(effective_to__gte=models.F("effective_from"))
                    )
                ),
                name="recipe_version_effective_range_is_ordered",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status=RecipeVersionStatus.SUPERSEDED)
                    | (
                        Q(superseded_at__isnull=False)
                        & Q(superseded_by_version__isnull=False)
                        & Q(effective_to__isnull=False)
                    )
                ),
                name="recipe_version_supersession_names_its_replacement",
            ),
            # A version cannot replace itself. Without this a supersession loop
            # of length one would satisfy every other rule here.
            models.CheckConstraint(
                condition=(
                    Q(superseded_by_version__isnull=True) | ~Q(superseded_by_version=models.F("id"))
                ),
                name="recipe_version_is_not_its_own_replacement",
            ),
            _provenance_constraint("recipe_version_provenance_is_complete"),
        ]
        indexes = [
            models.Index(fields=["recipe", "status"], name="recipe_version_status_idx"),
            models.Index(fields=["status", "effective_from"], name="recipe_version_effective_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.recipe.code} v{self.version_number}"

    @property
    def is_draft(self) -> bool:
        return self.status == RecipeVersionStatus.DRAFT

    @property
    def is_frozen(self) -> bool:
        """Left `DRAFT`, and therefore immutable in every owned table."""
        return self.status in FROZEN_VERSION_STATUSES

    def covers(self, on_date: datetime.date) -> bool:
        """
        Whether this version's own range includes `on_date`, inclusive.

        The range alone — it says nothing about branch scope or status, and is
        never a substitute for `resolve_recipe_version`.
        """
        if self.effective_from is None or on_date < self.effective_from:
            return False
        return self.effective_to is None or on_date <= self.effective_to

    def is_expired_on(self, on_date: datetime.date) -> bool:
        """
        Whether the range has run out by `on_date`.

        Derived, never stored (see `RecipeVersionStatus`): a status column
        saying `EXPIRED` would need something to write it on the right morning,
        and would be wrong on every morning that something did not run.
        """
        return self.effective_to is not None and on_date > self.effective_to


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
    def quantity_display(self) -> str:
        """
        The serving quantity as a technical identity: period, never comma.

        Django localises Decimals, so under Arabic a stored `1.000000` renders
        `1,000000` — which reads as a million beside a money column that is
        genuinely grouped that way. Found by opening the cost card, which is
        why the card is worth opening.
        """
        return f"{self.serving_quantity:f}"

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


class RecipeComponent(TimeStampedModel, SourceProvenance):
    """
    One **non-stocked** sub-recipe a version is built on (spec §5B, RCP-070).

    A blend can be two entirely different things, and they cost differently:

    * **Stocked.** Somebody produced or bought it, it has a book value, it sits
      in a warehouse. The parent consumes it as an ordinary `RecipeLine` on its
      `output_item`, at the ledger's moving average, and its ingredient tree is
      never expanded again — those ingredients were already consumed, by the
      blend's own batch, on its own day.
    * **Non-stocked.** It is mixed into the pot during the dish's own production
      and never exists as stock. There is nothing to value, because there is no
      thing. That is this table.

    **The two shapes are mutually exclusive by construction, not by rule**
    (RCP-070). A recipe with an `output_item` may be referenced only as a line;
    a recipe without one only as a component. The service refuses each half and
    a trigger holds it. This is the design's answer to double counting: the
    system cannot represent *"charge the blend's book value **and** expand its
    ingredients"*, because whichever shape a sub-recipe has, the other reference
    is refused. Forbidding double counting in a rule leaves it one careless save
    away; making it unrepresentable does not.

    **The link is to one exact child version, and it never moves** (RCP-072).
    Not "recipe X", not "the current version of X", not "the latest approved
    X" — a specific frozen row. A blend that changed in September must not
    restate what the July dish claimed to contain, which is RCP-011's rule one
    level down. Adopting a newer child is a **new parent version**; there is no
    silent re-pointing anywhere in this module, and `on_delete=PROTECT` on
    `component_version` means the child cannot vanish underneath a parent that
    named it.

    **Carries no cost.** Roll-up costing is Task 3.3 and flattening into a
    production batch is Task 3.4. `multiplier` is a scaling identity — how many
    child `batch_size`s go into one parent `batch_size` — and multiplying it by
    a price is somebody else's task.

    Both recipes are denormalised onto the row and held equal to their versions'
    by trigger, for the same reason `RecipeVersionBranchScope` denormalises
    `recipe`: a `CheckConstraint` sees only its own table, and *"a version may
    not contain its own recipe"* is the one cycle case cheap enough to make
    unrepresentable rather than merely refused. It catches `A v2 → A v1`, which
    a version-identity check alone would let through.
    """

    version = models.ForeignKey(
        RecipeVersion,
        on_delete=models.CASCADE,
        related_name="components",
        verbose_name=_("version"),
    )
    #: Denormalised from `version.recipe`, held equal by trigger.
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.PROTECT,
        related_name="component_uses",
        verbose_name=_("recipe"),
    )
    line_order = models.PositiveIntegerField(_("line order"))

    #: The exact child. `PROTECT`, because a parent that named it must keep
    #: naming it — a component whose child was deleted is a recipe that claims
    #: to contain something unidentifiable.
    component_version = models.ForeignKey(
        RecipeVersion,
        on_delete=models.PROTECT,
        related_name="used_by_components",
        verbose_name=_("component version"),
    )
    #: Denormalised from `component_version.recipe`, held equal by trigger.
    component_recipe = models.ForeignKey(
        Recipe,
        on_delete=models.PROTECT,
        related_name="used_as_component",
        verbose_name=_("component recipe"),
    )

    #: How many child `batch_size`s enter one parent `batch_size`. A technical
    #: identity at the repository's factor precision — the same precision
    #: `RecipeServing.factor_of_batch` and `ItemPackageConversion.factor` use,
    #: rather than the six places the specification sketched, because a factor
    #: multiplied down three levels must not lose places on the way.
    multiplier = models.DecimalField(
        _("multiplier"),
        max_digits=FACTOR_MAX_DIGITS,
        decimal_places=FACTOR_PLACES,
    )

    note = models.TextField(_("note"), blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recipe_components_created",
        null=True,
        blank=True,
        verbose_name=_("created by"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe component")
        verbose_name_plural = _("recipe components")
        ordering = ["version", "line_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "line_order"],
                name="recipe_component_order_unique_per_version",
            ),
            # One parent version names one child **recipe** at most once, and
            # therefore through exactly one child version. Two rows for the same
            # blend would be a recipe that cannot say which version of it it
            # contains — and two rows for the same *version* would just be a
            # multiplier somebody forgot to add up.
            models.UniqueConstraint(
                fields=["version", "component_recipe"],
                name="recipe_component_child_recipe_unique_per_version",
            ),
            models.CheckConstraint(
                condition=Q(multiplier__gt=Decimal("0")),
                name="recipe_component_multiplier_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(line_order__gt=0),
                name="recipe_component_order_is_positive",
            ),
            # `A → A` at version identity.
            models.CheckConstraint(
                condition=~Q(component_version=models.F("version")),
                name="recipe_component_is_not_its_own_parent",
            ),
            # `A v2 → A v1` at recipe identity. The same dish one version
            # earlier is still the same dish, and a version-only check would
            # accept it (RCP-076).
            models.CheckConstraint(
                condition=~Q(component_recipe=models.F("recipe")),
                name="recipe_component_recipe_is_not_its_own_parent",
            ),
            _provenance_constraint("recipe_component_provenance_is_complete"),
        ]
        indexes = [
            models.Index(fields=["component_version"], name="recipe_component_child_idx"),
            models.Index(fields=["component_recipe"], name="recipe_component_child_rcp_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.version} #{self.line_order} → {self.component_version}"

    @property
    def multiplier_display(self) -> str:
        """
        The multiplier as a technical identity — always a period, never a comma.

        Django localises Decimals, so under Arabic this would otherwise render
        `0,250000000000`. `CLAUDE.md` names this exact case: a conversion factor
        is re-enterable, and a comma there invites a mis-typed re-entry. Mirrors
        `RecipeServing.factor_display`.
        """
        quantum = Decimal(1).scaleb(-FACTOR_PLACES)
        return f"{self.multiplier.quantize(quantum):f}"


# ---------------------------------------------------------------------------
# The approval lifecycle
# ---------------------------------------------------------------------------


class RecipeVersionReview(TimeStampedModel):
    """
    One party's signature on a submitted version.

    `KM-RCP-004`'s field guide assigns the approved quantity to *"الشيف +
    المحاسب + المدير"* and its signature page carries a fourth line for the
    store. This table is that page, as rows: who reviewed what, when, whether
    they agreed, and — for the costing review — which evidence they attested.

    **Append-only.** There is exactly one row per `(version, review_type)`, it
    is never updated and never deleted, and a trigger holds that. A reviewer
    who changes their mind does not edit a signature; the version is rejected
    and a new one is prepared, which is the correction mechanism §C names.

    A `REJECTED` review does not by itself move the version — it makes final
    approval refuse, and somebody with `reject_recipe_version` still has to
    close it. Recording a refusal and ending the version are two different
    acts, and one person may hold only the first.
    """

    version = models.ForeignKey(
        RecipeVersion,
        on_delete=models.PROTECT,
        related_name="reviews",
        verbose_name=_("version"),
    )
    review_type = models.CharField(_("review"), max_length=12, choices=RecipeReviewType.choices)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recipe_version_reviews",
        verbose_name=_("reviewer"),
    )
    decision = models.CharField(_("decision"), max_length=8, choices=RecipeReviewDecision.choices)
    reviewed_at = models.DateTimeField(_("reviewed at"))
    reason = models.TextField(_("reason"), blank=True)

    #: What the reviewer looked at: a `KM-RCP-004` form reference, a costing
    #: sheet, a dated note. Required of the costing review, because that is the
    #: review KD-02 is actually about.
    evidence_reference = models.CharField(_("evidence reference"), max_length=120, blank=True)
    evidence_kind = models.CharField(
        _("evidence kind"),
        max_length=16,
        choices=ApprovalEvidenceKind.choices,
        blank=True,
    )
    note = models.TextField(_("note"), blank=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe version review")
        verbose_name_plural = _("recipe version reviews")
        ordering = ["version", "review_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "review_type"],
                name="recipe_review_one_per_type_per_version",
            ),
            models.CheckConstraint(
                condition=(~Q(decision=RecipeReviewDecision.REJECTED) | ~Q(reason="")),
                name="recipe_review_refusal_carries_its_reason",
            ),
            # The costing review is where the approval evidence enters the
            # system. An accountant who agrees without naming what they read
            # has recorded an opinion, not a control.
            models.CheckConstraint(
                condition=(
                    ~Q(
                        review_type=RecipeReviewType.ACCOUNTING,
                        decision=RecipeReviewDecision.APPROVED,
                    )
                    | (~Q(evidence_reference="") & ~Q(evidence_kind=""))
                ),
                name="recipe_review_costing_names_its_evidence",
            ),
        ]
        indexes = [
            models.Index(fields=["version", "decision"], name="recipe_review_decision_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.version} · {self.review_type} · {self.decision}"


class RecipeVersionBranchScope(TimeStampedModel):
    """
    One branch this version is in effect at, over one date range.

    **The branch is never null**, and that is the whole design. An "empty list
    means everywhere" convention reads well and cannot be enforced: no database
    constraint can tell that a row claiming *all branches* collides with a row
    claiming *branch B*, because there is nothing to compare branch B against.
    So an organization-wide activation **materialises one row per applicable
    branch** and records that it did so in `is_organization_wide`. After that,
    "do these two claims overlap" is a question about two ordinary rows, and an
    `EXCLUDE USING gist` over `(recipe, branch, daterange)` answers it — the
    same mechanism `ItemPackageConversion` has used since Task 1.0.

    Three things are denormalised onto this row because an exclusion constraint
    can only see its own table: `recipe`, and the effective range. Triggers hold
    each of them equal to the owning version's, so a scope row cannot drift into
    claiming a period or a recipe its version never claimed.

    Rows appear at activation and are closed — never deleted — at supersession,
    which is why a superseded version still resolves for its own historical
    dates.
    """

    version = models.ForeignKey(
        RecipeVersion,
        on_delete=models.PROTECT,
        related_name="branch_scopes",
        verbose_name=_("version"),
    )
    #: Denormalised from `version.recipe`, held equal by a trigger. The
    #: exclusion constraint needs it on this row.
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.PROTECT,
        related_name="version_scopes",
        verbose_name=_("recipe"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="recipe_version_scopes",
        verbose_name=_("branch"),
    )

    effective_from = models.DateField(_("effective from"))
    effective_to = models.DateField(_("effective to"), null=True, blank=True)

    #: Provenance, not a scope modifier: this row exists because the activation
    #: claimed every applicable branch rather than naming this one.
    is_organization_wide = models.BooleanField(_("organization-wide"), default=False)
    note = models.TextField(_("note"), blank=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe version branch scope")
        verbose_name_plural = _("recipe version branch scopes")
        ordering = ["recipe", "branch", "effective_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "branch"],
                name="recipe_scope_one_row_per_version_and_branch",
            ),
            models.CheckConstraint(
                condition=(
                    Q(effective_to__isnull=True) | Q(effective_to__gte=models.F("effective_from"))
                ),
                name="recipe_scope_range_is_ordered",
            ),
        ]
        indexes = [
            models.Index(
                fields=["recipe", "branch", "effective_from"], name="recipe_scope_lookup_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.version} @ {self.branch.code} من {self.effective_from}"

    def covers(self, on_date: datetime.date) -> bool:
        """Whether this claim includes `on_date`, inclusive at both ends."""
        if on_date < self.effective_from:
            return False
        return self.effective_to is None or on_date <= self.effective_to


# ---------------------------------------------------------------------------
# Task 3.3 - immutable cost snapshots
# ---------------------------------------------------------------------------
#
# A cost card is derived and disposable; a snapshot is a decision somebody made
# on a date and has to be able to explain in September. The three tables below
# are **append-only**, enforced by database triggers rather than by discipline,
# and they store enough denormalised text that a snapshot stays readable after
# an item is renamed or a recipe archived.


class CostValuationMode(models.TextChoices):
    """
    Which inventory read a snapshot was taken against.

    One value, and the enum exists anyway. `POSTED_AS_OF` is the audit answer -
    "what did the books say at that moment" - and it is the **only** basis an
    authoritative recipe cost may use (spec section 6, RCP-023). Inventory also
    offers `EFFECTIVE_DATE`, which answers a management question with a movement
    set that is not a prefix of the posting order; a cost snapshot taken that way
    could not be reproduced from a sequence and would disagree with itself the
    next time somebody keyed in a late delivery.

    Stored as a column rather than assumed, so a reader never has to trust that
    the code which wrote a two-year-old row meant what today's code means.
    """

    POSTED_AS_OF = "POSTED_AS_OF", _("حسب تاريخ الترحيل")


class CostLineSource(models.TextChoices):
    """Whether a snapshot line came from the version itself or from a child."""

    DIRECT = "DIRECT", _("سطر مباشر")
    COMPONENT = "COMPONENT", _("وصفة فرعية")


class ServingAllocationOutcome(models.TextChoices):
    """
    Whether a serving scenario divides the output into whole servings at all.

    There is deliberately no "too large" value. Size never decides whether the
    business calculation happens: the allocation is analytic, so fifty thousand
    portions are the same arithmetic as ten.
    """

    ALLOCATED = "ALLOCATED", _("موزّعة")
    #: The serving is larger than the whole output. A real state, not a refusal.
    NO_WHOLE_SERVING = "NO_WHOLE_SERVING", _("لا تكفي لحصة كاملة")


class RecipeCostSnapshot(models.Model):
    """
    One costing decision, frozen: this version, this warehouse, this date.

    **Append-only.** No update, no delete, no archive flag that hides it - a
    trigger refuses both verbs for everyone including a superuser at a psql
    prompt, exactly as `core_auditevent` does. A snapshot exists so that "we
    priced the mandi off March costs" stays checkable, and a snapshot that could
    be edited would answer that question with whatever somebody preferred later
    (RCP-025).

    **Not a `TimeStampedModel`**, deliberately: `updated_at` would be a column
    that can never change, and a column that lies about being mutable invites
    somebody to try. `StockMovement` is shaped the same way for the same reason.

    Corrections are new snapshots, never edits. Two snapshots of one version,
    warehouse and date are legitimate and expected - a menu is repriced more
    than once - so uniqueness is on the **idempotency key**, which distinguishes
    a retry from a second decision, and not on the costing inputs.

    `ledger_cutoff_sequence` is what makes the row reproducible: given the
    organization and that integer, the exact positions this snapshot read can be
    re-derived years later, whatever has posted since. Comparing a snapshot
    against *today's* inventory and calling the difference an error would be
    comparing two different questions; later postings are expected.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="recipe_cost_snapshots",
        verbose_name=_("organization"),
    )
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.PROTECT,
        related_name="cost_snapshots",
        verbose_name=_("recipe"),
    )
    #: The **exact** version costed. Never re-resolved, never re-pointed.
    version = models.ForeignKey(
        RecipeVersion,
        on_delete=models.PROTECT,
        related_name="cost_snapshots",
        verbose_name=_("version"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="recipe_cost_snapshots",
        verbose_name=_("branch"),
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        related_name="recipe_cost_snapshots",
        verbose_name=_("warehouse"),
    )

    as_of_date = models.DateField(_("as of date"))
    valuation_mode = models.CharField(
        _("valuation mode"),
        max_length=16,
        choices=CostValuationMode.choices,
        default=CostValuationMode.POSTED_AS_OF,
    )
    #: The organization's posted-sequence high-water mark this card was read
    #: against. Zero is meaningful: nothing had been posted by that date.
    ledger_cutoff_sequence = models.BigIntegerField(_("ledger cutoff sequence"))
    calculation_version = models.CharField(_("calculation version"), max_length=32)

    #: Always true. Stored anyway, so the column that says "this is a record" is
    #: visible in the row rather than implied by the absence of a preview table,
    #: and so a future non-authoritative persisted card could never be mistaken
    #: for this one. A check constraint holds the line.
    is_authoritative = models.BooleanField(_("authoritative"), default=True)
    #: The version's status **at the moment of the snapshot**. A version active
    #: in March is superseded by September, and a reader needs to know which it
    #: was when the decision was taken.
    version_status = models.CharField(_("version status"), max_length=12)
    version_number = models.PositiveIntegerField(_("version number"))
    recipe_code = models.CharField(_("recipe code"), max_length=32)
    recipe_name = models.CharField(_("recipe name"), max_length=200)
    warehouse_code = models.CharField(_("warehouse code"), max_length=32)

    output_quantity = models.DecimalField(
        _("output quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    output_unit_code = models.CharField(_("output unit"), max_length=16)

    #: The `KM-RCP-004` summary, in the workbook's own three parts. Their sum is
    #: held equal to the total by a check constraint rather than by a service:
    #: an equality the database can see is one no future code path can break.
    food_total = models.DecimalField(
        _("food cost"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    packaging_total = models.DecimalField(
        _("packaging cost"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    accompaniment_total = models.DecimalField(
        _("accompaniment cost"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    total_material_cost = models.DecimalField(
        _("total material cost"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    #: A rate, not a posted amount - six places (RCP-086, ADR-012).
    cost_per_output_unit = models.DecimalField(
        _("cost per output unit"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
    )

    #: The plate-cost basis, frozen. `portions_per_batch` is the version's
    #: expected output divided by the **primary** serving's quantity, and
    #: `plate_cost` is the total on that serving's frozen share of the basis.
    #:
    #: Stored rather than derived on read for the reason every other column
    #: here is: the serving row this came from may be renamed, its recipe
    #: archived, its version superseded, and the decision still has to be
    #: explicable. The serving's own identity, quantity, unit and factor are
    #: kept on `RecipeCostSnapshotServing`, so nothing about this figure
    #: depends on a mutable current name.
    portions_per_batch = models.DecimalField(
        _("portions per batch"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    plate_cost = models.DecimalField(
        _("plate cost"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
    )
    #: Which serving was the basis, by its own stable public id. The joinable
    #: row is `servings.filter(is_primary=True)`; this is the direct answer.
    primary_serving_code = models.CharField(_("primary serving"), max_length=32)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recipe_cost_snapshots",
        null=True,
        blank=True,
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    reason = models.TextField(_("reason"), blank=True)
    reference = models.CharField(_("reference"), max_length=120, blank=True)
    note = models.TextField(_("note"), blank=True)

    #: Unique **per organization** and matched against a fingerprint, never on
    #: the key alone (`CLAUDE.md`). A key match with a different request is a
    #: conflict, not a retry.
    idempotency_key = models.CharField(_("idempotency key"), max_length=128)
    request_fingerprint = models.CharField(_("request fingerprint"), max_length=64)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("recipe cost snapshot")
        verbose_name_plural = _("recipe cost snapshots")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                name="recipe_cost_snapshot_key_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key=""),
                name="recipe_cost_snapshot_key_not_empty",
            ),
            models.CheckConstraint(
                condition=~Q(request_fingerprint=""),
                name="recipe_cost_snapshot_fingerprint_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(is_authoritative=True),
                name="recipe_cost_snapshot_is_authoritative",
            ),
            # The workbook's own summary arithmetic, as a database fact.
            models.CheckConstraint(
                condition=Q(
                    total_material_cost=models.F("food_total")
                    + models.F("packaging_total")
                    + models.F("accompaniment_total")
                ),
                name="recipe_cost_snapshot_class_totals_agree",
            ),
            models.CheckConstraint(
                condition=Q(total_material_cost__gte=Decimal("0")),
                name="recipe_cost_snapshot_total_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(output_quantity__gt=Decimal("0")),
                name="recipe_cost_snapshot_output_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(portions_per_batch__gt=Decimal("0")),
                name="recipe_cost_snapshot_portions_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(plate_cost__gte=Decimal("0")),
                name="recipe_cost_snapshot_plate_cost_not_negative",
            ),
            models.CheckConstraint(
                condition=~Q(primary_serving_code=""),
                name="recipe_cost_snapshot_names_its_plate_basis",
            ),
            models.CheckConstraint(
                condition=Q(ledger_cutoff_sequence__gte=0),
                name="recipe_cost_snapshot_cutoff_not_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "as_of_date"], name="cost_snapshot_org_date_idx"),
            models.Index(fields=["version", "warehouse"], name="cost_snapshot_version_wh_idx"),
        ]

    def __str__(self) -> str:
        return (
            f"{self.recipe_code} v{self.version_number} @ {self.warehouse_code} {self.as_of_date}"
        )

    @property
    def output_quantity_display(self) -> str:
        return f"{self.output_quantity:f}"

    @property
    def cost_per_output_unit_display(self) -> str:
        """A rate. Six places, LTR, and never a localised comma."""
        return f"{self.cost_per_output_unit:f}"

    @property
    def plate_cost_display(self) -> str:
        """A rate. Six places, LTR, and never a localised comma."""
        return f"{self.plate_cost:f}"

    @property
    def portions_per_batch_display(self) -> str:
        return f"{self.portions_per_batch:f}"


class RecipeCostSnapshotLine(models.Model):
    """
    One economic path, frozen with the identities needed to read it later.

    Every foreign key is `PROTECT` **and** shadowed by denormalised text. The
    key keeps the row joinable; the text keeps it explicable after an item is
    renamed. Neither alone is enough: an id with no text is unreadable in a
    printed report, and text with no id cannot be traced back.

    The same item on two different paths is **two rows**, deliberately. A cost
    card exists to be traced, and collapsing "the dish's cardamom" into "the
    blend's cardamom" would hide which one to fix.
    """

    snapshot = models.ForeignKey(
        RecipeCostSnapshot,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("snapshot"),
    )
    #: 1..n in the card's own deterministic order: component path, then leaf
    #: line order, then item code. Never primary-key order.
    line_number = models.PositiveIntegerField(_("line number"))
    #: `2.1`, or empty for a line the costed version owns itself.
    component_path = models.CharField(_("component path"), max_length=64, blank=True)
    source_kind = models.CharField(_("source"), max_length=12, choices=CostLineSource.choices)

    source_version = models.ForeignKey(
        RecipeVersion,
        on_delete=models.PROTECT,
        related_name="cost_snapshot_lines",
        verbose_name=_("source version"),
    )
    source_version_number = models.PositiveIntegerField(_("source version number"))
    source_recipe_code = models.CharField(_("source recipe code"), max_length=32)
    source_version_public_id = models.UUIDField(_("source version public id"))

    recipe_line = models.ForeignKey(
        RecipeLine,
        on_delete=models.PROTECT,
        related_name="cost_snapshot_lines",
        verbose_name=_("recipe line"),
    )
    recipe_line_public_id = models.UUIDField(_("recipe line public id"))
    recipe_line_order = models.PositiveIntegerField(_("recipe line order"))

    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="recipe_cost_snapshot_lines",
        verbose_name=_("item"),
    )
    item_code = models.CharField(_("item code"), max_length=32)
    item_name = models.CharField(_("item name"), max_length=200)
    item_unit_code = models.CharField(_("item unit"), max_length=16)
    cost_class = models.CharField(
        _("cost class"), max_length=16, choices=RecipeLineCostClass.choices
    )

    #: The product of every multiplier from the root, at full precision and
    #: never rounded on the way down (RCP-073).
    cumulative_multiplier = models.DecimalField(
        _("cumulative multiplier"),
        max_digits=EXTENSION_MAX_DIGITS,
        decimal_places=FACTOR_PLACES,
    )
    effective_quantity = models.DecimalField(
        _("effective quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )

    #: What inventory actually held, so a reader can see the average was
    #: `value / quantity` across the lots and not an average of averages.
    valuation_quantity = models.DecimalField(
        _("valuation quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
    )
    valuation_value = models.DecimalField(
        _("valuation value"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    valuation_lot_count = models.PositiveIntegerField(_("lots"), default=0)
    unit_cost = models.DecimalField(
        _("unit cost"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
    )
    #: `effective_quantity x unit_cost`, exactly - twelve places is enough to
    #: hold the product of two six-place figures with nothing lost.
    raw_extension = models.DecimalField(
        _("raw extension"),
        max_digits=EXTENSION_MAX_DIGITS,
        decimal_places=FACTOR_PLACES,
    )
    #: This line's share of the **rounded** document total. Their sum is the
    #: total to the fils, because the residue was distributed and not dropped.
    allocated_extension = models.DecimalField(
        _("allocated extension"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)

    class Meta:
        verbose_name = _("recipe cost snapshot line")
        verbose_name_plural = _("recipe cost snapshot lines")
        ordering = ["snapshot", "line_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "line_number"],
                name="cost_snapshot_line_number_unique_per_snapshot",
            ),
            models.CheckConstraint(
                condition=Q(line_number__gte=1), name="cost_snapshot_line_number_is_positive"
            ),
            models.CheckConstraint(
                condition=Q(effective_quantity__gt=Decimal("0")),
                name="cost_snapshot_line_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_cost__gte=Decimal("0")),
                name="cost_snapshot_line_unit_cost_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(allocated_extension__gte=Decimal("0")),
                name="cost_snapshot_line_allocation_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.snapshot_id}#{self.line_number} {self.item_code}"

    @property
    def quantity_display(self) -> str:
        """
        The stored effective quantity, LTR with a period.

        Django localises a Decimal, so under Arabic this would render
        `1,500000`. A comma in a re-enterable quantity is ambiguous and invites
        a mis-typed re-entry (`CLAUDE.md`), and a snapshot exists precisely to
        be re-read years later by somebody checking a figure.
        """
        return f"{self.effective_quantity:f}"

    @property
    def unit_cost_display(self) -> str:
        return f"{self.unit_cost:f}"

    @property
    def multiplier_display(self) -> str:
        return f"{self.cumulative_multiplier.normalize():f}"

    @property
    def valuation_quantity_display(self) -> str:
        """
        The inventory quantity behind the unit cost, LTR with a period.

        `{{ value }}` renders `265,000` under Arabic for a stored `265.000`,
        which reads as two hundred sixty-five thousand beside a money column
        that is genuinely grouped that way. A quantity is a re-enterable
        technical value and never carries a locale separator (`CLAUDE.md`).
        """
        return f"{self.valuation_quantity:f}"


class RecipeCostSnapshotServing(models.Model):
    """
    One way of portioning this output, costed two ways because two were asked.

    `cost_per_serving` is the RCP-086 **rate** - the total times the serving's
    share of the output basis, quantized once to six places because it is a unit
    cost. `allocated_total` is the RCP-087 **allocation** - the exact total
    divided across the whole servings the output makes plus whatever output is
    left over, summing to the recipe total to the fils.

    Serving definitions are alternatives, never simultaneous: each row allocates
    the **whole** total, and adding two of them together would double the
    recipe. `minimum_allocated` and `maximum_allocated` differ by at most one
    fils and that difference *is* the remainder distribution, exposed rather
    than hidden.
    """

    snapshot = models.ForeignKey(
        RecipeCostSnapshot,
        on_delete=models.CASCADE,
        related_name="servings",
        verbose_name=_("snapshot"),
    )
    display_order = models.PositiveIntegerField(_("display order"))
    serving = models.ForeignKey(
        RecipeServing,
        on_delete=models.PROTECT,
        related_name="cost_snapshot_servings",
        verbose_name=_("serving"),
    )
    serving_public_id = models.UUIDField(_("serving public id"))
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)
    is_primary = models.BooleanField(_("primary"), default=False)

    serving_quantity = models.DecimalField(
        _("serving quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    serving_unit_code = models.CharField(_("serving unit"), max_length=16)
    base_quantity = models.DecimalField(
        _("serving quantity in output unit"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    factor_of_batch = models.DecimalField(
        _("factor of batch"), max_digits=FACTOR_MAX_DIGITS, decimal_places=FACTOR_PLACES
    )

    whole_serving_count = models.PositiveIntegerField(_("whole servings"))
    remainder_quantity = models.DecimalField(
        _("remainder output"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )

    cost_per_serving = models.DecimalField(
        _("cost per serving"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
    )
    allocation_state = models.CharField(
        _("allocation"),
        max_length=16,
        choices=ServingAllocationOutcome.choices,
        default=ServingAllocationOutcome.ALLOCATED,
    )
    allocated_total = models.DecimalField(
        _("allocated total"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )

    #: The whole distribution, in four numbers plus the leftover.
    #:
    #: Every whole serving carries equal weight, so the certified allocator can
    #: only produce two amounts: a floor, and that floor plus one fils for the
    #: servings the residue reaches. Recording both amounts and both counts is
    #: therefore the distribution itself rather than a summary of it — the
    #: per-serving list is reconstructible and adds nothing. It is also what
    #: lets a fifty-thousand-portion scenario be stored in five columns instead
    #: of fifty thousand rows.
    #:
    #: The identity a reader can check on the page, and the verifier does:
    #:
    #:     normal_count x normal_amount
    #:   + elevated_count x elevated_amount
    #:   + remainder_cost
    #:   = allocated_total = the snapshot total
    minimum_allocated = models.DecimalField(
        _("cost per serving (normal)"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
    )
    maximum_allocated = models.DecimalField(
        _("cost per serving (elevated)"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
    )
    normal_serving_count = models.PositiveIntegerField(_("servings at normal cost"), default=0)
    elevated_serving_count = models.PositiveIntegerField(_("servings at elevated cost"), default=0)
    remainder_cost = models.DecimalField(
        _("leftover output cost"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)

    class Meta:
        verbose_name = _("recipe cost snapshot serving")
        verbose_name_plural = _("recipe cost snapshot servings")
        ordering = ["snapshot", "display_order", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "serving"],
                name="cost_snapshot_serving_unique_per_snapshot",
            ),
            models.CheckConstraint(
                condition=Q(remainder_quantity__gte=Decimal("0")),
                name="cost_snapshot_serving_remainder_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(cost_per_serving__gte=Decimal("0")),
                name="cost_snapshot_serving_cost_not_negative",
            ),
            # The two counts must add up to the servings the output makes. A
            # database fact rather than a service assertion, because a summary
            # that stopped describing its own count would be unverifiable.
            models.CheckConstraint(
                condition=Q(
                    whole_serving_count=models.F("normal_serving_count")
                    + models.F("elevated_serving_count")
                ),
                name="cost_snapshot_serving_counts_add_up",
            ),
            models.CheckConstraint(
                condition=Q(maximum_allocated__gte=models.F("minimum_allocated")),
                name="cost_snapshot_serving_elevated_is_not_less",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.snapshot_id} · {self.code}"

    @property
    def factor_display(self) -> str:
        quantum = Decimal(1).scaleb(-FACTOR_PLACES)
        return f"{self.factor_of_batch.quantize(quantum):f}"

    @property
    def cost_per_serving_display(self) -> str:
        return f"{self.cost_per_serving:f}"

    @property
    def remainder_display(self) -> str:
        return f"{self.remainder_quantity:f}"

    def reconstructs_to(self) -> Decimal:
        """
        The distribution added back up, from the stored summary alone.

        What the verifier checks. If this stops equalling `allocated_total`,
        the five columns have stopped describing the allocation they claim to.
        """
        from apps.core.money import quantize_money

        return quantize_money(
            Decimal(self.normal_serving_count) * self.minimum_allocated
            + Decimal(self.elevated_serving_count) * self.maximum_allocated
            + self.remainder_cost
        )


# ---------------------------------------------------------------------------
# Task 3.4 - production batch drafting
# ---------------------------------------------------------------------------
#
# A recipe is an intention; the production batch is the event (RCP-002). Task
# 3.4 builds the **draft** of that event and nothing else: what the kitchen
# intends to consume, flattened from the exact recipe version, and what it
# actually consumed. Task 3.5 turns a draft into a posting.
#
# The split is not squeamishness. Drafting touches no ledger, so it can ship
# with screens and tests while posting waits on KD-09; and the database says so
# rather than the docstring - `production_batch_is_draft_only_until_task_3_5`
# refuses a POSTED row outright, so no service, migration, admin action or psql
# prompt can produce one before Task 3.5 removes that constraint deliberately.


class KitchenDocumentSequence(models.Model):
    """
    The gapless per-organization, per-type, per-year counter.

    A third sequence table beside inventory's and procurement's, and for the
    same reason procurement declared the second: `PRODUCTION_BATCH` is not an
    inventory document type, and keying it into `InventoryDocumentType` would
    make that enum a statement about documents inventory does not own. The
    counting *rule* is four lines under a row lock and is deliberately
    identical — what must never be duplicated is a sequence one document could
    draw from twice.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="kitchen_sequences",
        verbose_name=_("organization"),
    )
    document_type = models.CharField(_("document type"), max_length=32)
    year = models.PositiveSmallIntegerField(_("year"))
    last_number = models.PositiveIntegerField(_("last number"), default=0)

    class Meta:
        verbose_name = _("kitchen document sequence")
        verbose_name_plural = _("kitchen document sequences")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "document_type", "year"],
                name="kitchen_sequence_unique_per_type_and_year",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.code} {self.document_type} {self.year}: {self.last_number}"


class ProductionBatchStatus(models.TextChoices):
    """
    The approved lifecycle of one batch (spec section 7).

    Three states and no fourth. There is deliberately no `IN_PROGRESS`, no
    partial completion and no multi-day work in progress: a Release 1 batch
    posts atomically on one business date into one warehouse or stays a draft
    (RCP-094). Adding a fourth value would need §8A superseded first, because
    the absence of a WIP account is true only under those conditions.

    Task 3.4 declared all three and made only `DRAFT` reachable, through a
    check constraint named after the task that had to remove it. Task 3.5
    removed it in migration 0017 and put the posting-evidence constraints in
    its place — a batch is now a draft with none of the evidence or a posted
    one with all of it.
    """

    DRAFT = "DRAFT", _("مسودة")
    POSTED = "POSTED", _("مرحّلة")
    REVERSED = "REVERSED", _("معكوسة")


class ActualLineKind(models.TextChoices):
    """Whether an actual row records the planned item or an approved stand-in."""

    PRIMARY = "PRIMARY", _("الصنف الأصلي")
    SUBSTITUTE = "SUBSTITUTE", _("بديل معتمد")


class ProductionBatch(TimeStampedModel):
    """
    One intended production run, drafted from one exact `RecipeVersion`.

    **One organization, one branch, one warehouse** (RCP-029). Ingredients
    leave that warehouse and the output enters it; producing "into" another
    warehouse is a batch plus a Phase 1 transfer, because two things happened.

    **The version is chosen once and frozen.** Creation names a recipe, a
    branch and an explicit planned business date, and `resolve_recipe_version`
    answers which structure was in force. That answer is stored, and nothing
    re-resolves it afterwards - not when a newer version is activated, not when
    the child is superseded, not when the batch is reopened. A wrong date is
    corrected by discarding the draft and creating another, because silently
    re-pointing a batch would change what a half-finished document claims the
    kitchen is making (RCP-011 one level down).

    **What is frozen and what is not** is the whole design of this table. The
    organization, branch, warehouse, recipe, version and planned date are
    facts about the *decision*; the actual quantities are facts about *reality*
    and stay editable while the batch is a draft. `output_quantity` is entered
    by the operator and never derived from the inputs (RCP-031) - the scale
    decides, exactly as a variable-package receipt's measured weight does.

    The **multiplier** sits in neither group and is worth stating plainly,
    because it is the field most likely to be mistaken for frozen: how much of a
    recipe to make is revisable while the batch is a draft, through
    `rescale_production_batch` and through nothing else. Migration 0015 holds it,
    `expected_output_quantity` and every requirement's `planned_base_quantity`
    in agreement at COMMIT, so revisable does not become independently mutable -
    a batch claiming to be double the recipe while asking for one and a half
    times the rice is refused by the database, not merely unlikely.

    **Posting ends all of it** (Task 3.5). `post_production_batch` consumes
    every positive actual row through `PRODUCTION_OUT`, creates the output
    through `PRODUCTION_IN` at exactly the sum of the consumed values, draws the
    gapless number, writes the journal only if the per-account nets need one,
    and freezes the whole aggregate. From `POSTED` the only permitted change is
    a reversal; migration 0018's trigger enforces that against every writer,
    including a superuser at a psql prompt.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="production_batches",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="production_batches",
        verbose_name=_("branch"),
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        related_name="production_batches",
        verbose_name=_("warehouse"),
    )

    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.PROTECT,
        related_name="production_batches",
        verbose_name=_("recipe"),
    )
    #: The **exact** version, resolved once at creation and never again.
    recipe_version = models.ForeignKey(
        RecipeVersion,
        on_delete=models.PROTECT,
        related_name="production_batches",
        verbose_name=_("recipe version"),
    )
    #: The operational date this batch belongs to, and the date its version was
    #: resolved for. Explicit, never defaulted to today: a batch drafted on
    #: Monday for Sunday's production must use Sunday's recipe.
    planned_business_date = models.DateField(_("planned business date"))

    #: How many `batch_size`s this run is. Decimal because half a pit is a real
    #: thing, and positive because zero production is not a production.
    multiplier = models.DecimalField(
        _("multiplier"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )

    #: `expected_output_quantity x multiplier`, snapshotted so the comparison
    #: on the screen survives a version being superseded.
    expected_output_quantity = models.DecimalField(
        _("expected output"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    expected_output_unit_code = models.CharField(_("expected output unit"), max_length=16)

    #: What the scale said. Entered, not derived (RCP-031).
    actual_output_entered_quantity = models.DecimalField(
        _("actual output entered"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
        null=True,
        blank=True,
    )
    actual_output_unit = models.ForeignKey(
        "units.UnitOfMeasure",
        on_delete=models.PROTECT,
        related_name="production_batch_outputs",
        null=True,
        blank=True,
        verbose_name=_("actual output unit"),
    )
    actual_output_base_quantity = models.DecimalField(
        _("actual output"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
        null=True,
        blank=True,
    )

    status = models.CharField(
        _("status"),
        max_length=12,
        choices=ProductionBatchStatus.choices,
        default=ProductionBatchStatus.DRAFT,
    )
    #: Drawn at posting, gapless per organization. Empty on a draft, and a
    #: constraint holds it empty there: a number consumed by a posting that
    #: failed is a gap, and a gapless sequence with gaps in it is worse than an
    #: honest one.
    number = models.CharField(_("number"), max_length=32, blank=True)

    notes = models.TextField(_("notes"), blank=True)

    # -----------------------------------------------------------------------
    # Posting evidence (Task 3.5)
    #
    # Every field below is NULL on a draft and written exactly once, inside the
    # posting transaction. They are evidence rather than state: the authority
    # for what this batch did is the stock ledger entry, and these columns are
    # what let a reader reach it without reconstructing the link from movement
    # rows.
    # -----------------------------------------------------------------------
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="production_batches_posted",
        null=True,
        blank=True,
        verbose_name=_("posted by"),
    )
    stock_entry = models.ForeignKey(
        "inventory.StockLedgerEntry",
        on_delete=models.PROTECT,
        related_name="production_batches",
        null=True,
        blank=True,
        verbose_name=_("stock posting"),
    )
    #: Null on a **correctly** silent posting as well as on a draft, which is
    #: why `verify_kitchen` recomputes the per-account nets rather than reading
    #: this column: a journal that is rightly absent and one that is wrongly
    #: missing look identical here (RCP-112 proof 5).
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        related_name="production_batches",
        null=True,
        blank=True,
        verbose_name=_("journal entry"),
    )
    #: Snapshotted from `recipe.output_item` at posting. Re-reading the recipe
    #: later would let a master-data edit restate what a posted batch produced.
    output_item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="production_batch_outputs",
        null=True,
        blank=True,
        verbose_name=_("output item"),
    )
    output_lot = models.ForeignKey(
        "inventory.InventoryLot",
        on_delete=models.PROTECT,
        related_name="production_batches",
        null=True,
        blank=True,
        verbose_name=_("output lot"),
    )
    output_movement = models.ForeignKey(
        "inventory.StockMovement",
        on_delete=models.PROTECT,
        related_name="production_batch_outputs",
        null=True,
        blank=True,
        verbose_name=_("output movement"),
    )
    #: `Σ consumed input movement values`, and the output's inbound value. Two
    #: columns holding one number on purpose: value conservation (RCP-034) is
    #: the invariant this task exists to keep, and an invariant asserted
    #: against a single stored figure is not asserted at all.
    input_value = models.DecimalField(
        _("consumed value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )
    output_value = models.DecimalField(
        _("output value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )
    #: The posting command's own key, separate from the creation key above:
    #: drafting and posting are two commands, and one key matched against two
    #: fingerprints would make a retry of either look like a conflict.
    post_idempotency_key = models.CharField(_("posting key"), max_length=128, blank=True)
    post_request_fingerprint = models.CharField(_("posting fingerprint"), max_length=64, blank=True)
    posting_rule_version = models.CharField(_("posting rule version"), max_length=32, blank=True)

    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="production_batches_reversed",
        null=True,
        blank=True,
        verbose_name=_("reversed by"),
    )
    reversal_reason = models.CharField(_("reversal reason"), max_length=200, blank=True)
    reversal_stock_entry = models.ForeignKey(
        "inventory.StockLedgerEntry",
        on_delete=models.PROTECT,
        related_name="production_batch_reversals",
        null=True,
        blank=True,
        verbose_name=_("reversing stock posting"),
    )
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        related_name="production_batch_reversals",
        null=True,
        blank=True,
        verbose_name=_("reversing journal entry"),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="production_batches_created",
        null=True,
        blank=True,
        verbose_name=_("created by"),
    )

    #: Unique **per organization** and matched against a fingerprint, never on
    #: the key alone (`CLAUDE.md`).
    idempotency_key = models.CharField(_("idempotency key"), max_length=128)
    request_fingerprint = models.CharField(_("request fingerprint"), max_length=64)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("production batch")
        verbose_name_plural = _("production batches")
        ordering = ["-planned_business_date", "-id"]
        permissions = [
            ("create_production_batch", "Can draft and edit production batches"),
            ("post_production_batch", "Can post production batches"),
            ("reverse_production_batch", "Can reverse posted production batches"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                name="production_batch_key_unique_per_organization",
            ),
            # The posting command's key, unique per organization exactly as the
            # drafting one is. Partial, because a draft has none and every
            # draft would otherwise collide on the empty string.
            models.UniqueConstraint(
                fields=["organization", "post_idempotency_key"],
                condition=~Q(post_idempotency_key=""),
                name="production_batch_post_key_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key=""),
                name="production_batch_key_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(multiplier__gt=Decimal("0")),
                name="production_batch_multiplier_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(expected_output_quantity__gt=Decimal("0")),
                name="production_batch_expected_output_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(actual_output_base_quantity__isnull=True)
                | Q(actual_output_base_quantity__gte=Decimal("0")),
                name="production_batch_actual_output_not_negative",
            ),
            # ---------------------------------------------------------------
            # The Task 3.4 / 3.5 boundary was a check constraint named
            # `production_batch_is_draft_only_until_task_3_5`, and migration
            # 0017 removes it — deliberately, in its own migration, as the
            # first thing Task 3.5 does. What replaces it is not weaker: the
            # constraints below say that a batch is either a draft with **no**
            # posting evidence at all or a posted one with **all** of it, and
            # there is no third shape. The old rule refused one status; these
            # refuse every half-posted row.
            # ---------------------------------------------------------------
            # A draft carries no posting evidence. This half predates Task 3.5
            # and survives it unchanged.
            models.CheckConstraint(
                condition=Q(status=ProductionBatchStatus.DRAFT, number="")
                | ~Q(status=ProductionBatchStatus.DRAFT),
                name="production_batch_draft_has_no_number",
            ),
            # Everything a posting writes, written together or not at all. A
            # row with a stock entry and no value, or a value and no number, is
            # a posting that failed halfway and looks complete — the one shape
            # `transaction.atomic` is supposed to make impossible and the one a
            # later bulk update could still produce.
            models.CheckConstraint(
                condition=Q(
                    status=ProductionBatchStatus.DRAFT,
                    posted_at__isnull=True,
                    stock_entry__isnull=True,
                    output_value__isnull=True,
                    input_value__isnull=True,
                    output_item__isnull=True,
                    output_movement__isnull=True,
                )
                | (
                    ~Q(status=ProductionBatchStatus.DRAFT)
                    & ~Q(number="")
                    & Q(
                        posted_at__isnull=False,
                        stock_entry__isnull=False,
                        output_value__isnull=False,
                        input_value__isnull=False,
                        output_item__isnull=False,
                        output_movement__isnull=False,
                    )
                ),
                name="production_batch_posting_evidence_is_complete",
            ),
            # Value conservation, at the database. The service computes the
            # output's inbound value as the sum of the consumed movement values
            # and the kernel writes exactly that; this refuses the row where
            # the two ever disagree, so RCP-034 is a property of the schema
            # rather than of one code path.
            models.CheckConstraint(
                condition=Q(input_value__isnull=True, output_value__isnull=True)
                | Q(input_value=F("output_value")),
                name="production_batch_conserves_value",
            ),
            # A reversal names who did it, when, and why — all three or none.
            models.CheckConstraint(
                condition=Q(
                    status=ProductionBatchStatus.REVERSED,
                    reversed_at__isnull=False,
                    reversal_stock_entry__isnull=False,
                )
                & ~Q(reversal_reason="")
                | ~Q(status=ProductionBatchStatus.REVERSED)
                & Q(
                    reversed_at__isnull=True,
                    reversal_stock_entry__isnull=True,
                    reversal_reason="",
                ),
                name="production_batch_reversal_evidence_is_complete",
            ),
            # A posting key belongs to a posting. A draft holding one would be
            # a key consumed by a command that never ran.
            models.CheckConstraint(
                condition=Q(status=ProductionBatchStatus.DRAFT, post_idempotency_key="")
                | ~Q(status=ProductionBatchStatus.DRAFT) & ~Q(post_idempotency_key=""),
                name="production_batch_posting_key_belongs_to_a_posting",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "planned_business_date"],
                name="production_batch_org_date_idx",
            ),
            models.Index(fields=["warehouse", "status"], name="production_batch_wh_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.recipe_id} x{self.multiplier} @ {self.planned_business_date}"

    @property
    def is_draft(self) -> bool:
        return self.status == ProductionBatchStatus.DRAFT

    @property
    def multiplier_display(self) -> str:
        """A technical identity: period, never a localised comma."""
        return f"{self.multiplier:f}"

    @property
    def expected_output_display(self) -> str:
        return f"{self.expected_output_quantity:f}"

    @property
    def actual_output_display(self) -> str:
        if self.actual_output_base_quantity is None:
            return ""
        return f"{self.actual_output_base_quantity:f}"


class ProductionBatchLine(TimeStampedModel):
    """
    One flattened requirement: one economic path from the version to one item.

    Written once, at creation, from `apps/kitchen/expansion.py`. **Every source
    field here is immutable from that moment**, draft or not, and a trigger
    enforces it: these are not the operator's facts to correct. They are what
    the recipe said, and correcting them would mean the batch quietly claims to
    have been drafted from a structure it was not.

    What *is* editable lives on `ProductionBatchActualLine` beside it. The plan
    and reality are different rows on purpose (RCP-030): an operator who
    consumed something else has recorded a fact, not amended a recipe.

    The same `InventoryItem` reached by two paths stays **two** requirement
    rows. Aggregating them would save a line and lose the ability to say which
    level of the tree the variance came from, which is the batch variance
    report's entire subject.

    A stocked semi-finished item is one row and is never expanded - its book
    value already contains its ingredients (RCP-071).
    """

    batch = models.ForeignKey(
        ProductionBatch,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("batch"),
    )
    #: 1..n in the expansion's own deterministic order: component path, then
    #: leaf line order, then item code. Never primary-key order.
    line_order = models.PositiveIntegerField(_("line order"))

    #: The **exact** version this requirement came from - the root's own for a
    #: direct line, the frozen `component_version` for a nested one.
    source_version = models.ForeignKey(
        RecipeVersion,
        on_delete=models.PROTECT,
        related_name="production_batch_lines",
        verbose_name=_("source version"),
    )
    source_line = models.ForeignKey(
        RecipeLine,
        on_delete=models.PROTECT,
        related_name="production_batch_lines",
        verbose_name=_("source recipe line"),
    )
    source_line_public_id = models.UUIDField(_("source line public id"))
    source_version_public_id = models.UUIDField(_("source version public id"))

    #: `2.1` - the component `line_order` path from the root. Empty for a line
    #: the batch's own version owns (RCP-080).
    component_path = models.CharField(_("component path"), max_length=64, blank=True)
    #: `DISH v2 ← BLEND v1 ← SPICE v1`, so a two-year-old batch reconstructs its
    #: exact tree without consulting versions that may since have been
    #: superseded (RCP-080).
    component_label_path = models.CharField(_("component label path"), max_length=400)
    source_kind = models.CharField(_("source"), max_length=12, choices=CostLineSource.choices)

    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="production_batch_lines",
        verbose_name=_("item"),
    )
    item_code = models.CharField(_("item code"), max_length=32)
    item_name = models.CharField(_("item name"), max_length=200)
    base_unit_code = models.CharField(_("base unit"), max_length=16)

    #: What **one batch of the leaf's own recipe** consumes. Kept so a rescale
    #: recomputes from the recipe's own figure rather than from a previously
    #: scaled result, which would compound rounding every time.
    source_base_quantity = models.DecimalField(
        _("source quantity per recipe batch"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    #: The product of every component multiplier from the root, at full
    #: precision and never rounded on the way down (RCP-073).
    cumulative_multiplier = models.DecimalField(
        _("cumulative multiplier"),
        max_digits=EXTENSION_MAX_DIGITS,
        decimal_places=FACTOR_PLACES,
    )
    #: `source x cumulative x batch multiplier`, quantized **once**, here.
    planned_base_quantity = models.DecimalField(
        _("planned quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )

    cost_class = models.CharField(
        _("cost class"), max_length=16, choices=RecipeLineCostClass.choices
    )
    is_optional = models.BooleanField(_("optional"), default=False)
    preparation_stage = models.CharField(_("stage"), max_length=12, blank=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("production batch line")
        verbose_name_plural = _("production batch lines")
        ordering = ["batch", "line_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "line_order"],
                name="production_line_order_unique_per_batch",
            ),
            # One requirement per economic path. Two rows claiming the same
            # path would make the variance report ambiguous about which one
            # the kitchen actually consumed against.
            models.UniqueConstraint(
                fields=["batch", "component_path", "source_line"],
                name="production_line_path_unique_per_batch",
            ),
            models.CheckConstraint(
                condition=Q(line_order__gte=1), name="production_line_order_is_positive"
            ),
            models.CheckConstraint(
                condition=Q(source_base_quantity__gt=Decimal("0")),
                name="production_line_source_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(planned_base_quantity__gt=Decimal("0")),
                name="production_line_planned_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(cumulative_multiplier__gt=Decimal("0")),
                name="production_line_multiplier_is_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.batch_id}#{self.line_order} {self.item_code}"

    @property
    def planned_display(self) -> str:
        return f"{self.planned_base_quantity:f}"

    @property
    def multiplier_display(self) -> str:
        return f"{self.cumulative_multiplier.normalize():f}"

    @property
    def source_display(self) -> str:
        return f"{self.source_base_quantity:f}"


class ProductionBatchActualLine(TimeStampedModel):
    """
    What was actually consumed against one requirement.

    A **separate** table rather than a `consumed_quantity` column on the
    requirement, and the reason is partial substitution: 3 kg of the primary
    plus 1 kg of an approved stand-in is two facts about one requirement, and a
    single column can hold only one of them. The spec's sketch put
    `consumed_quantity` on the line; that shape cannot express the case
    RCP-022's ranked substitute table exists for, so this is normalized instead
    and the departure is recorded in spec section 28.

    Created with one row per requirement at batch creation - actual item equals
    source item, actual quantity equals plan - so the common case needs no
    typing at all and a variance is a deliberate edit rather than an omission.

    **The recipe is the plan; these rows are reality** (RCP-030). More, less,
    zero on an optional line, a split across approved substitutes: all
    permitted. A variance is the batch variance report's business, never a
    refusal - refusing would teach kitchens to falsify quantities to match the
    recipe, which is the one outcome that makes the whole module useless.

    What is *not* permitted is an item nobody approved. The actual item must be
    the requirement's own item or an active `RecipeLineSubstitute` **of that
    same source line**: a substitute approved for the rice line is not approved
    for the oil line, and a substitute from another organization is not
    approved at all.
    """

    line = models.ForeignKey(
        ProductionBatchLine,
        on_delete=models.CASCADE,
        related_name="actuals",
        verbose_name=_("requirement"),
    )
    entry_order = models.PositiveIntegerField(_("entry order"), default=1)

    kind = models.CharField(
        _("kind"), max_length=12, choices=ActualLineKind.choices, default=ActualLineKind.PRIMARY
    )
    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="production_actual_lines",
        verbose_name=_("actual item"),
    )
    #: The approval this stand-in rests on. Null for a primary row, and a
    #: constraint holds the two consistent so a substitute row cannot lose the
    #: record of what made it acceptable.
    substitute = models.ForeignKey(
        RecipeLineSubstitute,
        on_delete=models.PROTECT,
        related_name="production_actual_lines",
        null=True,
        blank=True,
        verbose_name=_("approved substitute"),
    )

    entered_quantity = models.DecimalField(
        _("entered quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )
    entered_unit = models.ForeignKey(
        "units.UnitOfMeasure",
        on_delete=models.PROTECT,
        related_name="production_actual_lines",
        null=True,
        blank=True,
        verbose_name=_("entered unit"),
    )
    package_unit = models.ForeignKey(
        "inventory.PackageUnit",
        on_delete=models.PROTECT,
        related_name="production_actual_lines",
        null=True,
        blank=True,
        verbose_name=_("package"),
    )
    #: **How many base units one entered unit is**, snapshotted at entry.
    #:
    #: Always present, and always a fact rather than a convenience: `1` for a
    #: base-unit entry, `0.001` for grams against a KG item, the package factor
    #: for a package. Uniform because a snapshot that is sometimes null is a
    #: snapshot a reader has to interpret, and because readiness can then ask
    #: one question — is the conversion complete — instead of three.
    #:
    #: For a package it also freezes the sack size, so correcting that size next
    #: year cannot restate what this batch recorded consuming.
    conversion_factor = models.DecimalField(
        _("conversion factor"),
        max_digits=FACTOR_MAX_DIGITS,
        decimal_places=FACTOR_PLACES,
        null=True,
        blank=True,
    )
    conversion_version = models.PositiveIntegerField(_("conversion version"), null=True, blank=True)
    #: A VARIABLE package has no arithmetic answer - one meat container is
    #: whatever it weighed - so the caller measures it, exactly as a
    #: variable-weight receipt does.
    measured_base_quantity = models.DecimalField(
        _("measured base quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
        null=True,
        blank=True,
    )
    base_quantity = models.DecimalField(
        _("actual quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )

    reason = models.CharField(_("substitution reason"), max_length=200, blank=True)
    note = models.TextField(_("note"), blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="production_actual_lines",
        null=True,
        blank=True,
        verbose_name=_("recorded by"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("production actual line")
        verbose_name_plural = _("production actual lines")
        ordering = ["line", "entry_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["line", "entry_order"],
                name="production_actual_order_unique_per_line",
            ),
            # Stable *and* positive. `PositiveIntegerField` permits zero, and a
            # zero-ordered row sorts ahead of the generated primary row that is
            # always 1 - so an added substitute could silently become the first
            # thing an operator reads about a requirement.
            models.CheckConstraint(
                condition=Q(entry_order__gte=1),
                name="production_actual_order_is_positive",
            ),
            # One row per item per requirement. Two rows for the same item
            # would be one quantity written twice, and the second would be a
            # correction masquerading as a second consumption.
            models.UniqueConstraint(
                fields=["line", "item"], name="production_actual_item_unique_per_line"
            ),
            # Zero is legitimate - an optional line the kitchen skipped - and
            # negative is not: consuming minus two kilos is not a fact.
            models.CheckConstraint(
                condition=Q(base_quantity__gte=Decimal("0")),
                name="production_actual_quantity_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(entered_quantity__gte=Decimal("0")),
                name="production_actual_entered_not_negative",
            ),
            # A substitute row names its approval; a primary row does not.
            models.CheckConstraint(
                condition=Q(kind=ActualLineKind.PRIMARY, substitute__isnull=True)
                | Q(kind=ActualLineKind.SUBSTITUTE, substitute__isnull=False),
                name="production_actual_substitute_names_its_approval",
            ),
            # Exactly one entry mode, the same exclusivity `RecipeLine` uses.
            models.CheckConstraint(
                condition=Q(entered_unit__isnull=False, package_unit__isnull=True)
                | Q(entered_unit__isnull=True, package_unit__isnull=False),
                name="production_actual_one_entry_mode",
            ),
            # Every entry carries its factor, package or not. One-directional
            # rather than the biconditional a recipe line uses, because a
            # base-unit entry has a real factor of 1 and calling that "no
            # conversion" would leave readiness unable to tell a complete
            # snapshot from a missing one.
            models.CheckConstraint(
                condition=Q(conversion_factor__isnull=False),
                name="production_actual_carries_its_factor",
            ),
            models.CheckConstraint(
                condition=Q(conversion_factor__gt=Decimal("0")),
                name="production_actual_factor_is_positive",
            ),
            # ---------------------------------------------------------------
            # Cardinality. Each of these closes a way of recording the same
            # consumption twice, which is the shape a variance report cannot
            # survive: two rows for one item are one quantity written twice, and
            # the second is a correction masquerading as a second consumption.
            # ---------------------------------------------------------------
            models.UniqueConstraint(
                fields=["line"],
                condition=Q(kind=ActualLineKind.PRIMARY),
                name="production_actual_one_primary_per_line",
            ),
            models.UniqueConstraint(
                fields=["line", "substitute"],
                condition=Q(substitute__isnull=False),
                name="production_actual_one_row_per_substitute",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.line_id}#{self.entry_order} {self.item_id}"

    @property
    def quantity_display(self) -> str:
        return f"{self.base_quantity:f}"

    @property
    def entered_display(self) -> str:
        return f"{self.entered_quantity:f}"

    @property
    def factor_display(self) -> str:
        if self.conversion_factor is None:
            return ""
        quantum = Decimal(1).scaleb(-FACTOR_PLACES)
        return f"{self.conversion_factor.quantize(quantum):f}"

    @property
    def is_substitute(self) -> bool:
        return self.kind == ActualLineKind.SUBSTITUTE


class ProductionBatchAllocation(TimeStampedModel):
    """
    Which lot, and out of which bin, one actual row's quantity came from.

    A fourth table rather than three nullable columns on the actual row,
    because the cardinality is genuinely one-to-many: 4 kg of rice may be two
    kilos from the lot that expires on Friday and two from the one that expires
    next month, and a kitchen that had to record it as one lot would record the
    wrong one. Lot-tracked stock is exactly where "roughly which batch" is not
    an acceptable answer — it is what a recall traces.

    **Drafted before posting, frozen after it.** The rows are the operator's
    plan while the batch is a draft; posting validates them against real
    availability, posts one `PRODUCTION_OUT` movement per row, and writes back
    the movement and the value the kernel took. After that they are history.

    **Optional where the item permits it.** An item that tracks no lots and
    sits in a warehouse with no bins needs no allocation row: the posting
    derives one lot-less, location-less effect from the actual row itself.
    Requiring an empty formality there would be a form to fill in for the sake
    of the schema. A lot-tracked item is the opposite case and posting refuses
    without exact allocation (RCP-038's neighbour: you cannot produce from a
    lot you did not name).
    """

    actual = models.ForeignKey(
        ProductionBatchActualLine,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name=_("actual line"),
    )
    #: 1..n, stable, and the order the effects are built in — so two postings
    #: of the same shape produce the same movement sequence.
    allocation_order = models.PositiveIntegerField(_("allocation order"))

    lot = models.ForeignKey(
        "inventory.InventoryLot",
        on_delete=models.PROTECT,
        related_name="production_allocations",
        null=True,
        blank=True,
        verbose_name=_("lot"),
    )
    location = models.ForeignKey(
        "inventory.StockLocation",
        on_delete=models.PROTECT,
        related_name="production_allocations",
        null=True,
        blank=True,
        verbose_name=_("location"),
    )
    base_quantity = models.DecimalField(
        _("quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=CALCULATION_PLACES,
    )

    #: Written at posting, once. Null while the batch is a draft.
    movement = models.ForeignKey(
        "inventory.StockMovement",
        on_delete=models.PROTECT,
        related_name="production_allocations",
        null=True,
        blank=True,
        verbose_name=_("stock movement"),
    )
    #: What the kernel's moving average actually charged. Not
    #: `quantity x average` recomputed later — the exact figure the movement
    #: carries, which is what value conservation is summed from.
    consumed_value = models.DecimalField(
        _("consumed value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("production allocation")
        verbose_name_plural = _("production allocations")
        ordering = ["actual", "allocation_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["actual", "allocation_order"],
                name="production_allocation_order_unique_per_actual",
            ),
            models.CheckConstraint(
                condition=Q(allocation_order__gte=1),
                name="production_allocation_order_is_positive",
            ),
            # Zero is not an allocation. An actual row that consumed nothing
            # simply has no allocation rows.
            models.CheckConstraint(
                condition=Q(base_quantity__gt=Decimal("0")),
                name="production_allocation_quantity_is_positive",
            ),
            # One row per (lot, location) per actual row. Two rows naming the
            # same position would be one quantity written twice, and the sum
            # against the actual row would still add up — which is exactly the
            # kind of double count no later report can detect.
            models.UniqueConstraint(
                fields=["actual", "lot", "location"],
                name="production_allocation_position_unique_per_actual",
                nulls_distinct=False,
            ),
            # A posted allocation names its movement and its value together.
            models.CheckConstraint(
                condition=Q(movement__isnull=True, consumed_value__isnull=True)
                | Q(movement__isnull=False, consumed_value__isnull=False),
                name="production_allocation_posting_evidence_is_complete",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.actual_id}#{self.allocation_order} {self.base_quantity}"

    @property
    def quantity_display(self) -> str:
        return f"{self.base_quantity:f}"
