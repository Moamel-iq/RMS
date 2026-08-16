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

**`RecipeComponent` is still absent.** Nested recipes are Task 3.2B, costing is
Task 3.3, and production is Task 3.4/3.5. Nothing here moves stock, touches a
ledger, or knows a price. A recipe is an intention; the production batch is the
event (RCP-002).

See `docs/tasks/task-3-0-recipes-production-domain-spec.md` for the approved
design — §3 the recipe, §4 versions, §5 lines, §5A steps, §5C servings — plus
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
