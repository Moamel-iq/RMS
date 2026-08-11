"""
Procurement and accounts payable.

Task 2.1 delivers the supplier master — the one thing every later procurement
document has to name. The seven business events that follow it (request,
quotation, order, receipt, invoice, return, credit note, payment) arrive one
task at a time, each as its own aggregate, because they happen on different
dates and a single mutable "purchase" row can honestly represent at most one of
them.

See `docs/tasks/task-2-0-procurement-domain-spec.md` for the approved design
and `docs/invariants/procurement-invariants.md` for the rules these must
satisfy.
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
from apps.core.money import (
    MONEY_PLACES,
    UNIT_PRICE_PLACES,
    quantize_money,
    quantize_unit_price,
)
from apps.core.quantity import FACTOR_PLACES, QUANTITY_PLACES

#: Supplier codes. The same shape inventory item codes use, and canonicalised
#: to uppercase before storage so uniqueness is case-insensitive in effect
#: without a functional index.
CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._-]*$"

#: Room for a credit limit in IQD, a currency with large nominal amounts.
MONEY_MAX_DIGITS = MONEY_PLACES + 18
UNIT_PRICE_MAX_DIGITS = UNIT_PRICE_PLACES + 15
QUANTITY_MAX_DIGITS = QUANTITY_PLACES + 15
#: A factor is a technical identity at the same precision inventory uses
#: (ADR-006). Twelve places, because an ounce needs them.
FACTOR_MAX_DIGITS = FACTOR_PLACES + 12


class Supplier(TimeStampedModel):
    """
    Somebody the organization buys from.

    Organization-scoped, like `InventoryItem` and for the same reason: branches
    buy from the organization's suppliers, and one branch inventing its own
    supplier list would make group purchasing analysis meaningless.

    **Carries no balance field.** What is owed to a supplier is derived from
    posted invoices, credit notes and payment allocations every time it is
    asked for. A stored balance is a second source of truth, and the one that
    drifts is always the stored one. This is not an optimisation to revisit
    under load — the architecture plan names it explicitly, and the aging
    report exists to answer the question properly.

    **Carries no account foreign key.** Which payable account a supplier posts
    to is an `AccountRole` mapping (ADR-019), and a second path here would
    compete with it silently.

    `payment_terms_days` is the **default for new documents**, not a live
    lookup. Each order and invoice snapshots the terms that applied to it:
    changing a supplier's terms in March must not restate January's due dates.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="suppliers",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=32)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)

    contact_name = models.CharField(_("contact person"), max_length=200, blank=True)
    #: Canonicalised the same way a user's phone is, so the same number is the
    #: same string however it was typed. Not unique: two suppliers may share an
    #: office line, and refusing that would be refusing a real arrangement.
    phone = models.CharField(_("phone"), max_length=20, blank=True)
    email = models.EmailField(_("email"), blank=True)
    address = models.TextField(_("address"), blank=True)

    #: Zero means cash on delivery, which is the common case in this business
    #: and the reason the default is not null.
    payment_terms_days = models.PositiveSmallIntegerField(_("payment terms (days)"), default=0)
    #: NULL means "no stated limit", which is a different statement from zero.
    #: Release 1 reports against it and refuses nothing.
    credit_limit = models.DecimalField(
        _("credit limit"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )

    notes = models.TextField(_("notes"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    #: The identity a journal entry points at. Immutable, and deliberately not
    #: the primary key or the code: a code can be corrected, and the ledger has
    #: to still point at something in five years (ADR-017).
    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, editable=False, unique=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("supplier")
        verbose_name_plural = _("suppliers")
        ordering = ["organization__code", "code"]
        # `view_supplier` is deliberately absent: Django creates it as this
        # model's builtin view permission, and declaring it again is an
        # `auth.E005` clash. The codename is the one procurement checks, so it
        # is real — it simply arrives from the default set rather than from
        # here. Inventory never hit this because its model is `InventoryItem`
        # and its permission is `view_item`; the names only collide when a
        # custom codename happens to match `view_<model>`.
        permissions = [
            ("manage_suppliers", _("Can create, edit and archive suppliers")),
            ("view_supplier_cost", _("Can view supplier prices and amounts")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="procurement_supplier_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN),
                name="procurement_supplier_code_format",
            ),
            models.CheckConstraint(
                condition=~Q(name_ar=""),
                name="procurement_supplier_name_ar_not_empty",
            ),
            # A stated limit is a number the business will be compared against.
            # A negative one is not a limit, it is a typo.
            models.CheckConstraint(
                condition=Q(credit_limit__isnull=True) | Q(credit_limit__gte=0),
                name="procurement_supplier_credit_limit_not_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "is_active"], name="supplier_org_active_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


class SupplierItem(TimeStampedModel):
    """
    What one supplier calls one of our items, and on what terms.

    The catalogue answers "what do we usually pay, in what package, and how
    long does it take". It is **planning data**. It never values stock and no
    posting service reads it: inventory value comes from a receipt line's own
    price snapshot and nowhere else. That separation is the whole reason a
    price can sit here at all — a catalogue that fed valuation would silently
    reprice history every time somebody corrected a supplier's price list.

    Effective-dated and versioned, like `ItemPackageConversion` and for the
    same reason: a quotation raised in March referenced the terms that were
    live in March, and correcting them in June must not restate it.

    `supplier_sku` is stored as the supplier wrote it — stripped, never
    upper-cased. It is **their** vocabulary, and the same reasoning ADR-017
    applies to `source_document_id`: folding the case of somebody else's
    identifier is a guess about a system we do not control.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="supplier_items",
        verbose_name=_("organization"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="catalogue",
        verbose_name=_("supplier"),
    )
    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="supplier_catalogue",
        verbose_name=_("item"),
    )

    supplier_sku = models.CharField(_("supplier reference"), max_length=64, blank=True)
    supplier_description = models.CharField(_("supplier description"), max_length=200, blank=True)

    #: NULL means the item is bought in its own base unit. A package must be
    #: one the *item* has a conversion for, because a receipt has to snapshot
    #: a factor and there would be none.
    package_unit = models.ForeignKey(
        "inventory.PackageUnit",
        on_delete=models.PROTECT,
        related_name="supplier_items",
        null=True,
        blank=True,
        verbose_name=_("purchase package"),
    )

    #: Informational. PRC-005: no posting service may read this field, and an
    #: architectural test proves none does.
    last_quoted_price = models.DecimalField(
        _("last quoted price"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
        null=True,
        blank=True,
        help_text=_("Planning information only. Never used to value stock."),
    )
    lead_time_days = models.PositiveSmallIntegerField(_("lead time (days)"), null=True, blank=True)
    #: In the purchase package where one is named, otherwise in base units.
    minimum_order_quantity = models.DecimalField(
        _("minimum order quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )

    #: At most one preferred row per item across every supplier — enforced by a
    #: partial unique index, so "who do we normally buy this from" has exactly
    #: one answer at a time.
    is_preferred = models.BooleanField(_("preferred"), default=False)

    effective_from = models.DateField(_("effective from"))
    effective_to = models.DateField(_("effective to"), null=True, blank=True)

    #: Incremented per (supplier, item, package) whenever terms are superseded.
    version = models.PositiveIntegerField(_("version"), default=1)
    is_active = models.BooleanField(_("active"), default=True)
    notes = models.TextField(_("notes"), blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("supplier item")
        verbose_name_plural = _("supplier items")
        ordering = ["supplier__code", "item__code", "-effective_from"]
        permissions = [
            ("manage_supplier_items", _("Can maintain the supplier item catalogue")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="procurement_supplier_item_period_is_ordered",
            ),
            models.CheckConstraint(
                condition=Q(last_quoted_price__isnull=True) | Q(last_quoted_price__gte=0),
                name="procurement_supplier_item_price_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(minimum_order_quantity__isnull=True) | Q(minimum_order_quantity__gt=0),
                name="procurement_supplier_item_minimum_is_positive",
            ),
            # One version of one supplier's terms for one item and package.
            # `nulls_distinct=False` because a NULL package means "base units",
            # which is one answer and not unlimited ones.
            models.UniqueConstraint(
                fields=["supplier", "item", "package_unit", "version"],
                name="procurement_supplier_item_version_unique",
                nulls_distinct=False,
            ),
            # One preferred source per item, among active rows.
            models.UniqueConstraint(
                fields=["organization", "item"],
                condition=Q(is_preferred=True, is_active=True),
                name="procurement_supplier_item_one_preferred_per_item",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "is_active"], name="supplier_item_org_active_idx"),
            models.Index(fields=["item", "is_active"], name="supplier_item_item_idx"),
        ]

    def __str__(self) -> str:
        package = self.package_unit.code if self.package_unit else "—"
        return f"{self.supplier.code} · {self.item.code} · {package}"


class ProcurementDocumentSequence(models.Model):
    """
    The gapless per-organization, per-type, per-year counter.

    A second table rather than inventory's, because the two modules number
    different vocabularies: `PR` and `PO` are not inventory document types and
    keying them into an inventory enum would make the enum a lie. The counting
    *rule* is four lines under a row lock and is deliberately identical — what
    must never be duplicated is a sequence a document could draw from twice.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="procurement_sequences",
        verbose_name=_("organization"),
    )
    document_type = models.CharField(_("document type"), max_length=32)
    year = models.PositiveSmallIntegerField(_("year"))
    last_number = models.PositiveIntegerField(_("last number"), default=0)

    class Meta:
        verbose_name = _("procurement document sequence")
        verbose_name_plural = _("procurement document sequences")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "document_type", "year"],
                name="procurement_sequence_unique_per_type_and_year",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.code} {self.document_type} {self.year}: {self.last_number}"


class PurchaseRequestStatus(models.TextChoices):
    """
    What a request is, and what may still happen to it.

    Three terminal states rather than one. `REJECTED` is somebody refusing the
    need; `CANCELLED` is the requester withdrawing it; and the difference
    matters to whoever later asks why a branch never got what it asked for.
    """

    DRAFT = "DRAFT", _("مسودة")
    SUBMITTED = "SUBMITTED", _("مُرسل")
    APPROVED = "APPROVED", _("معتمد")
    REJECTED = "REJECTED", _("مرفوض")
    CANCELLED = "CANCELLED", _("ملغى")


class PurchaseRequest(TimeStampedModel):
    """
    What a branch says it needs, and nothing more.

    **No inventory effect and no accounting effect, in any status.** A request
    is a statement of need: approving one commits nobody to anything and moves
    no goods and no money. That is the whole reason it is a separate document
    from the order — the two happen on different days, are decided by different
    people, and only one of them is a commercial commitment.

    Identity is `public_id`, immutable from birth. The human `number` is
    presentation and is assigned at **submission**, so a draft that is
    abandoned cannot burn a number out of a gapless sequence.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="purchase_requests",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="purchase_requests",
        verbose_name=_("branch"),
    )
    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    number = models.CharField(_("number"), max_length=32, blank=True)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=PurchaseRequestStatus.choices,
        default=PurchaseRequestStatus.DRAFT,
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="purchase_requests",
        verbose_name=_("requested by"),
    )
    #: Where the goods are wanted. Validated against the caller's warehouse
    #: scope, so a request cannot name a store somebody cannot reach.
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        related_name="purchase_requests",
        verbose_name=_("destination warehouse"),
    )
    location = models.ForeignKey(
        "inventory.StockLocation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_requests",
        verbose_name=_("destination location"),
    )
    required_date = models.DateField(_("required by"))
    purpose = models.CharField(_("purpose"), max_length=200)
    notes = models.TextField(_("notes"), blank=True)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submitted_purchase_requests",
        verbose_name=_("submitted by"),
    )
    submitted_at = models.DateTimeField(_("submitted at"), null=True, blank=True)
    #: The one actor for approve, reject **and** cancel. One pair of columns
    #: rather than three: the maker-checker constraint has to compare against a
    #: single field, and three nullable pairs would let two of them be set at
    #: once and mean nothing.
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="decided_purchase_requests",
        verbose_name=_("decided by"),
    )
    decided_at = models.DateTimeField(_("decided at"), null=True, blank=True)
    decision_reason = models.TextField(_("decision reason"), blank=True)

    #: Which offer was chosen, and why. On the request rather than on a
    #: separate aggregate because the decision belongs to the need: one request
    #: is answered by several quotations and exactly one of them wins. A
    #: dedicated award model would add a row whose only content is a foreign
    #: key to the winner.
    #:
    #: Awarding creates no stock, no journal, no payable and no GRNI. The
    #: purchase order raised from it is the commitment.
    awarded_quotation = models.ForeignKey(
        "procurement.SupplierQuotation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="awarded_for",
        verbose_name=_("awarded quotation"),
    )
    awarded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="awarded_purchase_requests",
        verbose_name=_("awarded by"),
    )
    awarded_at = models.DateTimeField(_("awarded at"), null=True, blank=True)
    award_reason = models.TextField(_("award reason"), blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("purchase request")
        verbose_name_plural = _("purchase requests")
        ordering = ["-submitted_at", "-id"]
        permissions = [
            ("create_purchase_request", _("Can prepare and submit a purchase request")),
            ("approve_purchase_request", _("Can approve or reject a purchase request")),
        ]
        constraints = [
            # Maker-checker, at the database and not only in the service. A
            # service check is a promise; this survives a data fix applied at
            # two in the morning through a shell.
            models.CheckConstraint(
                condition=Q(decided_by__isnull=True) | ~Q(decided_by=models.F("submitted_by")),
                name="procurement_request_approver_is_not_the_submitter",
            ),
            # A decided request names who decided it and when. Half a decision
            # is not a state this document has.
            models.CheckConstraint(
                condition=Q(decided_by__isnull=True, decided_at__isnull=True)
                | Q(decided_by__isnull=False, decided_at__isnull=False),
                name="procurement_request_decision_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(submitted_by__isnull=True, submitted_at__isnull=True)
                | Q(submitted_by__isnull=False, submitted_at__isnull=False),
                name="procurement_request_submission_is_complete",
            ),
            # A refusal or a withdrawal has to say why. An approval need not.
            models.CheckConstraint(
                condition=~Q(status__in=["REJECTED", "CANCELLED"]) | ~Q(decision_reason=""),
                name="procurement_request_refusal_states_a_reason",
            ),
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="procurement_request_number_unique_per_organization",
            ),
            # An award is a complete statement or none of one: which offer,
            # decided by whom, when, and why. Three of the four with the
            # fourth missing is not a decision anybody can re-read.
            models.CheckConstraint(
                condition=Q(
                    awarded_quotation__isnull=True,
                    awarded_by__isnull=True,
                    awarded_at__isnull=True,
                    award_reason="",
                )
                | Q(
                    awarded_quotation__isnull=False,
                    awarded_by__isnull=False,
                    awarded_at__isnull=False,
                )
                & ~Q(award_reason=""),
                name="procurement_request_award_is_complete_or_absent",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="pr_org_status_idx"),
            models.Index(fields=["branch", "status"], name="pr_branch_status_idx"),
        ]

    def __str__(self) -> str:
        return self.number or f"PR draft {self.public_id}"

    @property
    def is_editable(self) -> bool:
        """Only a draft. A submitted request is frozen — PRC-011."""
        return self.status == PurchaseRequestStatus.DRAFT


class PurchaseRequestLine(TimeStampedModel):
    """
    One item somebody wants, in the unit they think in.

    Carries the same conversion snapshot a posted movement does — factor,
    version, entered unit, base quantity — even though nothing here posts.
    A request approved in March against a 30 kg sack must still mean 30 kg in
    June when the sack is redefined, or the order raised from it would quietly
    buy a different amount from the one that was approved.
    """

    request = models.ForeignKey(
        PurchaseRequest,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("request"),
    )
    #: Stable per line and never renumbered, so an allocation or an order line
    #: can name it for the life of the document.
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))

    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="purchase_request_lines",
        verbose_name=_("item"),
    )
    package_unit = models.ForeignKey(
        "inventory.PackageUnit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_request_lines",
        verbose_name=_("package"),
    )
    #: The conversion the base quantity was derived with, and its version.
    #: Null when the line is entered in the item's own base unit.
    conversion = models.ForeignKey(
        "inventory.ItemPackageConversion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_request_lines",
        verbose_name=_("conversion"),
    )
    conversion_version = models.PositiveIntegerField(_("conversion version"), null=True, blank=True)
    conversion_factor = models.DecimalField(
        _("factor to base"),
        max_digits=FACTOR_MAX_DIGITS,
        decimal_places=FACTOR_PLACES,
        null=True,
        blank=True,
    )

    entered_quantity = models.DecimalField(
        _("quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    base_quantity = models.DecimalField(
        _("base quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )

    #: Where the requester expects it to come from. Advisory: the order decides.
    preferred_supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="requested_lines",
        verbose_name=_("preferred supplier"),
    )
    note = models.CharField(_("note"), max_length=200, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("purchase request line")
        verbose_name_plural = _("purchase request lines")
        ordering = ["request", "sequence"]
        constraints = [
            models.CheckConstraint(
                condition=Q(entered_quantity__gt=0),
                name="procurement_request_line_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(base_quantity__gt=0),
                name="procurement_request_line_base_quantity_is_positive",
            ),
            # A package means a conversion. Without one there is no factor, and
            # the base quantity would be a number nobody can retrace.
            models.CheckConstraint(
                condition=Q(package_unit__isnull=True, conversion__isnull=True)
                | Q(package_unit__isnull=False, conversion__isnull=False),
                name="procurement_request_line_package_carries_its_conversion",
            ),
            models.UniqueConstraint(
                fields=["request", "sequence"],
                name="procurement_request_line_sequence_unique",
            ),
            models.UniqueConstraint(
                fields=["request", "item", "package_unit"],
                nulls_distinct=False,
                name="procurement_request_line_item_appears_once",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.request} · {self.item.code} × {self.entered_quantity}"


class SupplierQuotationStatus(models.TextChoices):
    """
    What a quotation is.

    `EXPIRED` is a status somebody sets, not a date arithmetic result. A
    quotation past its validity is still readable and still comparable as
    history; marking it expired is a decision that it will not be used, and the
    award service refuses an out-of-date quotation whether or not anybody got
    round to setting the flag.
    """

    DRAFT = "DRAFT", _("مسودة")
    SUBMITTED = "SUBMITTED", _("مُستلم")
    AWARDED = "AWARDED", _("مُرسى")
    DECLINED = "DECLINED", _("مستبعد")
    EXPIRED = "EXPIRED", _("منتهي")


class SupplierQuotation(TimeStampedModel):
    """
    What a supplier says something will cost.

    Evidence, not a commitment: no stock, no journal, no payable, in any
    status — including `AWARDED`. Awarding records which offer was chosen and
    why; the commercial commitment is the purchase order, and keeping the two
    apart is what lets a buyer change their mind after choosing without
    anything having to be unwound.

    Freight and other charges sit on the document rather than on the lines
    because that is how suppliers quote them: one delivery charge for the
    whole order. Comparison spreads them across the lines to reach a landed
    unit price (PRC-015) without ever storing the spread — a stored allocation
    would be a second figure to disagree with the quoted one.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="supplier_quotations",
        verbose_name=_("organization"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="quotations",
        verbose_name=_("supplier"),
    )
    #: The request this answers, where there is one. Nullable: a buyer may ask
    #: for a price before anybody raises a formal request, and refusing to
    #: record that would push the number into somebody's notebook.
    request = models.ForeignKey(
        PurchaseRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="quotations",
        verbose_name=_("purchase request"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    number = models.CharField(_("number"), max_length=32, blank=True)
    #: The supplier's own quotation reference, stored as they wrote it. Unique
    #: per supplier so the same offer cannot be entered twice — the cheapest
    #: possible protection against comparing a supplier against themselves.
    supplier_reference = models.CharField(_("supplier reference"), max_length=64, blank=True)

    quoted_at = models.DateField(_("quoted on"))
    valid_until = models.DateField(_("valid until"), null=True, blank=True)

    freight_amount = models.DecimalField(
        _("freight"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0.000"),
    )
    other_charges = models.DecimalField(
        _("other charges"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0.000"),
    )

    #: Where the paper, PDF or message lives. Required on submission: a price
    #: nobody can trace to something the supplier actually sent is a rumour,
    #: and the same argument opening stock makes about its count sheet.
    evidence_reference = models.CharField(_("evidence reference"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=SupplierQuotationStatus.choices,
        default=SupplierQuotationStatus.DRAFT,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_quotations",
        verbose_name=_("recorded by"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("supplier quotation")
        verbose_name_plural = _("supplier quotations")
        ordering = ["-quoted_at", "-id"]
        permissions = [
            ("manage_quotations", _("Can record and submit supplier quotations")),
            ("award_quotation", _("Can award a quotation and record the reason")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(freight_amount__gte=0) & Q(other_charges__gte=0),
                name="procurement_quotation_charges_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gte=models.F("quoted_at")),
                name="procurement_quotation_validity_is_ordered",
            ),
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="procurement_quotation_number_unique_per_organization",
            ),
            # The same offer cannot be entered twice against one supplier.
            models.UniqueConstraint(
                fields=["supplier", "supplier_reference"],
                condition=~Q(supplier_reference=""),
                name="procurement_quotation_reference_unique_per_supplier",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="quotation_org_status_idx"),
            models.Index(fields=["request"], name="quotation_request_idx"),
        ]

    def __str__(self) -> str:
        return self.number or f"quotation {self.public_id}"

    @property
    def is_editable(self) -> bool:
        return self.status == SupplierQuotationStatus.DRAFT

    @property
    def line_total(self) -> Decimal:
        """
        The sum of the posted lines, and never anything else.

        A document total is the SUM of its lines — never rounded independently
        of them (ADR-012). Derived rather than stored for exactly that reason:
        a stored total is a second number that can disagree with the lines
        under it.
        """
        total = sum((line.line_total for line in self.lines.all()), start=Decimal("0.000"))
        return quantize_money(total)

    @property
    def total_amount(self) -> Decimal:
        return quantize_money(self.line_total + self.freight_amount + self.other_charges)


class SupplierQuotationLine(TimeStampedModel):
    """
    One priced item, in the package the supplier quoted it in.

    Carries the conversion snapshot for the same reason a request line does:
    the comparison in Task 2.5 normalises to base units, and a factor that
    changed between quotation and comparison would silently change which
    supplier looked cheaper.
    """

    quotation = models.ForeignKey(
        SupplierQuotation,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("quotation"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))

    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="quotation_lines",
        verbose_name=_("item"),
    )
    #: The catalogue row this price came from, where one exists. Informational:
    #: the price on the line is what the supplier quoted, and the catalogue is
    #: never consulted to value anything (PRC-005).
    supplier_item = models.ForeignKey(
        SupplierItem,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="quotation_lines",
        verbose_name=_("catalogue row"),
    )
    package_unit = models.ForeignKey(
        "inventory.PackageUnit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="quotation_lines",
        verbose_name=_("package"),
    )
    conversion = models.ForeignKey(
        "inventory.ItemPackageConversion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="quotation_lines",
        verbose_name=_("conversion"),
    )
    conversion_version = models.PositiveIntegerField(_("conversion version"), null=True, blank=True)
    conversion_factor = models.DecimalField(
        _("factor to base"),
        max_digits=FACTOR_MAX_DIGITS,
        decimal_places=FACTOR_PLACES,
        null=True,
        blank=True,
    )

    quantity = models.DecimalField(
        _("quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    base_quantity = models.DecimalField(
        _("base quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    #: Per **entered** unit — per sack, not per kilogram. The comparison
    #: derives the base unit price; storing both would be two numbers that can
    #: disagree after a factor is corrected.
    unit_price = models.DecimalField(
        _("unit price"), max_digits=UNIT_PRICE_MAX_DIGITS, decimal_places=UNIT_PRICE_PLACES
    )
    line_total = models.DecimalField(
        _("line total"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    note = models.CharField(_("note"), max_length=200, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("supplier quotation line")
        verbose_name_plural = _("supplier quotation lines")
        ordering = ["quotation", "sequence"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="procurement_quotation_line_quantity_positive"
            ),
            models.CheckConstraint(
                condition=Q(base_quantity__gt=0),
                name="procurement_quotation_line_base_quantity_positive",
            ),
            # Zero is a legitimate quoted price — a free sample, a promotional
            # line — and negative is not a price at all.
            models.CheckConstraint(
                condition=Q(unit_price__gte=0),
                name="procurement_quotation_line_price_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(line_total__gte=0),
                name="procurement_quotation_line_total_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(package_unit__isnull=True, conversion__isnull=True)
                | Q(package_unit__isnull=False, conversion__isnull=False),
                name="procurement_quotation_line_package_carries_its_conversion",
            ),
            models.UniqueConstraint(
                fields=["quotation", "sequence"],
                name="procurement_quotation_line_sequence_unique",
            ),
            models.UniqueConstraint(
                fields=["quotation", "item", "package_unit"],
                nulls_distinct=False,
                name="procurement_quotation_line_item_appears_once",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.quotation} · {self.item.code}"

    @property
    def base_unit_price(self) -> Decimal:
        """
        What one base unit costs, before freight.

        Derived on read at full precision, never stored. This is the only
        figure two suppliers quoting different package sizes can honestly be
        compared on, and Task 2.5 adds the freight share on top of it.
        """
        return quantize_unit_price(self.line_total / self.base_quantity)
