"""
Sales — the menu, what it is sold through, and what it is sold for.

Phase 4 builds this domain one checkpoint at a time, and each checkpoint brings
its own aggregates *and* the permissions that guard them. That ordering is the
repository's standing rule rather than a convenience: a permission for a
workflow that does not exist is a grant nobody can audit.

Checkpoint 1 — this file's current contents:

    MenuCategory            how the menu is grouped
    MenuItem                one sellable thing, and what fulfils it
    MenuItemBranchSetting   where it is sold, and whether it is available
    MenuPriceVersion        what it costs the customer, effective-dated
    SalesChannel            how the customer bought it

See `docs/tasks/task-4-0-sales-domain-spec.md` for the approved design, and
ADR-027 / ADR-028 for the decisions the constraints here enforce.

## Two things this module deliberately does not carry

**No stored balance, anywhere.** Not on a menu item, not on a channel, and —
when checkpoint 5 arrives — not on a delivery application either. Every figure
is derived from the documents that produced it. A stored balance is a second
source of truth, and the one that drifts is always the stored one.

**No account foreign key as the primary answer.** Which account a sale posts to
is an `AccountRole` mapping (ADR-019). Where a channel or an application
genuinely needs to differ from the organization default it carries an explicit
*override*, which the posting service consults after the role — never instead
of it.
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
from apps.core.money import MONEY_PLACES, RATE_PLACES, UNIT_PRICE_PLACES
from apps.core.quantity import CALCULATION_PLACES, QUANTITY_PLACES

#: Codes are the same shape every other master in this system uses, and are
#: canonicalised to uppercase before storage so uniqueness is case-insensitive
#: in effect without a functional index.
CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._-]*$"

#: Room for IQD, a currency with large nominal amounts.
MONEY_MAX_DIGITS = MONEY_PLACES + 18
UNIT_PRICE_MAX_DIGITS = UNIT_PRICE_PLACES + 15
QUANTITY_MAX_DIGITS = QUANTITY_PLACES + 15
CALCULATION_MAX_DIGITS = CALCULATION_PLACES + 15
#: A commission percentage. Six places because a contract can state 15.75% and
#: a basis point matters on a month of orders.
RATE_MAX_DIGITS = RATE_PLACES + 6

#: A rate zero, named so a comparison never silently compares a `Decimal`
#: against a bare `int`.
ZERO_RATE = Decimal("0")

#: Demo rows are prefixed and labelled, never silently mixed with real data.
DEMO_CODE_PREFIX = "DEMO-"
DEMO_NOTICE = _("تجريبي — غير معتمد للإنتاج")


# ---------------------------------------------------------------------------
# Menu master
# ---------------------------------------------------------------------------


class MenuCategory(TimeStampedModel):
    """
    How the menu is grouped for the people reading it.

    Presentation only. Nothing about a category reaches a journal, and no
    posting rule consults it — grouping the mandi dishes together is a decision
    about a menu board, and letting it decide an account would make a designer's
    choice a financial one.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="menu_categories",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)
    display_order = models.PositiveIntegerField(_("display order"), default=1)
    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("menu category")
        verbose_name_plural = _("menu categories")
        ordering = ["organization__code", "display_order", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="sales_menu_category_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN),
                name="sales_menu_category_code_format",
            ),
            models.CheckConstraint(
                condition=~Q(name_ar=""), name="sales_menu_category_name_not_empty"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


class FulfillmentSource(models.TextChoices):
    """
    What actually leaves the building when this item is sold.

    `RECIPE_SERVING` is Release 1. The item names a recipe and a serving code;
    the sale resolves the recipe version in force on its business date and the
    serving on that version, and the kitchen's theoretical consumption follows
    from the pair.

    `DIRECT_STOCK` — a bottle of water taken off a shelf — is **declared and
    refused**, in the service and by a check constraint. There is no certified
    sales-and-COGS route out of a warehouse, and improvising one would open a
    second stock-consumption path beside production, which is exactly the kind
    of parallel ledger this system has spent three phases avoiding. Declaring
    the value now means enabling it later is a service branch and a dropped
    constraint rather than a redesign of every sales line (ADR-027 §10).
    """

    RECIPE_SERVING = "RECIPE_SERVING", _("حصة من وصفة")
    DIRECT_STOCK = "DIRECT_STOCK", _("صنف مخزني مباشر")


class MenuItem(TimeStampedModel):
    """
    One thing a customer can buy.

    Organization master data, like the recipe it is built on: the dish is one
    dish, and a branch inventing its own version of the group's menu would make
    every cross-branch comparison meaningless (RCP-006). Where a branch
    genuinely differs — this is not sold here, this costs more here — that is
    `MenuItemBranchSetting` and `MenuPriceVersion`, which are *data about* the
    one item rather than a second item.

    **Points at a `Recipe` and a serving *code*, not at a `RecipeServing`.**
    That looks like a missing foreign key and is deliberate: servings belong to
    a *version*, so a direct link would pin the menu to one version of the
    recipe forever and break the moment the kitchen approved a new one. The
    code is what is stable across versions — `WHOLE` means the same thing in
    version 3 as in version 1 — and the sale resolves the actual serving row on
    the version effective at its own business date (ADR-027 §4).

    A consequence worth stating: if the version in force on a date has no
    serving with this code, the item **cannot be sold on that date**. The
    service refuses the line rather than falling back to another serving,
    because a fallback is a silent answer to a question the operator did not
    know they were asking.

    **Carries no price.** Prices are effective-dated and scoped, and live in
    `MenuPriceVersion`. **Carries no cost**, for the reason `Recipe` carries
    none: cost is derived from the version's lines against the ledger.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="menu_items",
        verbose_name=_("organization"),
    )
    category = models.ForeignKey(
        MenuCategory,
        on_delete=models.PROTECT,
        related_name="items",
        null=True,
        blank=True,
        verbose_name=_("category"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)
    description_ar = models.TextField(_("description (Arabic)"), blank=True)

    fulfillment_source = models.CharField(
        _("fulfillment source"),
        max_length=16,
        choices=FulfillmentSource.choices,
        default=FulfillmentSource.RECIPE_SERVING,
    )
    #: PROTECT rather than CASCADE: deleting a recipe that something is sold as
    #: would take the menu with it, and the correct act is to archive the item.
    recipe = models.ForeignKey(
        "kitchen.Recipe",
        on_delete=models.PROTECT,
        related_name="menu_items",
        null=True,
        blank=True,
        verbose_name=_("recipe"),
    )
    #: The serving's stable code, resolved against the effective version at
    #: sale time. See the class docstring for why this is not a foreign key.
    serving_code = models.CharField(_("serving code"), max_length=32, blank=True)

    display_order = models.PositiveIntegerField(_("display order"), default=1)
    notes = models.TextField(_("notes"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    #: The identity a sales line points at. Immutable, and deliberately not the
    #: primary key or the code: a code can be corrected, and a posted line has
    #: to still point at something in five years (ADR-017).
    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("menu item")
        verbose_name_plural = _("menu items")
        ordering = ["organization__code", "display_order", "code"]
        # `view_menuitem` arrives from Django's default set; `view_sales` is
        # this module's own read grant and covers the whole domain, exactly as
        # `view_kitchen_report` covers the kitchen's report family. Declaring
        # them both is fine — they do not collide.
        permissions = [
            ("view_sales", _("Can view sales data")),
            ("manage_menu", _("Can create, edit and archive menu items and prices")),
            ("view_sales_reports", _("Can view sales reports and the dashboard")),
            ("view_sales_cost", _("Can view food cost and margin on sales screens")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="sales_menu_item_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="sales_menu_item_code_format"
            ),
            models.CheckConstraint(condition=~Q(name_ar=""), name="sales_menu_item_name_not_empty"),
            # A recipe-served item without a recipe or without a serving code
            # is an item nothing can fulfil. Refused here rather than
            # discovered by a cashier at the till.
            models.CheckConstraint(
                condition=(
                    ~Q(fulfillment_source=FulfillmentSource.RECIPE_SERVING)
                    | (Q(recipe__isnull=False) & ~Q(serving_code=""))
                ),
                name="sales_menu_item_recipe_serving_is_complete",
            ),
            # Release 1 refuses `DIRECT_STOCK` **in the database**, not only in
            # the service. Enabling direct-stock sales is then an explicit act
            # — a migration that drops this constraint alongside the service
            # that posts the COGS — rather than something a shell session or a
            # CSV import can do by accident.
            models.CheckConstraint(
                condition=Q(fulfillment_source=FulfillmentSource.RECIPE_SERVING),
                name="sales_menu_item_direct_stock_is_deferred",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"

    @property
    def is_demo(self) -> bool:
        return self.code.startswith(DEMO_CODE_PREFIX)


class MenuItemBranchSetting(TimeStampedModel):
    """
    Whether one branch sells one item, and what it calls it locally.

    A row's **absence** means the item is not offered at that branch. That is a
    deliberate choice over a nullable `is_available` on the item: absence is
    unambiguous, and it makes "what does this branch sell" one join rather than
    a query that has to reason about three states.
    """

    menu_item = models.ForeignKey(
        MenuItem,
        on_delete=models.CASCADE,
        related_name="branch_settings",
        verbose_name=_("menu item"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="menu_item_settings",
        verbose_name=_("branch"),
    )
    #: Present but temporarily off — sold here normally, not today. Distinct
    #: from having no row at all, which means never sold here.
    is_available = models.BooleanField(_("available"), default=True)
    local_name_ar = models.CharField(_("local name (Arabic)"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("menu item branch setting")
        verbose_name_plural = _("menu item branch settings")
        ordering = ["menu_item__code", "branch__code"]
        constraints = [
            models.UniqueConstraint(
                fields=["menu_item", "branch"],
                name="sales_menu_item_branch_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.menu_item.code} @ {self.branch.code}"


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class SalesChannelCategory(models.TextChoices):
    """
    How the customer reached the restaurant. A **closed** set.

    Closed because posting behaviour depends on it: whether a cashier is
    involved, whether a delivery application is, and which tender the money
    lands in. A free-text channel type would let a typo pick a default, and the
    default would be a financial destination.

    Note what this is **not**: one value per delivery company. Talabat and
    Toters are both `DELIVERY_APPLICATION`, and which company is separate
    master data — `DeliveryApplication`, checkpoint 2. A channel per
    application would multiply every report's channel axis by the number of
    contracts and make "how much did we sell through apps" a sum nobody
    maintains.
    """

    DINE_IN = "DINE_IN", _("صالة")
    TAKEAWAY = "TAKEAWAY", _("سفري")
    DIRECT_DELIVERY = "DIRECT_DELIVERY", _("توصيل مباشر")
    DELIVERY_APPLICATION = "DELIVERY_APPLICATION", _("تطبيق توصيل")
    OTHER = "OTHER", _("أخرى")


class TenderDestination(models.TextChoices):
    """
    Where the money goes when this channel's sale posts.

    Three destinations, and each is a different economic claim:

    * `CASH` — in the drawer, countable, and what a cashier closing reconciles.
    * `CARD` — a clearing asset. Real money the restaurant does not have yet;
      treating it as cash would make every closing count short by the day's
      card volume.
    * `APPLICATION_RECEIVABLE` — owed by a delivery company, cleared by a
      settlement rather than by a count.
    """

    CASH = "CASH", _("نقد")
    CARD = "CARD", _("بطاقة")
    APPLICATION_RECEIVABLE = "APPLICATION_RECEIVABLE", _("ذمة تطبيق")


class SalesChannel(TimeStampedModel):
    """
    A route a sale arrives by, and the financial behaviour that follows.

    **Carries the cost centre**, and that is the one field here that is not
    obvious. Revenue, discount, commission and fee accounts are classes 4, 5
    and 6, all of which require a cost centre (ADR-014), so every sales journal
    needs one and it has to come from somewhere principled. The channel is the
    only master with the right granularity and the right meaning: dine-in earns
    in the hall, application orders earn in delivery. Taking it from the branch
    would make every department's contribution identical; taking it from the
    menu item would say a dish belongs to a department, which it does not.

    `revenue_account` is an **override**, consulted after the `SALES_REVENUE`
    role and never instead of it. Null is the ordinary case.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="sales_channels",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)

    category = models.CharField(_("category"), max_length=24, choices=SalesChannelCategory.choices)
    default_tender = models.CharField(
        _("default tender"),
        max_length=24,
        choices=TenderDestination.choices,
        default=TenderDestination.CASH,
    )
    cost_center = models.ForeignKey(
        "accounting.CostCenter",
        on_delete=models.PROTECT,
        related_name="sales_channels",
        verbose_name=_("cost center"),
    )
    #: Optional. The organization's `SALES_REVENUE` mapping is the default and
    #: this narrows it — hall takings into one revenue account, application
    #: takings into another, when the organization wants that split.
    revenue_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="sales_channel_overrides",
        null=True,
        blank=True,
        verbose_name=_("revenue account override"),
    )

    #: A channel whose money passes through a till. Drives which sales days a
    #: cashier shift is expected to reconcile.
    requires_cashier = models.BooleanField(_("requires a cashier"), default=True)
    #: A channel whose lines must name a delivery application. True exactly
    #: when the category is `DELIVERY_APPLICATION`, enforced below rather than
    #: left to agree by convention.
    requires_delivery_application = models.BooleanField(
        _("requires a delivery application"), default=False
    )

    display_order = models.PositiveIntegerField(_("display order"), default=1)
    notes = models.TextField(_("notes"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("sales channel")
        verbose_name_plural = _("sales channels")
        ordering = ["organization__code", "display_order", "code"]
        permissions = [
            ("manage_sales_channels", _("Can create, edit and archive sales channels")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="sales_channel_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="sales_channel_code_format"
            ),
            models.CheckConstraint(condition=~Q(name_ar=""), name="sales_channel_name_not_empty"),
            # The two must agree, and the database says so. A channel flagged
            # `DELIVERY_APPLICATION` whose lines need not name an application
            # would post application sales with nobody to collect from.
            models.CheckConstraint(
                condition=(
                    Q(
                        category=SalesChannelCategory.DELIVERY_APPLICATION,
                        requires_delivery_application=True,
                    )
                    | (
                        ~Q(category=SalesChannelCategory.DELIVERY_APPLICATION)
                        & Q(requires_delivery_application=False)
                    )
                ),
                name="sales_channel_application_flag_matches_category",
            ),
            # An application channel is settled, never counted in a drawer.
            models.CheckConstraint(
                condition=(
                    ~Q(category=SalesChannelCategory.DELIVERY_APPLICATION)
                    | Q(default_tender=TenderDestination.APPLICATION_RECEIVABLE)
                ),
                name="sales_channel_application_tender_is_receivable",
            ),
            # ...and the converse: only an application channel may settle.
            models.CheckConstraint(
                condition=(
                    ~Q(default_tender=TenderDestination.APPLICATION_RECEIVABLE)
                    | Q(category=SalesChannelCategory.DELIVERY_APPLICATION)
                ),
                name="sales_channel_receivable_tender_needs_application_category",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"

    @property
    def is_application_channel(self) -> bool:
        return self.category == SalesChannelCategory.DELIVERY_APPLICATION


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


class PriceScope(models.TextChoices):
    """
    How narrowly a price applies. Resolution is **most specific wins**.

    `APPLICATION` arrives with checkpoint 2, together with the delivery
    application master it names. It is declared here because the resolution
    order is a property of this vocabulary and splitting it across two
    checkpoints would make the earlier half read as complete when it is not.
    """

    BRANCH_DEFAULT = "BRANCH_DEFAULT", _("سعر الفرع الافتراضي")
    CHANNEL = "CHANNEL", _("سعر قناة")
    APPLICATION = "APPLICATION", _("سعر تطبيق")


class MenuPriceVersion(TimeStampedModel):
    """
    What one item costs the customer, from one date to another.

    Effective-dated and never edited in place, for exactly the reason recipe
    versions are (ADR-024): a price changed on Monday must not restate Sunday's
    revenue. A correction is a new version, and the sales line snapshots the
    one it used.

    **Overlaps within a scope are refused by a PostgreSQL exclusion
    constraint**, not by a service check. A `UniqueConstraint` cannot express
    it — the clash is between *ranges*, and two rows can each be legitimate
    alone and contradictory together — and a service check alone would be a
    promise rather than a guarantee that a raw `INSERT` also keeps. The
    constraint lives in migration `0002`, in the same shape
    `procurement/0003` and `inventory/0002` use.

    `effective_to` NULL means open-ended, which is the ordinary case for a
    current price.
    """

    menu_item = models.ForeignKey(
        MenuItem,
        on_delete=models.PROTECT,
        related_name="prices",
        verbose_name=_("menu item"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="menu_prices",
        verbose_name=_("branch"),
    )
    scope = models.CharField(
        _("scope"),
        max_length=16,
        choices=PriceScope.choices,
        default=PriceScope.BRANCH_DEFAULT,
    )
    channel = models.ForeignKey(
        SalesChannel,
        on_delete=models.PROTECT,
        related_name="menu_prices",
        null=True,
        blank=True,
        verbose_name=_("channel"),
    )
    #: Checkpoint 2. An application-scoped price is the narrowest answer there
    #: is, and it exists because the delivery companies genuinely charge
    #: differently: the same plate is listed higher on an application than in
    #: the hall, and pretending otherwise would understate both the revenue and
    #: the commission it attracts.
    delivery_application = models.ForeignKey(
        "sales.DeliveryApplication",
        on_delete=models.PROTECT,
        related_name="menu_prices",
        null=True,
        blank=True,
        verbose_name=_("delivery application"),
    )

    unit_price = models.DecimalField(
        _("unit price"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
    )
    effective_from = models.DateField(_("effective from"))
    effective_to = models.DateField(_("effective to"), null=True, blank=True)

    evidence_reference = models.CharField(_("evidence"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("menu price version")
        verbose_name_plural = _("menu price versions")
        ordering = ["menu_item__code", "branch__code", "-effective_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(unit_price__gte=Decimal("0")),
                name="sales_menu_price_is_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="sales_menu_price_range_is_ordered",
            ),
            # The scope and the pointer it needs must agree. A `CHANNEL` price
            # with no channel is a price that applies to nothing; a
            # `BRANCH_DEFAULT` price *with* a channel is a price two different
            # resolvers would read differently.
            models.CheckConstraint(
                condition=(
                    Q(scope=PriceScope.CHANNEL, channel__isnull=False)
                    | (~Q(scope=PriceScope.CHANNEL) & Q(channel__isnull=True))
                ),
                name="sales_menu_price_channel_matches_scope",
            ),
            # Checkpoint 2 replaces the "not yet available" refusal with the
            # real rule: an application-scoped price names an application, and
            # nothing else does.
            models.CheckConstraint(
                condition=(
                    Q(scope=PriceScope.APPLICATION, delivery_application__isnull=False)
                    | (~Q(scope=PriceScope.APPLICATION) & Q(delivery_application__isnull=True))
                ),
                name="sales_menu_price_application_matches_scope",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.menu_item.code} @ {self.branch.code}: {self.unit_price:f}"

    @property
    def price_display(self) -> str:
        """
        The price as a technical identity: period, never comma.

        Django localises Decimals, so under Arabic a stored `12500.000000`
        renders `12500,000000`, which reads as a grouped thousand beside a
        money column that genuinely is grouped that way. The same rule the
        recipe serving's `quantity_display` follows, and for the same reason.
        """
        return f"{self.unit_price:f}"


# ---------------------------------------------------------------------------
# Delivery applications — checkpoint 2
# ---------------------------------------------------------------------------


class DeliveryApplication(TimeStampedModel):
    """
    A delivery company the restaurant sells through.

    Separate master data from `SalesChannel`, and the separation is the point.
    Talabat and Toters both arrive through the one `DELIVERY_APPLICATION`
    channel; which company took the order is *this*. A channel per company
    would multiply every report's channel axis by the number of contracts and
    turn "how much did we sell through apps" into a sum somebody maintains by
    hand.

    **Carries no balance.** What an application owes is derived from the
    append-only `ApplicationReceivableEntry` ledger every time it is asked for
    (ADR-027 §5). A stored balance is a number that can disagree with the
    entries that produced it, and the disagreement surfaces during a settlement
    argument — the one moment it cannot be sorted out quietly.

    **Carries no commission rate**, either. Rates are effective-dated contract
    terms and live on `DeliveryAgreement`; a rate here would be a second answer
    that no posted sale could be traced to.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="delivery_applications",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)

    #: How often the company remits. Reported and used to age a receivable; it
    #: refuses nothing, because a company that settles late is a commercial
    #: problem rather than a data-entry error.
    settlement_cycle_days = models.PositiveSmallIntegerField(
        _("settlement cycle (days)"), default=30
    )
    #: Optional override of the organization's `DELIVERY_APP_RECEIVABLE`
    #: mapping. The chart already carries one account per named company, and an
    #: organization that wants them separated says so here — accounting never
    #: learns what a delivery application is (ADR-019).
    receivable_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        related_name="delivery_application_overrides",
        null=True,
        blank=True,
        verbose_name=_("receivable account override"),
    )

    contact_name = models.CharField(_("contact person"), max_length=200, blank=True)
    phone = models.CharField(_("phone"), max_length=20, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("delivery application")
        verbose_name_plural = _("delivery applications")
        ordering = ["organization__code", "code"]
        permissions = [
            (
                "manage_delivery_applications",
                _("Can create, edit and archive delivery applications"),
            ),
            ("view_application_receivables", _("Can view delivery application receivables")),
            (
                "manage_application_settlements",
                _("Can create, reconcile, post and reverse application settlements"),
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="sales_delivery_application_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN),
                name="sales_delivery_application_code_format",
            ),
            models.CheckConstraint(
                condition=~Q(name_ar=""), name="sales_delivery_application_name_not_empty"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"

    @property
    def is_demo(self) -> bool:
        return self.code.startswith(DEMO_CODE_PREFIX)


class DeliveryApplicationBranchSetting(TimeStampedModel):
    """
    Whether one branch trades with one application, and under what store id.

    A missing row means the branch is not live on that application. Same
    arrangement as `MenuItemBranchSetting`, and for the same reason: absence is
    unambiguous, and it makes "which apps is this branch on" one join.
    """

    delivery_application = models.ForeignKey(
        DeliveryApplication,
        on_delete=models.CASCADE,
        related_name="branch_settings",
        verbose_name=_("delivery application"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="delivery_application_settings",
        verbose_name=_("branch"),
    )
    is_active = models.BooleanField(_("active"), default=True)
    #: The identifier the application's own dashboard uses for this branch.
    #: Reconciliation reads it; nothing posts on it.
    external_store_code = models.CharField(_("store code"), max_length=64, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("delivery application branch setting")
        verbose_name_plural = _("delivery application branch settings")
        ordering = ["delivery_application__code", "branch__code"]
        constraints = [
            models.UniqueConstraint(
                fields=["delivery_application", "branch"],
                name="sales_delivery_application_branch_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.delivery_application.code} @ {self.branch.code}"


class CommissionBasis(models.TextChoices):
    """
    What the commission percentage is charged on. A **closed** vocabulary.

    Closed because it decides money. A free-text basis would let a typo —
    `AFTER_ALL_DISCOUNT` — fall through to a default, and the default would be
    a pricing error nobody sees for a quarter.

    `AFTER_ALL_DISCOUNTS` and `CUSTOMER_PAID_AMOUNT` are numerically identical
    in Release 1 and are **still separate values**. They are different
    contractual claims, and they diverge the moment a delivery fee or a tip
    enters the model; collapsing them now would mean a later divergence
    silently restated every historical agreement (ADR-028 §4).
    """

    GROSS_LIST_AMOUNT = "GROSS_LIST_AMOUNT", _("إجمالي السعر المعلن")
    AFTER_RESTAURANT_DISCOUNT = "AFTER_RESTAURANT_DISCOUNT", _("بعد خصم المطعم")
    AFTER_ALL_DISCOUNTS = "AFTER_ALL_DISCOUNTS", _("بعد كل الخصومات")
    CUSTOMER_PAID_AMOUNT = "CUSTOMER_PAID_AMOUNT", _("المبلغ المدفوع من الزبون")


class DeliveryAgreement(TimeStampedModel):
    """
    What one application charges one branch, from one date to another.

    Effective-dated and never edited once a sale has snapshotted it — the same
    rule as a price and a recipe version, and it matters more here because the
    figure is a *cost*. A sales line stores the agreement identity **and** every
    calculation field, so a posted commission can be re-derived years later
    without reading this table at all.

    Scoped to a branch rather than to the organization, because that is how the
    contracts actually work: a new branch is onboarded at a different rate, and
    an organization-wide agreement would restate what every other branch pays
    the moment one of them renegotiated.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="delivery_agreements",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="delivery_agreements",
        verbose_name=_("branch"),
    )
    delivery_application = models.ForeignKey(
        DeliveryApplication,
        on_delete=models.PROTECT,
        related_name="agreements",
        verbose_name=_("delivery application"),
    )

    commission_percent = models.DecimalField(
        _("commission percent"),
        max_digits=RATE_MAX_DIGITS,
        decimal_places=RATE_PLACES,
        default=Decimal("0"),
    )
    #: Per order, where the contract states one. Zero is the common case and is
    #: a real answer rather than a missing one, which is why this is not null.
    fixed_fee_per_order = models.DecimalField(
        _("fixed fee per order"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )
    commission_basis = models.CharField(
        _("commission basis"),
        max_length=32,
        choices=CommissionBasis.choices,
        default=CommissionBasis.GROSS_LIST_AMOUNT,
    )
    #: How long after the sale the company is expected to remit. Reported, and
    #: used to age the receivable; it blocks nothing.
    settlement_lag_days = models.PositiveSmallIntegerField(_("settlement lag (days)"), default=30)

    effective_from = models.DateField(_("effective from"))
    effective_to = models.DateField(_("effective to"), null=True, blank=True)

    evidence_reference = models.CharField(_("evidence"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("delivery agreement")
        verbose_name_plural = _("delivery agreements")
        ordering = ["delivery_application__code", "branch__code", "-effective_from"]
        permissions = [
            ("manage_sales_agreements", _("Can create and end delivery agreements")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(commission_percent__gte=Decimal("0"))
                & Q(commission_percent__lte=Decimal("100")),
                name="sales_agreement_percent_in_range",
            ),
            models.CheckConstraint(
                condition=Q(fixed_fee_per_order__gte=Decimal("0")),
                name="sales_agreement_fee_is_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="sales_agreement_range_is_ordered",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.delivery_application.code} @ {self.branch.code} from {self.effective_from}"

    @property
    def percent_display(self) -> str:
        """A rate as a technical identity: period, never comma."""
        return f"{self.commission_percent:f}"


# ---------------------------------------------------------------------------
# Discounts — checkpoint 2
# ---------------------------------------------------------------------------


class DiscountProgram(TimeStampedModel):
    """
    A discount somebody has to fund, and the record of who funds it.

    **The funding split is the whole model.** A shared discount's restaurant
    share plus its application share must equal the discount — enforced by a
    check constraint here and re-checked on every sales line — because the two
    halves are economically different and neither may impersonate the other:

    * the **restaurant-funded** portion is contra-revenue: the restaurant chose
      not to collect it;
    * the **application-funded** portion is reimbursed, so it reduces neither
      revenue nor what the application owes — it is *part* of what it owes.

    Treating the second as a restaurant discount understates revenue and
    understates the receivable by the same amount, and both figures look
    internally consistent. The error then surfaces as a recurring favourable
    settlement variance, which is exactly the kind of difference that gets
    normalised into a rounding adjustment and never investigated (ADR-028 §3).

    Exactly one of `discount_percent` and `discount_amount` is set. A programme
    with both would have two answers; a programme with neither would have none.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="discount_programs",
        verbose_name=_("organization"),
    )
    #: NULL means every branch. A branch-specific programme names one.
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="discount_programs",
        null=True,
        blank=True,
        verbose_name=_("branch"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)

    discount_percent = models.DecimalField(
        _("discount percent"),
        max_digits=RATE_MAX_DIGITS,
        decimal_places=RATE_PLACES,
        null=True,
        blank=True,
    )
    discount_amount = models.DecimalField(
        _("discount amount"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )
    #: A ceiling on a percentage programme. NULL means no ceiling, which is a
    #: different statement from zero.
    maximum_amount = models.DecimalField(
        _("maximum amount"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )

    restaurant_funded_share = models.DecimalField(
        _("restaurant-funded share (%)"),
        max_digits=RATE_MAX_DIGITS,
        decimal_places=RATE_PLACES,
        default=Decimal("100"),
    )
    application_funded_share = models.DecimalField(
        _("application-funded share (%)"),
        max_digits=RATE_MAX_DIGITS,
        decimal_places=RATE_PLACES,
        default=Decimal("0"),
    )

    #: Applicability. NULL on each means "no restriction on this axis".
    channel = models.ForeignKey(
        SalesChannel,
        on_delete=models.PROTECT,
        related_name="discount_programs",
        null=True,
        blank=True,
        verbose_name=_("channel"),
    )
    delivery_application = models.ForeignKey(
        DeliveryApplication,
        on_delete=models.PROTECT,
        related_name="discount_programs",
        null=True,
        blank=True,
        verbose_name=_("delivery application"),
    )
    menu_item = models.ForeignKey(
        MenuItem,
        on_delete=models.PROTECT,
        related_name="discount_programs",
        null=True,
        blank=True,
        verbose_name=_("menu item"),
    )

    effective_from = models.DateField(_("effective from"))
    effective_to = models.DateField(_("effective to"), null=True, blank=True)

    evidence_reference = models.CharField(_("evidence"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("discount program")
        verbose_name_plural = _("discount programs")
        ordering = ["organization__code", "code"]
        permissions = [
            ("manage_sales_discounts", _("Can create and end discount programs")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="sales_discount_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="sales_discount_code_format"
            ),
            models.CheckConstraint(condition=~Q(name_ar=""), name="sales_discount_name_not_empty"),
            # Exactly one of the two ways to state a discount.
            models.CheckConstraint(
                condition=(
                    Q(discount_percent__isnull=False, discount_amount__isnull=True)
                    | Q(discount_percent__isnull=True, discount_amount__isnull=False)
                ),
                name="sales_discount_percent_xor_amount",
            ),
            models.CheckConstraint(
                condition=Q(discount_percent__isnull=True)
                | (Q(discount_percent__gt=Decimal("0")) & Q(discount_percent__lte=Decimal("100"))),
                name="sales_discount_percent_in_range",
            ),
            models.CheckConstraint(
                condition=Q(discount_amount__isnull=True) | Q(discount_amount__gt=Decimal("0")),
                name="sales_discount_amount_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(maximum_amount__isnull=True) | Q(maximum_amount__gt=Decimal("0")),
                name="sales_discount_maximum_is_positive",
            ),
            # **The funding equality.** The single most important line in this
            # model: a shared discount whose halves do not close is a discount
            # somebody is paying for and nobody has recorded.
            models.CheckConstraint(
                condition=Q(
                    restaurant_funded_share=Decimal("100") - models.F("application_funded_share")
                ),
                name="sales_discount_funding_shares_close",
            ),
            models.CheckConstraint(
                condition=Q(restaurant_funded_share__gte=Decimal("0"))
                & Q(restaurant_funded_share__lte=Decimal("100")),
                name="sales_discount_restaurant_share_in_range",
            ),
            models.CheckConstraint(
                condition=Q(application_funded_share__gte=Decimal("0"))
                & Q(application_funded_share__lte=Decimal("100")),
                name="sales_discount_application_share_in_range",
            ),
            # An application-funded share is a promise by a delivery company,
            # so a programme that claims one must name the company that made
            # it. Without this a "50% funded by the app" promotion could be
            # applied to a cash sale in the hall, and the receivable it implies
            # would be owed by nobody.
            models.CheckConstraint(
                condition=Q(application_funded_share=Decimal("0"))
                | Q(delivery_application__isnull=False),
                name="sales_discount_application_funding_names_an_application",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="sales_discount_range_is_ordered",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"

    @property
    def is_shared(self) -> bool:
        """Both parties fund part of it."""
        return (
            self.application_funded_share > ZERO_RATE and self.restaurant_funded_share > ZERO_RATE
        )

    @property
    def percent_display(self) -> str:
        return f"{self.discount_percent:f}" if self.discount_percent is not None else ""


# ---------------------------------------------------------------------------
# The daily sales document — checkpoint 3
# ---------------------------------------------------------------------------


class SalesDocumentSequence(models.Model):
    """
    The gapless per-organization, per-type, per-year counter.

    A fourth sequence table beside inventory's, procurement's and kitchen's,
    for the reason kitchen's docstring records: a sales day is not an inventory
    document, and keying it into another module's type enum would make that
    enum a statement about documents the module does not own. The counting
    *rule* is four lines under a row lock and is deliberately identical — what
    must never be shared is a sequence two documents could draw from.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="sales_sequences",
        verbose_name=_("organization"),
    )
    document_type = models.CharField(_("document type"), max_length=32)
    year = models.PositiveSmallIntegerField(_("year"))
    last_number = models.PositiveIntegerField(_("last number"), default=0)

    class Meta:
        verbose_name = _("sales document sequence")
        verbose_name_plural = _("sales document sequences")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "document_type", "year"],
                name="sales_sequence_unique_per_type_and_year",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.code} {self.document_type} {self.year}: {self.last_number}"


class SalesDayStatus(models.TextChoices):
    """
    The lifecycle of one branch's trading day.

    `SUBMITTED` exists here even though the supplier invoice has no such state,
    and the comparison is worth stating because it looks inconsistent. A
    supplier invoice arrives as a document somebody *else* authored, so
    approving it is itself the second pair of eyes. A sales day is authored
    in-house by the person whose till it describes, so the second pair of eyes
    has to come after the authoring is finished: `SUBMITTED` is the cashier
    saying "this is what I counted", and `POSTED` is somebody else agreeing it
    may reach the ledger (ADR-027 §3).
    """

    DRAFT = "DRAFT", _("مسودة")
    SUBMITTED = "SUBMITTED", _("مُرسل")
    POSTED = "POSTED", _("مرحّل")
    REVERSED = "REVERSED", _("معكوس")


class SalesDay(TimeStampedModel):
    """
    One branch's trading on one business date.

    **A day rather than an order**, and that is a decision about evidence
    rather than about convenience. Release 1 has no point-of-sale integration;
    takings arrive as a till report and three application dashboards. Modelling
    an individual order would mean inventing order identity the restaurant does
    not have, and every report would then rest on fabricated granularity.
    `SalesDayLine.order_count` records how many orders produced a quantity
    without pretending to identify them.

    **A posted day is never edited.** Correction is reversal plus a replacement
    day, or a `SalesAdjustment` where the correction concerns one line. The
    freeze is a database trigger, not application code.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="sales_days",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="sales_days",
        verbose_name=_("branch"),
    )
    #: Entered, never derived from a timestamp. Which day a sale belongs to
    #: depends on the branch's own business-day start, and `date(timestamp)`
    #: would file a 01:30 sale under the wrong day every night.
    business_date = models.DateField(_("business date"))

    status = models.CharField(
        _("status"), max_length=12, choices=SalesDayStatus.choices, default=SalesDayStatus.DRAFT
    )
    #: Assigned at successful posting and never before. A number on a draft is
    #: a gap in the sequence waiting to happen.
    number = models.CharField(_("number"), max_length=32, blank=True)

    notes = models.TextField(_("notes"), blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sales_days_created",
        null=True,
        blank=True,
        verbose_name=_("created by"),
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sales_days_submitted",
        null=True,
        blank=True,
        verbose_name=_("submitted by"),
    )
    submitted_at = models.DateTimeField(_("submitted at"), null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sales_days_posted",
        null=True,
        blank=True,
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sales_days_reversed",
        null=True,
        blank=True,
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    #: The identity every journal, receivable entry and consumption
    #: contribution points at. Immutable, and deliberately not the primary key.
    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)
    #: Derived from `public_id` at posting rather than accepted from a caller:
    #: posting *this day* is the command, the day is the payload, and a posted
    #: day is frozen — so a retry cannot present the same key with a different
    #: payload. The same arrangement `post_goods_receipt` uses.
    idempotency_key = models.CharField(_("idempotency key"), max_length=128, blank=True)
    request_fingerprint = models.CharField(_("request fingerprint"), max_length=128, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("sales day")
        verbose_name_plural = _("sales days")
        ordering = ["-business_date", "branch__code"]
        permissions = [
            ("create_daily_sales", _("Can create and edit a draft sales day")),
            ("submit_daily_sales", _("Can submit a sales day for posting")),
            ("post_daily_sales", _("Can post a sales day to the ledger")),
            ("reverse_daily_sales", _("Can reverse a posted sales day")),
            ("manage_sales_adjustments", _("Can record returns and cancellations")),
            ("close_cashier_shift", _("Can open and close a cashier shift")),
            ("approve_cashier_closing", _("Can approve a cashier closing")),
        ]
        constraints = [
            # One document per branch per day. Two would each be a partial
            # truth, and every report would have to know to add them.
            models.UniqueConstraint(
                fields=["branch", "business_date"],
                name="sales_day_unique_per_branch_and_date",
            ),
            # A number exists exactly when the day has reached the ledger.
            models.CheckConstraint(
                condition=(
                    Q(status__in=["DRAFT", "SUBMITTED"], number="")
                    | (Q(status__in=["POSTED", "REVERSED"]) & ~Q(number=""))
                ),
                name="sales_day_number_iff_posted",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="REVERSED", reversed_at__isnull=False)
                    | (~Q(status="REVERSED") & Q(reversed_at__isnull=True))
                ),
                name="sales_day_reversal_is_complete",
            ),
            models.CheckConstraint(
                condition=~Q(status="REVERSED") | ~Q(reversal_reason=""),
                name="sales_day_reversal_has_a_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.branch.code} {self.business_date.isoformat()} ({self.get_status_display()})"

    @property
    def is_editable(self) -> bool:
        return self.status == SalesDayStatus.DRAFT

    @property
    def is_posted(self) -> bool:
        return self.status == SalesDayStatus.POSTED


class SalesDayLine(TimeStampedModel):
    """
    One menu item, sold through one channel, on one day.

    **Every resolved identity is stored, not re-derivable.** The recipe, the
    exact version, the serving, the price version, the agreement and every
    field its commission used are all snapshotted here. That is the charter's
    absolute rule (ADR-024) and Phase 4 is where it would be easiest to break:
    a recipe changed in September must not restate what August consumed, and a
    price changed on Monday must not restate Sunday's revenue.

    The redundancy is the point. A three-year-old line explains itself without
    reading the menu, the price table or the agreement — none of which is
    guaranteed to still contain the row it used.
    """

    sales_day = models.ForeignKey(
        SalesDay, on_delete=models.CASCADE, related_name="lines", verbose_name=_("sales day")
    )
    sequence = models.PositiveIntegerField(_("sequence"))

    menu_item = models.ForeignKey(
        MenuItem, on_delete=models.PROTECT, related_name="sales_lines", verbose_name=_("menu item")
    )
    channel = models.ForeignKey(
        SalesChannel,
        on_delete=models.PROTECT,
        related_name="sales_lines",
        verbose_name=_("channel"),
    )
    delivery_application = models.ForeignKey(
        DeliveryApplication,
        on_delete=models.PROTECT,
        related_name="sales_lines",
        null=True,
        blank=True,
        verbose_name=_("delivery application"),
    )

    # --- the snapshots ----------------------------------------------------
    recipe = models.ForeignKey(
        "kitchen.Recipe",
        on_delete=models.PROTECT,
        related_name="sales_lines",
        verbose_name=_("recipe"),
    )
    #: The **exact** version in force on this line's business date, resolved
    #: once and never re-resolved. Everything the kitchen computes from this
    #: line walks this row.
    recipe_version = models.ForeignKey(
        "kitchen.RecipeVersion",
        on_delete=models.PROTECT,
        related_name="sales_lines",
        verbose_name=_("recipe version"),
    )
    serving = models.ForeignKey(
        "kitchen.RecipeServing",
        on_delete=models.PROTECT,
        related_name="sales_lines",
        verbose_name=_("serving"),
    )
    price_version = models.ForeignKey(
        MenuPriceVersion,
        on_delete=models.PROTECT,
        related_name="sales_lines",
        null=True,
        blank=True,
        verbose_name=_("price version"),
    )
    agreement = models.ForeignKey(
        DeliveryAgreement,
        on_delete=models.PROTECT,
        related_name="sales_lines",
        null=True,
        blank=True,
        verbose_name=_("delivery agreement"),
    )
    discount_program = models.ForeignKey(
        DiscountProgram,
        on_delete=models.PROTECT,
        related_name="sales_lines",
        null=True,
        blank=True,
        verbose_name=_("discount program"),
    )

    unit_price = models.DecimalField(
        _("unit price"), max_digits=UNIT_PRICE_MAX_DIGITS, decimal_places=UNIT_PRICE_PLACES
    )
    #: Decimal, and `0.500` is an ordinary value. Nothing anywhere special-cases
    #: a half: the serving row carries the fraction, and no code names a
    #: chicken (RCP-082).
    quantity = models.DecimalField(
        _("quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    #: How many orders produced that quantity. Entered where the till reports
    #: it; zero means not stated, which is different from one order.
    order_count = models.PositiveIntegerField(_("order count"), default=0)

    gross_amount = models.DecimalField(
        _("gross amount"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    restaurant_discount = models.DecimalField(
        _("restaurant-funded discount"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )
    application_discount = models.DecimalField(
        _("application-funded discount"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )
    #: A manual discount needs a reason, and the constraint below enforces the
    #: pair. A silent override is the thing this field exists to prevent.
    manual_discount_reason = models.CharField(
        _("manual discount reason"), max_length=300, blank=True
    )

    # --- the agreement's own terms, frozen --------------------------------
    commission_basis = models.CharField(
        _("commission basis"), max_length=32, choices=CommissionBasis.choices, blank=True
    )
    commission_percent = models.DecimalField(
        _("commission percent"),
        max_digits=RATE_MAX_DIGITS,
        decimal_places=RATE_PLACES,
        default=Decimal("0"),
    )
    commission_fixed_fee = models.DecimalField(
        _("fixed fee per order"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )
    commission_amount = models.DecimalField(
        _("commission amount"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )
    other_fee_amount = models.DecimalField(
        _("other application fees"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )

    #: What the customer handed over, and what the application owes. Both are
    #: stored rather than derived at read time, because both are what a
    #: reconciliation argues with.
    customer_charge = models.DecimalField(
        _("customer charge"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    net_amount = models.DecimalField(
        _("net amount"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        help_text=_("Cash or card takings, or the expected application receivable."),
    )

    notes = models.TextField(_("notes"), blank=True)
    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("sales day line")
        verbose_name_plural = _("sales day lines")
        ordering = ["sales_day", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["sales_day", "sequence"], name="sales_day_line_sequence_unique"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=Decimal("0")), name="sales_line_quantity_is_positive"
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=Decimal("0")), name="sales_line_price_is_not_negative"
            ),
            models.CheckConstraint(
                condition=Q(gross_amount__gte=Decimal("0")),
                name="sales_line_gross_is_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(restaurant_discount__gte=Decimal("0"))
                & Q(application_discount__gte=Decimal("0")),
                name="sales_line_discounts_are_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(commission_amount__gte=Decimal("0"))
                & Q(other_fee_amount__gte=Decimal("0")),
                name="sales_line_fees_are_not_negative",
            ),
            # An application line names an application; a hall line does not.
            # Without this a cash sale could accrue a receivable owed by
            # nobody, and an application sale could post as cash.
            models.CheckConstraint(
                condition=(
                    Q(delivery_application__isnull=False, agreement__isnull=False)
                    | Q(delivery_application__isnull=True, agreement__isnull=True)
                ),
                name="sales_line_application_and_agreement_travel_together",
            ),
            # An application-funded discount is a promise by a delivery
            # company. A line with no application cannot carry one.
            models.CheckConstraint(
                condition=Q(application_discount=Decimal("0"))
                | Q(delivery_application__isnull=False),
                name="sales_line_application_discount_needs_an_application",
            ),
            # A manual discount and its reason travel together. A discount
            # nobody explained is exactly the silent override the design
            # refuses.
            models.CheckConstraint(
                condition=(
                    Q(discount_program__isnull=False)
                    | Q(restaurant_discount=Decimal("0"), application_discount=Decimal("0"))
                    | ~Q(manual_discount_reason="")
                ),
                name="sales_line_manual_discount_has_a_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.sales_day} · {self.menu_item.code} × {self.quantity:f}"

    @property
    def quantity_display(self) -> str:
        """The quantity as a technical identity: period, never comma."""
        return f"{self.quantity:f}"

    @property
    def is_application_sale(self) -> bool:
        return self.delivery_application_id is not None


class SalesTenderSummary(TimeStampedModel):
    """
    What the day's takings were **declared** to be, per tender.

    Entered rather than derived, and the difference is the whole reason this
    table exists: the derived figure is what the lines say, and this is what
    the operator says. A cashier closing reconciles a *counted* drawer against
    the derived expectation, and a day whose declared and derived cash disagree
    is a day worth looking at before it posts.
    """

    sales_day = models.ForeignKey(
        SalesDay,
        on_delete=models.CASCADE,
        related_name="tender_summaries",
        verbose_name=_("sales day"),
    )
    tender = models.CharField(_("tender"), max_length=24, choices=TenderDestination.choices)
    declared_amount = models.DecimalField(
        _("declared amount"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    notes = models.TextField(_("notes"), blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("sales tender summary")
        verbose_name_plural = _("sales tender summaries")
        ordering = ["sales_day", "tender"]
        constraints = [
            models.UniqueConstraint(
                fields=["sales_day", "tender"], name="sales_tender_unique_per_day"
            ),
            models.CheckConstraint(
                condition=Q(declared_amount__gte=Decimal("0")),
                name="sales_tender_is_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.sales_day} · {self.get_tender_display()}"


# ---------------------------------------------------------------------------
# The application receivable ledger — checkpoint 3 writes it, 5 reads it
# ---------------------------------------------------------------------------


class ReceivableSource(models.TextChoices):
    """
    Why a receivable entry exists. A closed set, extended with code and tests.

    Deliberately mirrors `accounting.SourceEvent`'s reasoning: the source
    identity is what stops a retried posting writing a second entry, and a
    free-text field would let one typo slip past the uniqueness guarantee whose
    whole job is to catch that retry.
    """

    SALE_POSTED = "SALE_POSTED", _("ترحيل مبيعات")
    SALE_REVERSED = "SALE_REVERSED", _("عكس مبيعات")
    SETTLEMENT = "SETTLEMENT", _("تسوية")
    SETTLEMENT_REVERSED = "SETTLEMENT_REVERSED", _("عكس تسوية")
    AUTHORIZED_ADJUSTMENT = "AUTHORIZED_ADJUSTMENT", _("تسوية معتمدة")


class ApplicationReceivableEntry(models.Model):
    """
    One movement in what a delivery application owes. **Append-only.**

    There is no balance field anywhere in this module, and this table is why.
    The balance is `SUM(debit) - SUM(credit)` over these rows, computed every
    time it is asked for. A stored balance is a number that can disagree with
    the entries that produced it, and the disagreement is discovered during a
    settlement argument — the worst possible moment, because the counterparty
    has their own figure and the restaurant can no longer explain its own
    (ADR-027 §5).

    The performance objection is real and answered rather than dismissed: the
    rows are indexed on `(organization, delivery_application, business_date)`
    and a period balance is one aggregate query. If that ever stops being
    enough the answer is a *derived and rebuildable* period summary, never an
    incrementally-maintained field.

    No `TimeStampedModel`: an append-only row has no `updated_at`, and offering
    one would invite something to write it.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="application_receivable_entries",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="application_receivable_entries",
        verbose_name=_("branch"),
    )
    delivery_application = models.ForeignKey(
        DeliveryApplication,
        on_delete=models.PROTECT,
        related_name="receivable_entries",
        verbose_name=_("delivery application"),
    )
    business_date = models.DateField(_("business date"))

    source = models.CharField(_("source"), max_length=24, choices=ReceivableSource.choices)
    #: `sales.SalesDay`, `sales.DeliveryApplicationSettlement`, and so on —
    #: the same shape the journal's own source identity uses (ADR-017).
    source_document_type = models.CharField(_("source document type"), max_length=100)
    source_document_id = models.CharField(_("source document id"), max_length=64)

    debit = models.DecimalField(
        _("debit"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )
    credit = models.DecimalField(
        _("credit"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )
    narration = models.CharField(_("narration"), max_length=300, blank=True)

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("application receivable entry")
        verbose_name_plural = _("application receivable entries")
        ordering = ["business_date", "pk"]
        indexes = [
            models.Index(
                fields=["organization", "delivery_application", "business_date"],
                name="sales_receivable_scope_idx",
            ),
        ]
        constraints = [
            # Exactly one side, and it is positive. A row with both would be
            # two facts, and a row with neither would be a fact about nothing.
            models.CheckConstraint(
                condition=(
                    Q(debit__gt=Decimal("0"), credit=Decimal("0"))
                    | Q(debit=Decimal("0"), credit__gt=Decimal("0"))
                ),
                name="sales_receivable_exactly_one_side",
            ),
            # One entry per economic event. This is what makes a retried
            # posting idempotent at the ledger rather than only at the journal.
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "delivery_application",
                    "source",
                    "source_document_type",
                    "source_document_id",
                ],
                name="sales_receivable_one_entry_per_event",
            ),
        ]

    def __str__(self) -> str:
        side = f"Dr {self.debit}" if self.debit else f"Cr {self.credit}"
        return f"{self.delivery_application.code} {self.business_date.isoformat()} {side}"

    @property
    def signed_amount(self) -> Decimal:
        """Positive increases what the application owes."""
        return self.debit - self.credit


__all__ = [
    "CALCULATION_MAX_DIGITS",
    "CODE_PATTERN",
    "DEMO_CODE_PREFIX",
    "DEMO_NOTICE",
    "MONEY_MAX_DIGITS",
    "QUANTITY_MAX_DIGITS",
    "RATE_MAX_DIGITS",
    "UNIT_PRICE_MAX_DIGITS",
    "ZERO_RATE",
    "ApplicationReceivableEntry",
    "CommissionBasis",
    "DeliveryAgreement",
    "DeliveryApplication",
    "DeliveryApplicationBranchSetting",
    "DiscountProgram",
    "FulfillmentSource",
    "MenuCategory",
    "MenuItem",
    "MenuItemBranchSetting",
    "MenuPriceVersion",
    "PriceScope",
    "ReceivableSource",
    "SalesChannel",
    "SalesChannelCategory",
    "SalesDay",
    "SalesDayLine",
    "SalesDayStatus",
    "SalesDocumentSequence",
    "SalesTenderSummary",
    "TenderDestination",
]
