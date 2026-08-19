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
from apps.core.quantity import FACTOR_PLACES, QUANTITY_PLACES, quantize_quantity

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
            # Task 2.16. Declared here because the report family has no model
            # of its own, and the supplier is the entity every report reads.
            ("view_procurement_report", _("Can view procurement reports")),
            # Task 2.17. Spec §13. Declared on the supplier for the same
            # reason: the batches live on inventory's ImportBatch, and what
            # these authorize reshaping is this module's masters.
            ("import_supplier", _("Can import suppliers")),
            ("import_supplier_item", _("Can import the supplier catalogue")),
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


class SupplierCreditTermStatus(models.TextChoices):
    """Lifecycle of one effective-dated supplier credit-term version."""

    DRAFT = "DRAFT", _("مسودة")
    ACTIVE = "ACTIVE", _("فعّال")
    SUPERSEDED = "SUPERSEDED", _("مستبدل")


class SupplierCreditTerm(TimeStampedModel):
    """
    One version of the commercial due-date agreement with a supplier.

    `Supplier.payment_terms_days` is retained as a compatibility projection for
    older integrations and list screens. It is written by the activation
    service only; this row is the effective-dated source used by invoice
    approval. Posted and approved invoices keep their own immutable snapshots.
    """

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="supplier_credit_terms",
        verbose_name=_("organization"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="credit_terms",
        verbose_name=_("supplier"),
    )
    version = models.PositiveIntegerField(_("version"))
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=SupplierCreditTermStatus.choices,
        default=SupplierCreditTermStatus.DRAFT,
    )
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)
    net_days = models.PositiveSmallIntegerField(_("net days"), default=0)
    effective_from = models.DateField(_("effective from"))
    effective_to = models.DateField(_("effective to"), null=True, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_supplier_credit_terms",
        verbose_name=_("created by"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_supplier_credit_terms",
        verbose_name=_("approved by"),
    )
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    supersedes = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replacement",
        verbose_name=_("supersedes"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("supplier credit term")
        verbose_name_plural = _("supplier credit terms")
        ordering = ["supplier__code", "-version"]
        permissions = [
            ("create_supplier_credit_term", _("Can draft supplier credit terms")),
            ("approve_supplier_credit_term", _("Can activate supplier credit terms")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["supplier", "version"],
                name="procurement_credit_term_version_unique",
            ),
            models.UniqueConstraint(
                fields=["supplier"],
                condition=Q(status="DRAFT"),
                name="procurement_credit_term_one_draft_per_supplier",
            ),
            models.CheckConstraint(
                condition=~Q(name_ar=""),
                name="procurement_credit_term_name_ar_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="procurement_credit_term_period_is_ordered",
            ),
            models.CheckConstraint(
                condition=Q(approved_by__isnull=True, approved_at__isnull=True)
                | Q(approved_by__isnull=False, approved_at__isnull=False),
                name="procurement_credit_term_approval_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(created_by__isnull=True)
                | Q(approved_by__isnull=True)
                | ~Q(created_by=models.F("approved_by")),
                name="procurement_credit_term_maker_checker",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "status", "effective_from"],
                name="credit_term_org_status_idx",
            ),
            models.Index(
                fields=["supplier", "effective_from", "effective_to"],
                name="credit_term_supplier_date_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.supplier.code} · v{self.version} · {self.name_ar}"


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


class PurchaseOrderStatus(models.TextChoices):
    """
    A commitment, and how far along it is.

    `APPROVED` and `ISSUED` are separate because they are separate acts by
    separate people: somebody inside the business agrees to spend the money,
    and somebody sends the order to the supplier. Collapsing them would lose
    the moment the commitment actually left the building, which is the moment
    it stops being reversible by a decision alone.
    """

    DRAFT = "DRAFT", _("مسودة")
    APPROVED = "APPROVED", _("معتمد")
    ISSUED = "ISSUED", _("مُرسل للمورد")
    CANCELLED = "CANCELLED", _("ملغى")


class PurchaseOrder(TimeStampedModel):
    """
    What was agreed with a supplier, and at what price.

    **Creates no stock and no payable, in any status including `ISSUED`.** An
    order is a commitment, and a commitment is not a liability under the
    accounting policy this system implements: nothing is owed until goods
    arrive and an invoice states an amount. An order placed and never
    delivered leaves the books exactly as it found them, and open orders are
    reported rather than posted.

    Terms are **snapshotted**, not looked up. `payment_terms_days` is copied
    from the supplier when the order is raised, so renegotiating a supplier's
    terms in March cannot restate the due date of an order placed in January.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="purchase_orders",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="purchase_orders",
        verbose_name=_("branch"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="purchase_orders",
        verbose_name=_("supplier"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    number = models.CharField(_("number"), max_length=32, blank=True)

    #: Where this came from, when it came from anywhere. Both nullable: buying
    #: meat from the market without a formal request first is normal for this
    #: business, and forcing a request would produce fictional ones written
    #: after the fact.
    request = models.ForeignKey(
        PurchaseRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_orders",
        verbose_name=_("purchase request"),
    )
    quotation = models.ForeignKey(
        SupplierQuotation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_orders",
        verbose_name=_("awarded quotation"),
    )

    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        related_name="purchase_orders",
        verbose_name=_("destination warehouse"),
    )
    location = models.ForeignKey(
        "inventory.StockLocation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_orders",
        verbose_name=_("destination location"),
    )

    ordered_on = models.DateField(_("order date"))
    expected_on = models.DateField(_("expected delivery"), null=True, blank=True)
    #: Copied from the supplier at creation. See the class docstring.
    payment_terms_days = models.PositiveSmallIntegerField(_("payment terms (days)"), default=0)
    supplier_reference = models.CharField(_("supplier reference"), max_length=64, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=PurchaseOrderStatus.choices,
        default=PurchaseOrderStatus.DRAFT,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_purchase_orders",
        verbose_name=_("created by"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_purchase_orders",
        verbose_name=_("approved by"),
    )
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="issued_purchase_orders",
        verbose_name=_("issued by"),
    )
    issued_at = models.DateTimeField(_("issued at"), null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cancelled_purchase_orders",
        verbose_name=_("cancelled by"),
    )
    cancelled_at = models.DateTimeField(_("cancelled at"), null=True, blank=True)
    cancellation_reason = models.TextField(_("cancellation reason"), blank=True)

    #: 1 until the first revision. The live row is always the current version;
    #: every earlier one is a `PurchaseOrderVersion` snapshot.
    version = models.PositiveIntegerField(_("version"), default=1)
    revised_at = models.DateTimeField(_("last revised at"), null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("purchase order")
        verbose_name_plural = _("purchase orders")
        ordering = ["-ordered_on", "-id"]
        permissions = [
            ("create_purchase_order", _("Can prepare a purchase order")),
            ("approve_purchase_order", _("Can approve a purchase order")),
            ("issue_purchase_order", _("Can issue a purchase order to a supplier")),
            ("cancel_purchase_order", _("Can cancel a purchase order")),
        ]
        constraints = [
            # Maker-checker on the money commitment: whoever prepared the order
            # is not the person who agrees to spend it.
            models.CheckConstraint(
                condition=Q(approved_by__isnull=True) | ~Q(approved_by=models.F("created_by")),
                name="procurement_order_approver_is_not_the_preparer",
            ),
            models.CheckConstraint(
                condition=Q(approved_by__isnull=True, approved_at__isnull=True)
                | Q(approved_by__isnull=False, approved_at__isnull=False),
                name="procurement_order_approval_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(issued_by__isnull=True, issued_at__isnull=True)
                | Q(issued_by__isnull=False, issued_at__isnull=False),
                name="procurement_order_issue_is_complete",
            ),
            models.CheckConstraint(
                condition=~Q(status="CANCELLED") | ~Q(cancellation_reason=""),
                name="procurement_order_cancellation_states_a_reason",
            ),
            models.CheckConstraint(
                condition=Q(expected_on__isnull=True) | Q(expected_on__gte=models.F("ordered_on")),
                name="procurement_order_expected_is_not_before_ordered",
            ),
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="procurement_order_number_unique_per_organization",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="po_org_status_idx"),
            models.Index(fields=["supplier", "status"], name="po_supplier_status_idx"),
        ]

    def __str__(self) -> str:
        return self.number or f"PO draft {self.public_id}"

    @property
    def is_editable(self) -> bool:
        """Only a draft. Issued terms are what the supplier was told."""
        return self.status == PurchaseOrderStatus.DRAFT

    @property
    def total_amount(self) -> Decimal:
        """The sum of the posted lines, derived and never stored (ADR-012)."""
        return quantize_money(
            sum((line.line_total for line in self.lines.all()), start=Decimal("0.000"))
        )


class PurchaseOrderLine(TimeStampedModel):
    """
    One agreed item, at one agreed price.

    Carries the conversion snapshot for the reason every procurement line
    does: a receipt against this order will convert the delivered package to
    base units, and it must use the factor that was agreed rather than the one
    in force on the day the lorry arrives.
    """

    order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("order"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))

    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="purchase_order_lines",
        verbose_name=_("item"),
    )
    supplier_item = models.ForeignKey(
        SupplierItem,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="order_lines",
        verbose_name=_("catalogue row"),
    )
    package_unit = models.ForeignKey(
        "inventory.PackageUnit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_order_lines",
        verbose_name=_("package"),
    )
    conversion = models.ForeignKey(
        "inventory.ItemPackageConversion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_order_lines",
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

    ordered_quantity = models.DecimalField(
        _("ordered quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    ordered_base_quantity = models.DecimalField(
        _("ordered base quantity"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
    )
    #: Per entered unit, as quoted. The base unit price is derived where it is
    #: needed and never stored alongside this one.
    unit_price = models.DecimalField(
        _("agreed unit price"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
    )
    line_total = models.DecimalField(
        _("line total"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    note = models.CharField(_("note"), max_length=200, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("purchase order line")
        verbose_name_plural = _("purchase order lines")
        ordering = ["order", "sequence"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ordered_quantity__gt=0),
                name="procurement_order_line_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(ordered_base_quantity__gt=0),
                name="procurement_order_line_base_quantity_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0),
                name="procurement_order_line_price_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(package_unit__isnull=True, conversion__isnull=True)
                | Q(package_unit__isnull=False, conversion__isnull=False),
                name="procurement_order_line_package_carries_its_conversion",
            ),
            models.UniqueConstraint(
                fields=["order", "sequence"],
                name="procurement_order_line_sequence_unique",
            ),
            models.UniqueConstraint(
                fields=["order", "item", "package_unit"],
                nulls_distinct=False,
                name="procurement_order_line_item_appears_once",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.order} · {self.item.code} × {self.ordered_quantity}"


class PurchaseOrderVersion(TimeStampedModel):
    """
    What an order said before it was revised.

    An issued order is a statement somebody outside the business has been
    given, so it is never edited in place. A revision copies the current
    header and lines into one of these rows, then changes the live order — so
    the live row is always what is true now, and every version before it stays
    readable exactly as the supplier received it.

    Lines are held as JSON rather than as a second line table. They are a
    frozen photograph and nothing will ever join to them, query across them or
    reconcile against them; a parallel `PurchaseOrderVersionLine` model would
    add foreign keys that make an item or a package undeletable for the sake of
    a snapshot nobody navigates.
    """

    order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="versions",
        verbose_name=_("order"),
    )
    #: 1 is what the order said when it was first approved. The live row is
    #: always `order.version`, which is this plus one after each revision.
    version = models.PositiveIntegerField(_("version"))

    #: The header as it stood, keyed by field name. JSON because it is read as
    #: a whole and never filtered on.
    header = models.JSONField(_("header snapshot"), default=dict)
    lines = models.JSONField(_("line snapshot"), default=list)

    reason = models.TextField(_("revision reason"))
    revised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="purchase_order_revisions",
        verbose_name=_("revised by"),
    )
    revised_at = models.DateTimeField(_("revised at"))

    class Meta:
        verbose_name = _("purchase order version")
        verbose_name_plural = _("purchase order versions")
        ordering = ["order", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "version"],
                name="procurement_order_version_unique",
            ),
            models.CheckConstraint(
                condition=~Q(reason=""),
                name="procurement_order_version_states_a_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.order} v{self.version}"


class GoodsReceiptStatus(models.TextChoices):
    """
    Draft, posted, reversed — the three the specification names, and no more.

    There is deliberately no `INSPECTED` or `READY` state. Task 2.0 §7 puts
    inspection on the *line* (`accepted`, `rejected`, `rejection_reason`,
    `inspected_by`), not in the status, and adding a fourth state would invent
    a lifecycle the specification does not have. Whether a draft is complete
    enough to post is derived — see `GoodsReceipt.is_ready_to_post` — because a
    stored readiness flag would be a second opinion that goes stale the moment
    somebody edits a line.

    Task 2.9 reached `POSTED` and `REVERSED` through one command each, and
    both commit the stock effect and the GRNI journal in a single transaction.
    There is still no stock-only post: `apps.procurement.posting` is the only
    module that writes either status, and it never calls the kernel twice.
    """

    DRAFT = "DRAFT", _("مسودة")
    POSTED = "POSTED", _("مرحّل")
    REVERSED = "REVERSED", _("معكوس")


class QualityResult(models.TextChoices):
    """What inspection concluded about a line."""

    NOT_INSPECTED = "NOT_INSPECTED", _("لم يُفحص")
    ACCEPTED = "ACCEPTED", _("مقبول")
    PARTIAL = "PARTIAL", _("مقبول جزئياً")
    REJECTED = "REJECTED", _("مرفوض")


class GoodsReceipt(TimeStampedModel):
    """
    What physically arrived, and what inspection made of it.

    The inspection fields live here rather than on a later document, and that
    placement is the point of the whole model. Only the accepted quantity ever
    enters stock; rejected goods are recorded so the supplier can be argued
    with and so quality can be reported on, and they post nothing at all
    (PRC-025). Putting acceptance on the invoice instead — which is the common
    shortcut — means stock has already moved by the time anybody says the meat
    was off.

    A receipt may name no purchase order. Buying from the market without one is
    normal for this business (PRC-028). What it may not do is exist without a
    price: value with no number is how zero-cost stock gets created.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="goods_receipts",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="goods_receipts",
        verbose_name=_("branch"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="goods_receipts",
        verbose_name=_("supplier"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    number = models.CharField(_("number"), max_length=32, blank=True)

    order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipts",
        verbose_name=_("purchase order"),
    )
    #: The order's version at the moment goods were received. A later revision
    #: must not change what this delivery was measured against, so the number
    #: is copied rather than joined to.
    order_version = models.PositiveIntegerField(_("order version"), null=True, blank=True)

    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        related_name="goods_receipts",
        verbose_name=_("warehouse"),
    )
    location = models.ForeignKey(
        "inventory.StockLocation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipts",
        verbose_name=_("location"),
    )

    #: The branch business date, derived through the branch timezone and
    #: operating-day cutoff (ADR-008) — never `date(timestamp)`.
    received_at = models.DateField(_("business date"))
    #: The wall-clock moment the lorry arrived, which is a different fact.
    delivered_at = models.DateTimeField(_("delivered at"), null=True, blank=True)

    #: The supplier's own note number. Their vocabulary, stored as written.
    delivery_reference = models.CharField(_("delivery reference"), max_length=64, blank=True)
    evidence_reference = models.CharField(_("evidence reference"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=GoodsReceiptStatus.choices,
        default=GoodsReceiptStatus.DRAFT,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_goods_receipts",
        verbose_name=_("created by"),
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="received_goods_receipts",
        verbose_name=_("received by"),
    )
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inspected_goods_receipts",
        verbose_name=_("inspected by"),
    )
    inspected_at = models.DateTimeField(_("inspected at"), null=True, blank=True)

    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_goods_receipts",
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_goods_receipts",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    #: The branch settings that were in force when this receipt posted, stored
    #: so the business date it claims can be re-derived exactly. `received_at`
    #: is entered rather than computed — a lorry arrives on a day somebody
    #: knows — but which day that *is* depends on the branch timezone and
    #: operating-day cutoff, and a cutoff changed later must not restate which
    #: period a posted receipt belongs to (ADR-008).
    business_date_timezone = models.CharField(_("business timezone"), max_length=64, blank=True)
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)

    #: What actually posted: the stock side, the accounting side, and — after a
    #: reversal — the journal that took it back. A posted receipt without all
    #: three of the first two is a half-applied transaction, and the database
    #: refuses that shape outright.
    stock_entry = models.ForeignKey(
        "inventory.StockLedgerEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipts",
        verbose_name=_("stock entry"),
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipts",
        verbose_name=_("journal entry"),
    )
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_goods_receipts",
        verbose_name=_("reversal journal entry"),
    )

    #: The sum of the posted line values, stored once at posting. Derived
    #: figures answer "what is it worth now"; this one answers "what did it
    #: post", which is the question reconciliation asks and the only one a
    #: reversal may act on (ADR-012: a total is the sum of its lines).
    posted_value = models.DecimalField(
        _("posted value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("goods receipt")
        verbose_name_plural = _("goods receipts")
        ordering = ["-received_at", "-id"]
        permissions = [
            ("create_goods_receipt", _("Can record a goods receipt")),
            ("inspect_goods_receipt", _("Can accept or reject received goods")),
            ("post_goods_receipt", _("Can post a goods receipt to stock")),
            ("reverse_goods_receipt", _("Can reverse a posted goods receipt")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(inspected_by__isnull=True, inspected_at__isnull=True)
                | Q(inspected_by__isnull=False, inspected_at__isnull=False),
                name="procurement_receipt_inspection_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(posted_by__isnull=True, posted_at__isnull=True)
                | Q(posted_by__isnull=False, posted_at__isnull=False),
                name="procurement_receipt_posting_is_complete",
            ),
            # A draft has never posted. Catches a posting service that sets a
            # timestamp without moving the status, which is exactly the shape
            # of a half-applied transaction.
            models.CheckConstraint(
                condition=~Q(status="DRAFT") | Q(posted_at__isnull=True),
                name="procurement_receipt_draft_has_not_posted",
            ),
            models.CheckConstraint(
                condition=~Q(status="POSTED") | Q(posted_at__isnull=False),
                name="procurement_receipt_posted_says_when",
            ),
            # A posted receipt names both ledgers it reached and what it was
            # worth. Together with the trigger that freezes the row, this is
            # what makes "stock without accounting" unrepresentable rather
            # than merely unwritten: the shape does not exist in the table.
            models.CheckConstraint(
                condition=~Q(status__in=["POSTED", "REVERSED"])
                | Q(
                    stock_entry__isnull=False,
                    journal_entry__isnull=False,
                    posted_value__isnull=False,
                    business_date_timezone__gt="",
                    business_day_start__isnull=False,
                ),
                name="procurement_receipt_posted_names_both_ledgers",
            ),
            # A draft has reached neither.
            models.CheckConstraint(
                condition=~Q(status="DRAFT")
                | Q(stock_entry__isnull=True, journal_entry__isnull=True),
                name="procurement_receipt_draft_reached_no_ledger",
            ),
            # A reversal says who, when, why, and which journal took it back.
            models.CheckConstraint(
                condition=~Q(status="REVERSED")
                | Q(
                    reversed_by__isnull=False,
                    reversed_at__isnull=False,
                    reversal_journal_entry__isnull=False,
                )
                & ~Q(reversal_reason=""),
                name="procurement_receipt_reversal_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(status="REVERSED")
                | Q(
                    reversed_by__isnull=True,
                    reversed_at__isnull=True,
                    reversal_journal_entry__isnull=True,
                    reversal_reason="",
                ),
                name="procurement_receipt_unreversed_carries_no_reversal",
            ),
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="procurement_receipt_number_unique_per_organization",
            ),
            # One supplier's delivery note is entered once. Scoped to the
            # supplier, not globally: two suppliers numbering their notes "1"
            # is not a conflict, and refusing it would be refusing reality.
            models.UniqueConstraint(
                fields=["organization", "supplier", "delivery_reference"],
                condition=~Q(delivery_reference="") & ~Q(status="REVERSED"),
                name="procurement_receipt_delivery_reference_unique_per_supplier",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="gr_org_status_idx"),
            models.Index(fields=["order"], name="gr_order_idx"),
        ]

    def __str__(self) -> str:
        return self.number or f"GRN draft {self.public_id}"

    @property
    def is_editable(self) -> bool:
        return self.status == GoodsReceiptStatus.DRAFT

    @property
    def is_ready_to_post(self) -> bool:
        """
        Whether a draft holds everything posting will need.

        Derived, never stored. A readiness flag would be a second opinion that
        goes stale the moment somebody edits a line, and the one that is stale
        is always the flag.

        This is `post_goods_receipt`'s precondition, surfaced so the screen can
        say "ready" before anybody presses anything. Posting re-derives it from
        the locked rows rather than trusting what a screen last rendered.

        Every line must be *fully disposed of*: `accepted + rejected` equals
        `delivered`, with no undisposed remainder. A line where three of twelve
        cartons are still unexamined is not a receipt anybody can post — the
        goods are neither in stock nor claimed against the supplier, and
        posting it would silently drop the difference.
        """
        lines = list(self.lines.all())
        if self.status != GoodsReceiptStatus.DRAFT:
            return False
        if not lines or not self.evidence_reference:
            return False
        return all(
            line.quality_result != QualityResult.NOT_INSPECTED
            and line.accepted_base_quantity + line.rejected_base_quantity
            == line.delivered_base_quantity
            for line in lines
        )

    @property
    def accepted_value(self) -> Decimal:
        """What the accepted goods are worth, summed from the lines."""
        return quantize_money(
            sum(
                (line.accepted_value for line in self.lines.all()),
                start=Decimal("0.000"),
            )
        )


class GoodsReceiptLine(TimeStampedModel):
    """
    One item that arrived, in the package it arrived in.

    `delivered = accepted + rejected` is a database `CheckConstraint`, not a
    service check (PRC-024): a line that does not add up is not a line, and the
    arithmetic is simple enough that the database can own it outright.

    The unit price is snapshotted here and is what will value the stock. It
    comes from the order where one is linked, and is entered where none is —
    never from the supplier catalogue, whose price is planning information that
    no posting path may read (PRC-005).
    """

    receipt = models.ForeignKey(
        GoodsReceipt,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("receipt"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))

    order_line = models.ForeignKey(
        PurchaseOrderLine,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipt_lines",
        verbose_name=_("order line"),
    )
    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="goods_receipt_lines",
        verbose_name=_("item"),
    )
    supplier_item = models.ForeignKey(
        SupplierItem,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipt_lines",
        verbose_name=_("catalogue row"),
    )
    package_unit = models.ForeignKey(
        "inventory.PackageUnit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipt_lines",
        verbose_name=_("package"),
    )
    conversion = models.ForeignKey(
        "inventory.ItemPackageConversion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipt_lines",
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

    delivered_quantity = models.DecimalField(
        _("delivered"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    delivered_base_quantity = models.DecimalField(
        _("delivered (base)"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
    )
    #: What the scale actually said, for a `VARIABLE` package. Twelve lambs is
    #: not a quantity of meat (PRC-026); this is, and it is what the base
    #: quantity comes from when the package is variable.
    measured_base_quantity = models.DecimalField(
        _("measured (base)"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )

    accepted_base_quantity = models.DecimalField(
        _("accepted (base)"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        default=Decimal("0.000"),
    )
    rejected_base_quantity = models.DecimalField(
        _("rejected (base)"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        default=Decimal("0.000"),
    )
    quality_result = models.CharField(
        _("quality result"),
        max_length=16,
        choices=QualityResult.choices,
        default=QualityResult.NOT_INSPECTED,
    )
    rejection_reason = models.ForeignKey(
        "inventory.InventoryReasonCode",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipt_rejections",
        verbose_name=_("rejection reason"),
    )

    lot = models.ForeignKey(
        "inventory.InventoryLot",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipt_lines",
        verbose_name=_("lot"),
    )
    expiry_date = models.DateField(_("expiry"), null=True, blank=True)

    #: Per entered unit. What will value the stock, and never the catalogue.
    unit_price = models.DecimalField(
        _("unit price"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
    )
    note = models.CharField(_("note"), max_length=200, blank=True)

    # --- what posting wrote ------------------------------------------------
    #
    # Written by `apps.procurement.posting` in the window between resolving the
    # accounts and flipping the status, which is exactly as long as the trigger
    # leaves the line unfrozen. `accepted_value` computes the same figure from
    # today's row; `posted_value` records the figure that actually reached both
    # ledgers, and only the second one may be reversed.

    #: The cost per **base** unit the stock entered at. Derived from the
    #: entered unit price and the package conversion, so a partial rejection
    #: values the accepted kilograms at the rate the whole delivery was priced.
    posted_unit_cost = models.DecimalField(
        _("posted unit cost"),
        max_digits=UNIT_PRICE_MAX_DIGITS,
        decimal_places=UNIT_PRICE_PLACES,
        null=True,
        blank=True,
    )
    posted_value = models.DecimalField(
        _("posted value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )
    inventory_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipt_lines",
        verbose_name=_("inventory account"),
    )
    contra_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipt_contra_lines",
        verbose_name=_("GRNI account"),
    )
    #: The mapping rows that produced the account, kept so a later mapping
    #: change cannot make a posted line unexplainable (ADR-019).
    resolved_mapping = models.ForeignKey(
        "inventory.InventoryAccountMapping",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipt_lines",
        verbose_name=_("item mapping"),
    )
    resolved_organization_mapping = models.ForeignKey(
        "accounting.OrganizationAccountMapping",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipt_lines",
        verbose_name=_("organization mapping"),
    )
    movement = models.ForeignKey(
        "inventory.StockMovement",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipt_lines",
        verbose_name=_("stock movement"),
    )
    journal_line = models.ForeignKey(
        "accounting.JournalLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_receipt_lines",
        verbose_name=_("journal line"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("goods receipt line")
        verbose_name_plural = _("goods receipt lines")
        ordering = ["receipt", "sequence"]
        constraints = [
            models.CheckConstraint(
                condition=Q(delivered_quantity__gt=0),
                name="procurement_receipt_line_delivered_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(delivered_base_quantity__gt=0),
                name="procurement_receipt_line_delivered_base_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(accepted_base_quantity__gte=0) & Q(rejected_base_quantity__gte=0),
                name="procurement_receipt_line_split_is_not_negative",
            ),
            # PRC-024. The one rule the database owns outright.
            models.CheckConstraint(
                condition=Q(accepted_base_quantity__lte=models.F("delivered_base_quantity")),
                name="procurement_receipt_line_accepted_within_delivered",
            ),
            models.CheckConstraint(
                condition=Q(rejected_base_quantity__lte=models.F("delivered_base_quantity")),
                name="procurement_receipt_line_rejected_within_delivered",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0),
                name="procurement_receipt_line_price_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(package_unit__isnull=True, conversion__isnull=True)
                | Q(package_unit__isnull=False, conversion__isnull=False),
                name="procurement_receipt_line_package_carries_its_conversion",
            ),
            # Rejecting something requires saying why.
            models.CheckConstraint(
                condition=Q(rejected_base_quantity=0) | Q(rejection_reason__isnull=False),
                name="procurement_receipt_line_rejection_states_a_reason",
            ),
            # An expiry belongs to a lot.
            models.CheckConstraint(
                condition=Q(expiry_date__isnull=True) | Q(lot__isnull=False),
                name="procurement_receipt_line_expiry_requires_a_lot",
            ),
            models.UniqueConstraint(
                fields=["receipt", "sequence"],
                name="procurement_receipt_line_sequence_unique",
            ),
            # A movement always names the value it carried.
            models.CheckConstraint(
                condition=Q(movement__isnull=True) | Q(posted_value__isnull=False),
                name="procurement_receipt_line_movement_states_its_value",
            ),
            # A posted line that accepted something must name the movement it
            # produced. A wholly rejected line is the one shape where "posted
            # nothing" is the correct answer rather than a missing link: the
            # goods never entered inventory, so there is no movement to name
            # and its posted value is zero (PRC-025).
            models.CheckConstraint(
                condition=Q(posted_value__isnull=True)
                | Q(accepted_base_quantity=0)
                | Q(movement__isnull=False),
                name="procurement_receipt_line_accepted_stock_has_a_movement",
            ),
            # Both sides of the entry or neither — and a line with a movement
            # has both, because value that entered inventory came from
            # somewhere.
            models.CheckConstraint(
                condition=Q(inventory_account__isnull=True, contra_account__isnull=True)
                | Q(inventory_account__isnull=False, contra_account__isnull=False),
                name="procurement_receipt_line_names_both_accounts",
            ),
            models.CheckConstraint(
                condition=Q(movement__isnull=True) | Q(inventory_account__isnull=False),
                name="procurement_receipt_line_movement_names_its_account",
            ),
        ]
        indexes = [
            models.Index(fields=["order_line"], name="gr_line_order_line_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.receipt} · {self.item.code}"

    @property
    def base_unit_cost(self) -> Decimal:
        """
        What one **base** unit of this delivery cost.

        The entered price is per entered unit — per carton, per container — and
        stock is held per kilogram, so the two have to be reconciled once,
        here, rather than at each of the four places that need the answer.

        Twelve cartons at 14,000 over 120 kg is 1,400 a kilogram. A container
        priced at 9,500 that weighed 17.4 kg is 545.977011 a kilogram, and the
        scale decides the divisor, not the planning factor (PRC-026).
        """
        if self.delivered_base_quantity == 0:  # pragma: no cover - a constraint refuses it
            return Decimal("0.000000")
        return quantize_unit_price(
            self.delivered_quantity * self.unit_price / self.delivered_base_quantity
        )

    @property
    def accepted_value(self) -> Decimal:
        """
        What the accepted portion is worth, at the moment it is asked.

        Quantized **once**, from the accepted base quantity at the base unit
        cost — the same arithmetic `post_goods_receipt` will hand the kernel,
        so the figure the screen shows before posting is the figure that
        posts. Rating the whole line and then taking a share of it would round
        twice and could disagree with the ledger by a unit in the last place,
        which is exactly the disagreement ADR-006 exists to prevent.

        Once posted, `posted_value` is the authoritative figure: this one
        recomputes from today's row, and only the stored one reached a ledger.
        """
        return quantize_money(self.accepted_base_quantity * self.base_unit_cost)


# ---------------------------------------------------------------------------
# Supplier invoices (Task 2.10)
# ---------------------------------------------------------------------------


class SupplierInvoiceStatus(models.TextChoices):
    """
    The four Task 2.0 §9 statuses, and no more.

    `APPROVED` is not decoration. A receipt goes straight from draft to posted
    because the goods are either acceptable or they are not; an invoice is a
    claim on the organization's money, and somebody other than the person who
    typed it has to agree the claim is real before it reaches the ledger.

    `APPROVED` is also where an invoice **waits**. An invoice carrying an
    inventory line has no determinate accounting until it is matched against
    the receipt it covers (Task 2.0 §9), so it approves and then holds — see
    `SupplierInvoice.blocking_lines`.
    """

    DRAFT = "DRAFT", _("مسودة")
    APPROVED = "APPROVED", _("معتمدة")
    POSTED = "POSTED", _("مرحّلة")
    REVERSED = "REVERSED", _("معكوسة")


class SupplierInvoiceLineType(models.TextChoices):
    """
    What one invoice line economically *is*. Exactly one answer per line.

    `INVENTORY` bills for goods. Its accounting is `Dr GRNI / Cr payable` for
    the matched portion, with the difference going to purchase price variance
    (Task 2.0 §9) — and "the matched portion" is a Task 2.11 fact, so a line of
    this type is enterable and approvable here and posts in Task 2.12.

    `ACCOUNT` bills for something that never entered stock: a delivery charge,
    a repair, a subscription. It names the account it belongs to, its
    accounting is `Dr that account / Cr payable`, and nothing about it depends
    on matching — so it posts here, today.

    The two are mutually exclusive by database constraint. A line that was both
    would have two contradictory debits and no way to choose between them.
    """

    INVENTORY = "INVENTORY", _("بضاعة")
    ACCOUNT = "ACCOUNT", _("مصروف أو حساب مباشر")


class SupplierInvoice(TimeStampedModel):
    """
    What the supplier says is owed, and for what.

    A separate event from the receipt, on a different date, often for a
    different amount — which is the whole reason Task 2.0 §1 refuses to model a
    purchase as one editable form. The meat arrived Sunday; the invoice is
    approved Tuesday for a figure that does not quite agree; the payment leaves
    at month end covering three invoices and part of a fourth.

    **This document never touches stock** (PRC-038). Not a quantity, not a lot,
    not a movement, not a valuation. What it creates is a *payable*: an amount
    owed to one supplier, due on one date, that a credit note or a payment will
    later reduce.

    **The supplier's balance is not stored anywhere.** It is derived from
    posted invoices less active credit and payment allocations, every time it
    is asked for — see `apps.procurement.selectors.supplier_outstanding`. A
    stored balance is a second source of truth, and the second one is always
    the one that is wrong.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="supplier_invoices",
        verbose_name=_("organization"),
    )
    #: The accounting scope every journal line carries. An invoice belongs to
    #: the branch that will answer for the spend, even though the payable is
    #: the organization's.
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="supplier_invoices",
        verbose_name=_("branch"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="invoices",
        verbose_name=_("supplier"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    #: Our own gapless number, drawn at posting. The supplier's number is
    #: theirs and may be anything; this one is ours and is what the ledger
    #: cites.
    number = models.CharField(_("number"), max_length=32, blank=True)

    #: The supplier's own reference, stored exactly as they wrote it.
    supplier_invoice_number = models.CharField(_("supplier invoice number"), max_length=64)
    #: The same reference, folded for comparison. Stored rather than computed
    #: in a functional index so the uniqueness rule is legible in the schema
    #: and identical in Python and in PostgreSQL. See
    #: `apps.procurement.invoices.normalize_invoice_number`.
    supplier_invoice_number_key = models.CharField(
        _("supplier invoice number (key)"), max_length=64, editable=False
    )
    supplier_reference = models.CharField(_("supplier reference"), max_length=200, blank=True)

    #: The date on the supplier's document. Theirs, and not necessarily ours.
    invoice_date = models.DateField(_("invoice date"))
    #: The branch business date this posts on. Ours (ADR-008), and the date
    #: whose accounting period must be open.
    business_date = models.DateField(_("business date"))
    #: `invoice_date` plus the terms snapshot, computed once and stored.
    #: Renegotiating a supplier's terms in March must not restate the due date
    #: of an invoice received in January.
    due_date = models.DateField(_("due date"))
    payment_terms_days = models.PositiveSmallIntegerField(_("payment terms (days)"), default=0)
    #: Effective-dated term chosen and frozen at approval. The FK is evidence;
    #: the copied public identity, version, label and days are the historical
    #: statement and never re-resolve through today's supplier terms.
    credit_term = models.ForeignKey(
        SupplierCreditTerm,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoices",
        verbose_name=_("credit term"),
    )
    credit_term_public_id = models.UUIDField(_("credit term public id"), null=True, blank=True)
    credit_term_version = models.PositiveIntegerField(
        _("credit term version"), null=True, blank=True
    )
    credit_term_name = models.CharField(_("credit term name"), max_length=200, blank=True)
    credit_term_net_days = models.PositiveSmallIntegerField(_("credit term net days"), default=0)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=SupplierInvoiceStatus.choices,
        default=SupplierInvoiceStatus.DRAFT,
    )

    #: The sum of the stored line amounts, before document-level charges.
    lines_total = models.DecimalField(
        _("lines total"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0.000"),
    )
    #: Captured because the supplier charged it. Whether freight is
    #: capitalised into inventory value is PRC-046, deferred with its reason;
    #: here it is allocated across the lines and billed.
    freight_amount = models.DecimalField(
        _("freight"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0.000"),
    )
    discount_amount = models.DecimalField(
        _("discount"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0.000"),
    )
    #: `lines_total + freight - discount`, and equal to the sum of the stored
    #: line net amounts. Never rounded independently of its lines (ADR-012).
    total_amount = models.DecimalField(
        _("total"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0.000"),
    )

    notes = models.TextField(_("notes"), blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_supplier_invoices",
        verbose_name=_("created by"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_supplier_invoices",
        verbose_name=_("approved by"),
    )
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_supplier_invoices",
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_supplier_invoices",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    #: The branch settings in force when this posted, so the business date it
    #: claims can be re-derived exactly however the cutoff changes later.
    business_date_timezone = models.CharField(_("business timezone"), max_length=64, blank=True)
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)

    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_invoices",
        verbose_name=_("journal entry"),
    )
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_supplier_invoices",
        verbose_name=_("reversal journal entry"),
    )
    #: What actually reached the ledger. `total_amount` is what the document
    #: says today; this is what was posted, and only this may be reversed.
    posted_amount = models.DecimalField(
        _("posted amount"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("supplier invoice")
        verbose_name_plural = _("supplier invoices")
        ordering = ["-invoice_date", "-id"]
        permissions = [
            ("create_supplier_invoice", _("Can record a supplier invoice")),
            ("approve_supplier_invoice", _("Can approve a supplier invoice")),
            ("post_supplier_invoice", _("Can post a supplier invoice to the ledger")),
            ("reverse_supplier_invoice", _("Can reverse a posted supplier invoice")),
        ]
        constraints = [
            # PRC-037. Paying the same invoice twice is the most expensive
            # ordinary mistake in accounts payable, so the database refuses it
            # rather than a service remembering to. Scoped to the supplier,
            # because two suppliers both numbering an invoice "1" is not a
            # conflict; excluding reversed rows, because a reversed invoice is
            # corrected by re-entering the same supplier reference.
            models.UniqueConstraint(
                fields=["organization", "supplier", "supplier_invoice_number_key"],
                condition=~Q(status="REVERSED"),
                name="procurement_invoice_number_unique_per_supplier",
            ),
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="procurement_invoice_number_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=~Q(supplier_invoice_number="") & ~Q(supplier_invoice_number_key=""),
                name="procurement_invoice_states_the_supplier_reference",
            ),
            models.CheckConstraint(
                condition=Q(due_date__gte=models.F("invoice_date")),
                name="procurement_invoice_due_after_invoice_date",
            ),
            models.CheckConstraint(
                condition=Q(credit_term__isnull=True)
                | Q(
                    credit_term_public_id__isnull=False,
                    credit_term_version__isnull=False,
                    credit_term_name__gt="",
                    credit_term_net_days=models.F("payment_terms_days"),
                ),
                name="procurement_invoice_credit_term_snapshot_complete",
            ),
            models.CheckConstraint(
                condition=Q(freight_amount__gte=0) & Q(discount_amount__gte=0),
                name="procurement_invoice_charges_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(approved_by__isnull=True, approved_at__isnull=True)
                | Q(approved_by__isnull=False, approved_at__isnull=False),
                name="procurement_invoice_approval_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(posted_by__isnull=True, posted_at__isnull=True)
                | Q(posted_by__isnull=False, posted_at__isnull=False),
                name="procurement_invoice_posting_is_complete",
            ),
            # A draft has been neither approved nor posted.
            models.CheckConstraint(
                condition=~Q(status="DRAFT")
                | Q(approved_at__isnull=True, posted_at__isnull=True, journal_entry__isnull=True),
                name="procurement_invoice_draft_has_done_nothing",
            ),
            # An approved invoice says who agreed to it and has not posted.
            models.CheckConstraint(
                condition=~Q(status="APPROVED")
                | Q(approved_at__isnull=False, posted_at__isnull=True, journal_entry__isnull=True),
                name="procurement_invoice_approved_says_who",
            ),
            # A posted invoice names its journal and what it posted. Together
            # with the immutability trigger this is what makes "a payable with
            # no journal" a shape the table cannot hold.
            models.CheckConstraint(
                condition=~Q(status__in=["POSTED", "REVERSED"])
                | Q(
                    approved_at__isnull=False,
                    posted_at__isnull=False,
                    journal_entry__isnull=False,
                    posted_amount__isnull=False,
                    business_date_timezone__gt="",
                    business_day_start__isnull=False,
                ),
                name="procurement_invoice_posted_names_its_journal",
            ),
            models.CheckConstraint(
                condition=~Q(status="REVERSED")
                | Q(
                    reversed_by__isnull=False,
                    reversed_at__isnull=False,
                    reversal_journal_entry__isnull=False,
                )
                & ~Q(reversal_reason=""),
                name="procurement_invoice_reversal_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(status="REVERSED")
                | Q(
                    reversed_by__isnull=True,
                    reversed_at__isnull=True,
                    reversal_journal_entry__isnull=True,
                    reversal_reason="",
                ),
                name="procurement_invoice_unreversed_carries_no_reversal",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="sinv_org_status_idx"),
            models.Index(fields=["supplier", "status"], name="sinv_supplier_status_idx"),
            models.Index(fields=["due_date"], name="sinv_due_idx"),
        ]

    def __str__(self) -> str:
        return self.number or f"{self.supplier.code} {self.supplier_invoice_number}"

    @property
    def is_editable(self) -> bool:
        return self.status == SupplierInvoiceStatus.DRAFT

    @property
    def blocking_lines(self) -> list[SupplierInvoiceLine]:
        """
        The lines that have no complete accounting route yet.

        Today that is every `INVENTORY` line, and the reason is specific rather
        than provisional: Task 2.0 §9 posts the **matched receipt value** to
        GRNI and the difference to purchase price variance. Both figures come
        from a match allocation, which is Task 2.11. Posting the invoice amount
        to GRNI instead would clear a variance nobody computed and leave Task
        2.12 nothing to recognise — so the invoice waits in `APPROVED` and says
        why, rather than posting an entry that is merely balanced.
        """
        return [
            line for line in self.lines.all() if line.line_type == SupplierInvoiceLineType.INVENTORY
        ]

    @property
    def is_ready_to_post(self) -> bool:
        """
        Whether every line has a complete, approved accounting route.

        Derived, never stored. A readiness flag would be a second opinion that
        goes stale the moment somebody edits a line.
        """
        lines = list(self.lines.all())
        if self.status != SupplierInvoiceStatus.APPROVED or not lines:
            return False
        return not self.blocking_lines


class SupplierInvoiceLine(TimeStampedModel):
    """
    One thing the supplier is charging for.

    Exactly one economic type (`SupplierInvoiceLineType`), enforced by a
    database constraint rather than by a service remembering. A line that named
    both an inventory item and a direct expense account would have two
    contradictory debits and no principled way to choose.

    A line may **reference** a purchase order line or a goods receipt line.
    That reference is evidence, not an allocation: it says "this is what I
    believe this charge covers", and it does not consume a receipt's matchable
    remainder, mark anything matched, or clear any variance. Those are Task
    2.11 records and they are deliberately absent here (PRC-040 – PRC-042).
    """

    invoice = models.ForeignKey(
        SupplierInvoice,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("invoice"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))

    line_type = models.CharField(
        _("line type"), max_length=16, choices=SupplierInvoiceLineType.choices
    )

    # --- the inventory shape ------------------------------------------------
    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_invoice_lines",
        verbose_name=_("item"),
    )
    supplier_item = models.ForeignKey(
        SupplierItem,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoice_lines",
        verbose_name=_("catalogue row"),
    )
    order_line = models.ForeignKey(
        PurchaseOrderLine,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoice_lines",
        verbose_name=_("order line"),
    )
    #: Which version of the order this charge was checked against, copied
    #: rather than joined to for the same reason the receipt copies it.
    order_version = models.PositiveIntegerField(_("order version"), null=True, blank=True)
    receipt_line = models.ForeignKey(
        GoodsReceiptLine,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoice_lines",
        verbose_name=_("receipt line"),
    )
    base_quantity = models.DecimalField(
        _("quantity (base)"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
        null=True,
        blank=True,
    )

    # --- the direct-account shape -------------------------------------------
    account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_invoice_lines",
        verbose_name=_("account"),
    )
    cost_center = models.ForeignKey(
        "accounting.CostCenter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_invoice_lines",
        verbose_name=_("cost center"),
    )

    # --- what both shapes carry ---------------------------------------------
    description = models.CharField(_("description"), max_length=200, blank=True)
    quantity = models.DecimalField(
        _("quantity"), max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_PLACES
    )
    unit_price = models.DecimalField(
        _("unit price"), max_digits=UNIT_PRICE_MAX_DIGITS, decimal_places=UNIT_PRICE_PLACES
    )
    #: `quantize_money(quantity * unit_price)`, stored once at the boundary.
    line_amount = models.DecimalField(
        _("line amount"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    #: This line's share of the document freight and discount, allocated with
    #: `apps.core.allocation.allocate` — largest remainder over an explicit
    #: sequence, so the parts sum exactly to the whole and the answer does not
    #: depend on queryset order (PRC-039).
    allocated_freight = models.DecimalField(
        _("allocated freight"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0.000"),
    )
    allocated_discount = models.DecimalField(
        _("allocated discount"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0.000"),
    )
    #: `line_amount + allocated_freight - allocated_discount`. The figure that
    #: posts, and the one the document total is the sum of.
    net_amount = models.DecimalField(
        _("net amount"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    note = models.CharField(_("note"), max_length=200, blank=True)

    # --- what posting wrote --------------------------------------------------
    journal_line = models.ForeignKey(
        "accounting.JournalLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_invoice_lines",
        verbose_name=_("journal line"),
    )
    resolved_organization_mapping = models.ForeignKey(
        "accounting.OrganizationAccountMapping",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_invoice_lines",
        verbose_name=_("organization mapping"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("supplier invoice line")
        verbose_name_plural = _("supplier invoice lines")
        ordering = ["invoice", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "sequence"],
                name="procurement_invoice_line_sequence_unique",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="procurement_invoice_line_quantity_positive"
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0), name="procurement_invoice_line_price_not_negative"
            ),
            models.CheckConstraint(
                condition=Q(allocated_freight__gte=0) & Q(allocated_discount__gte=0),
                name="procurement_invoice_line_shares_not_negative",
            ),
            # The exclusive line-type invariant, at the database. An inventory
            # line names an item and a base quantity and never an account; a
            # direct line names an account and never an item, an order line or
            # a receipt line. There is no third shape and no overlap.
            models.CheckConstraint(
                condition=(
                    Q(
                        line_type="INVENTORY",
                        item__isnull=False,
                        base_quantity__isnull=False,
                        account__isnull=True,
                    )
                    | Q(
                        line_type="ACCOUNT",
                        account__isnull=False,
                        item__isnull=True,
                        supplier_item__isnull=True,
                        order_line__isnull=True,
                        receipt_line__isnull=True,
                        base_quantity__isnull=True,
                    )
                ),
                name="procurement_invoice_line_has_one_economic_type",
            ),
            # A cost centre belongs to the account that demanded it, so it can
            # only appear on a line that names one.
            models.CheckConstraint(
                condition=Q(cost_center__isnull=True) | Q(account__isnull=False),
                name="procurement_invoice_line_cost_center_needs_an_account",
            ),
        ]
        indexes = [
            models.Index(fields=["receipt_line"], name="sinv_line_receipt_idx"),
            models.Index(fields=["order_line"], name="sinv_line_order_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.invoice} · {self.sequence}"

    @property
    def target_label(self) -> str:
        """What this line is for, in one string, for a screen or an export."""
        if self.line_type == SupplierInvoiceLineType.INVENTORY and self.item is not None:
            return f"{self.item.code} — {self.item.name_ar}"
        if self.account is not None:
            return f"{self.account.code} — {self.account.name_ar}"
        return self.description  # pragma: no cover - a constraint refuses this row


# ---------------------------------------------------------------------------
# Three-way matching (Task 2.11)
# ---------------------------------------------------------------------------


class PurchaseMatchStatus(models.TextChoices):
    """
    Three states, and still deliberately no `POSTED` — now for a better reason
    than when Task 2.11 wrote it.

    Matching decides *what covers what*. Turning that into a payable and a GRNI
    clearing is a separate act, and Task 2.12 made it one: a
    `SupplierInvoicePosting` generation, which knows whether it is live and
    which journal it produced. Adding a fourth value here would be a second
    copy of a fact that table already holds, and the two would drift the first
    time a reversal touched one and not the other. `live_posting_for(match)`
    derives it instead, exactly as line match state is derived (PRC-042).

    `READY` therefore still means "the evidence is complete and immutable" and
    nothing more. What changed at Task 2.12 is that a `READY` match may now be
    *stood on* by a live posting, and cancelling one that is refuses — in the
    service and at the database both.
    """

    DRAFT = "DRAFT", _("مسودة")
    READY = "READY", _("جاهزة للترحيل المالي")
    CANCELLED = "CANCELLED", _("ملغاة")


class PurchaseMatch(TimeStampedModel):
    """
    One reconciliation of a supplier invoice against what actually arrived.

    Task 2.0 §9 sketches `MatchAllocation` rows and no header. The rows are
    kept exactly as specified — they are where the economics live — but they
    are gathered under this aggregate, because Task 2.12 has to post from a set
    of allocations that cannot change underneath it. Without a header, "the
    allocations for this invoice" is a query whose answer moves every time
    somebody adds a row, and a financial posting cannot be built on that.

    So the division is: allocations say what covers what, and the match says
    *this* set of them is the agreed answer. `READY` freezes it.

    **Nothing here reaches a ledger.** No journal, no stock movement, no
    payable. The invoice stays `APPROVED` throughout, and a test asserts it.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="purchase_matches",
        verbose_name=_("organization"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="purchase_matches",
        verbose_name=_("supplier"),
    )
    #: `PROTECT` rather than `CASCADE`: a match is evidence about an invoice,
    #: and an invoice that has been matched is not one anybody deletes.
    supplier_invoice = models.ForeignKey(
        SupplierInvoice,
        on_delete=models.PROTECT,
        related_name="matches",
        verbose_name=_("supplier invoice"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    number = models.CharField(_("number"), max_length=32, blank=True)

    status = models.CharField(
        _("status"),
        max_length=12,
        choices=PurchaseMatchStatus.choices,
        default=PurchaseMatchStatus.DRAFT,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_purchase_matches",
        verbose_name=_("created by"),
    )
    ready_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="readied_purchase_matches",
        verbose_name=_("marked ready by"),
    )
    ready_at = models.DateTimeField(_("ready at"), null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cancelled_purchase_matches",
        verbose_name=_("cancelled by"),
    )
    cancelled_at = models.DateTimeField(_("cancelled at"), null=True, blank=True)
    cancellation_reason = models.TextField(_("cancellation reason"), blank=True)

    notes = models.TextField(_("notes"), blank=True)

    #: A canonical hash of the allocation set at the moment it was frozen.
    #: A retried `ready` command carrying the same allocations returns the
    #: same match; one carrying different allocations is a different economic
    #: claim and is refused (ADR-017).
    allocation_fingerprint = models.CharField(
        _("allocation fingerprint"), max_length=64, blank=True
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("purchase match")
        verbose_name_plural = _("purchase matches")
        ordering = ["-id"]
        permissions = [
            ("match_supplier_invoice", _("Can match a supplier invoice to its deliveries")),
            ("cancel_purchase_match", _("Can cancel a purchase match")),
        ]
        constraints = [
            # One live match per invoice. Two competing answers to "what does
            # this invoice cover" is not a state anybody can act on, and Task
            # 2.12 would have to pick one arbitrarily. A cancelled match stays
            # for the history and stops competing.
            models.UniqueConstraint(
                fields=["supplier_invoice"],
                condition=~Q(status="CANCELLED"),
                name="procurement_one_active_match_per_invoice",
            ),
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="procurement_match_number_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=~Q(status="READY")
                | Q(ready_by__isnull=False, ready_at__isnull=False) & ~Q(allocation_fingerprint=""),
                name="procurement_match_ready_says_who_and_what",
            ),
            # A draft has not been readied. Deliberately not "only READY may
            # carry readiness": cancelling a READY match is the correction
            # mechanism, and it must keep the record of who froze it and when.
            # Wiping that on cancellation would erase the fact that somebody
            # agreed the match before it was withdrawn.
            models.CheckConstraint(
                condition=~Q(status="DRAFT") | Q(ready_by__isnull=True, ready_at__isnull=True),
                name="procurement_match_draft_carries_no_readiness",
            ),
            models.CheckConstraint(
                condition=~Q(status="CANCELLED")
                | Q(cancelled_by__isnull=False, cancelled_at__isnull=False)
                & ~Q(cancellation_reason=""),
                name="procurement_match_cancellation_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(status="CANCELLED")
                | Q(
                    cancelled_by__isnull=True,
                    cancelled_at__isnull=True,
                    cancellation_reason="",
                ),
                name="procurement_match_uncancelled_carries_no_cancellation",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="match_org_status_idx"),
            models.Index(fields=["supplier_invoice"], name="match_invoice_idx"),
        ]

    #: What the invoice's header-level reversal guard reads (Task 2.14 made
    #: the header walk real): a cancelled match is history and holds nothing.
    #: The reversal flow cancels the invoice's own match *before* the guard
    #: runs, so this filter is what lets the documented correction proceed.
    live_dependency = Q(status__in=("DRAFT", "READY"))

    def __str__(self) -> str:
        return self.number or f"match {self.public_id}"

    @property
    def is_editable(self) -> bool:
        return self.status == PurchaseMatchStatus.DRAFT

    @property
    def is_active(self) -> bool:
        """A match that still counts against availability."""
        return self.status != PurchaseMatchStatus.CANCELLED

    @property
    def total_matched_quantity(self) -> Decimal:
        return quantize_quantity(
            sum(
                (row.matched_base_quantity for row in self.allocations.all()),
                start=Decimal("0.000"),
            )
        )

    @property
    def total_price_variance(self) -> Decimal:
        """
        What this match says the invoice differs from the deliveries by.

        Informational in Task 2.11. It becomes the purchase price variance
        posting in Task 2.12, and until then it is a number on a screen that a
        human reads and nothing else acts on.
        """
        return quantize_money(
            sum((row.price_variance for row in self.allocations.all()), start=Decimal("0.000"))
        )


class PurchaseMatchAllocation(TimeStampedModel):
    """
    One statement that this much of a delivery covers this much of an invoice.

    Task 2.0 §9's `MatchAllocation`, with its single `matched_value` split into
    the two figures the same section's posting formula actually needs:

        Dr  GRNI                     the matched **receipt** value
        Dr  purchase price variance  the difference
            Cr  supplier payable     the **invoiced** value

    One value cannot express that. The receipt side is what the delivery
    parked in GRNI; the invoice side is what the supplier is charging; the
    difference between them is the whole reason three-way matching exists, and
    storing only one of the two would make it uncomputable (PRC-043).

    The purchase order line **and its version** are both retained. A revision
    after this allocation was made must not reinterpret what was matched: the
    order that was agreed when the goods arrived is the order this covers.
    """

    #: Task 2.9's reversal guard asks each dependent relation which of its rows
    #: still stand, and this is the answer for allocations. A cancelled match
    #: consumes no availability, so it must not hold a delivery hostage either;
    #: the two answers have to agree, or cancelling — the documented correction
    #: — would leave the receipt permanently unreversible. Not a field: `Q` has
    #: no `contribute_to_class`, so Django leaves it alone.
    live_dependency = Q(match__status__in=(PurchaseMatchStatus.DRAFT, PurchaseMatchStatus.READY))

    match = models.ForeignKey(
        PurchaseMatch,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name=_("match"),
    )
    allocation_uid = models.UUIDField(_("allocation uid"), default=uuid.uuid4, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))

    supplier_invoice_line = models.ForeignKey(
        SupplierInvoiceLine,
        on_delete=models.PROTECT,
        related_name="match_allocations",
        verbose_name=_("invoice line"),
    )
    goods_receipt_line = models.ForeignKey(
        GoodsReceiptLine,
        on_delete=models.PROTECT,
        related_name="match_allocations",
        verbose_name=_("receipt line"),
    )
    #: Derived from the receipt line where it has one (Task 2.0 §9), and null
    #: for a direct market purchase that never had an order.
    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="match_allocations",
        verbose_name=_("order line"),
    )
    #: The version this allocation was made against, copied rather than joined
    #: to. A later revision must not restate what was matched.
    purchase_order_version = models.PositiveIntegerField(_("order version"), null=True, blank=True)

    #: Base units, always. Two cartons and sixty kilograms are comparable only
    #: through the conversion each document already snapshotted (PRC-014).
    matched_base_quantity = models.DecimalField(
        _("matched quantity (base)"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
    )
    #: This much of what the receipt posted. Derived from the receipt's own
    #: posted value, never from today's warehouse average.
    receipt_allocated_value = models.DecimalField(
        _("receipt value"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    #: This much of what the invoice charges, after any Task 2.10 freight and
    #: discount allocation.
    invoice_allocated_value = models.DecimalField(
        _("invoice value"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    #: `invoice_allocated_value - receipt_allocated_value`. Stored rather than
    #: derived so the database can assert the arithmetic, and so a report can
    #: sum it without recomputing.
    price_variance = models.DecimalField(
        _("price variance"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_match_allocations",
        verbose_name=_("created by"),
    )
    note = models.CharField(_("note"), max_length=200, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("purchase match allocation")
        verbose_name_plural = _("purchase match allocations")
        ordering = ["match", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["match", "sequence"], name="procurement_match_allocation_sequence_unique"
            ),
            # One pairing per match. Two rows for the same invoice line and
            # receipt line are one allocation entered twice, and summing them
            # would consume the remainder at double the rate.
            models.UniqueConstraint(
                fields=["match", "supplier_invoice_line", "goods_receipt_line"],
                name="procurement_match_allocation_pair_unique",
            ),
            models.CheckConstraint(
                condition=Q(matched_base_quantity__gt=0),
                name="procurement_match_allocation_quantity_positive",
            ),
            # Values are non-negative: a delivery cannot cover a negative
            # amount. The variance is the only figure that may be either sign.
            models.CheckConstraint(
                condition=Q(receipt_allocated_value__gte=0) & Q(invoice_allocated_value__gte=0),
                name="procurement_match_allocation_values_not_negative",
            ),
            # The variance is its own arithmetic, asserted by the database so
            # no path can store a difference its own components do not support.
            models.CheckConstraint(
                condition=Q(
                    price_variance=models.F("invoice_allocated_value")
                    - models.F("receipt_allocated_value")
                ),
                name="procurement_match_allocation_variance_is_the_difference",
            ),
            # An order line brings its version, or neither is recorded.
            models.CheckConstraint(
                condition=Q(purchase_order_line__isnull=True, purchase_order_version__isnull=True)
                | Q(purchase_order_line__isnull=False, purchase_order_version__isnull=False),
                name="procurement_match_allocation_order_carries_its_version",
            ),
        ]
        indexes = [
            models.Index(fields=["supplier_invoice_line"], name="match_alloc_invoice_line_idx"),
            models.Index(fields=["goods_receipt_line"], name="match_alloc_receipt_line_idx"),
            models.Index(fields=["purchase_order_line"], name="match_alloc_order_line_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.match} · {self.sequence}"


# ---------------------------------------------------------------------------
# The financial posting of a matched invoice (Task 2.12)
# ---------------------------------------------------------------------------


class SupplierInvoicePostingStatus(models.TextChoices):
    """Two states. A posting is live, or it has been reversed by its own twin."""

    LIVE = "LIVE", _("سارٍ")
    REVERSED = "REVERSED", _("معكوس")


class SupplierInvoicePosting(TimeStampedModel):
    """
    One generation of the financial posting of one matched supplier invoice.

    Task 2.11 decided *what covers what*. This is the act that turns that into
    money: it clears from GRNI exactly what the deliveries posted, credits the
    supplier with exactly what they are charging, and parks the difference.

    ## Why a record and not just a journal

    Because "has this invoice been posted?", "may this match be cancelled?" and
    "may this delivery be reversed?" are the same question, and the answer has
    to survive a correction. A boolean on the invoice cannot express *posted,
    then reversed, then posted again from a corrected match* — and that
    sequence is the ordinary correction path, not an exotic one.

    So each posting is a **generation**. Generation 1 posts; reversing it marks
    that generation `REVERSED` and leaves it in place; a replacement match
    produces generation 2. Nothing is edited and nothing is deleted, and the
    history reads as what happened rather than as the latest opinion.

    ## What "live" governs

    While a `LIVE` posting exists for an invoice:

    - its match cannot be cancelled — the ledger is standing on it;
    - its allocations stay frozen, as they already are from `READY`;
    - the deliveries it cites cannot be reversed;
    - the invoice cannot be posted again.

    Every one of those reads the same predicate, `status=LIVE`, rather than
    each guard inventing its own idea of "in use". A reversed generation
    governs nothing: it is history, and history holds nothing hostage. That is
    the property that makes the correction path work — the guard that stops a
    match being cancelled underneath a live ledger entry must not also be the
    guard that stops the reversal from ever happening.

    ## Source identity

    The journal names **this record's** `public_id`, not the invoice's. An
    invoice may legitimately reach the ledger twice — posted, reversed,
    re-posted from a corrected match — and ADR-017's source identity has to
    tell those two entries apart. Keying on the invoice would make the second
    posting look like a retry of the first, and the kernel would refuse it.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="supplier_invoice_postings",
        verbose_name=_("organization"),
    )
    #: `PROTECT` throughout. A posted record is evidence, and evidence does not
    #: disappear because somebody deleted the thing it describes.
    supplier_invoice = models.ForeignKey(
        SupplierInvoice,
        on_delete=models.PROTECT,
        related_name="postings",
        verbose_name=_("supplier invoice"),
    )
    #: The match this generation posted from. Retained after reversal: "which
    #: evidence did we act on" is exactly what an auditor asks.
    #:
    #: Null on an invoice with no goods on it. A delivery charge or a repair
    #: bill is matched against nothing — there is no delivery to compare it to
    #: — and inventing an empty match for it would put a row in the matching
    #: workspace that says nothing and that somebody would have to explain.
    purchase_match = models.ForeignKey(
        PurchaseMatch,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="postings",
        verbose_name=_("purchase match"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    #: 1, 2, 3 … per invoice. How many times this invoice has reached the
    #: ledger, and the reason a re-post is not mistaken for a retry.
    generation = models.PositiveIntegerField(_("generation"), default=1)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=SupplierInvoicePostingStatus.choices,
        default=SupplierInvoicePostingStatus.LIVE,
    )

    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        related_name="supplier_invoice_postings",
        verbose_name=_("journal entry"),
    )
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_supplier_invoice_postings",
        verbose_name=_("reversal journal entry"),
    )

    #: The allocation set this generation acted on, hashed. Copied from the
    #: match rather than recomputed, so the posting names the exact evidence
    #: that existed when it ran. Empty where there was no match.
    allocation_fingerprint = models.CharField(
        _("allocation fingerprint"), max_length=64, blank=True
    )

    #: What the deliveries posted, and therefore what clears from GRNI.
    #: `SUM(allocation.receipt_allocated_value)`.
    goods_cleared_value = models.DecimalField(
        _("goods cleared from GRNI"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    #: What the supplier charges for those same goods.
    #: `SUM(allocation.invoice_allocated_value)`.
    invoice_matched_value = models.DecimalField(
        _("invoiced value of matched goods"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
    )
    #: `invoice_matched_value - goods_cleared_value`, signed. Positive means
    #: the supplier is charging more than the delivery posted.
    price_variance = models.DecimalField(
        _("price variance"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    #: The direct-charge lines — freight, repairs, subscriptions — which post
    #: to their own accounts and have nothing to do with matching.
    direct_charge_value = models.DecimalField(
        _("direct charges"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    #: What the supplier is owed: the whole invoice, never a re-derivation.
    payable_value = models.DecimalField(
        _("supplier payable"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )

    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="posted_supplier_invoice_postings",
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"))
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_supplier_invoice_postings",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    history = HistoricalRecords()

    #: Task 2.9's and Task 2.10's reversal guards ask each dependent relation
    #: which of its rows still stand. For a posting the answer is `LIVE`: a
    #: reversed generation is history and holds nothing hostage.
    live_dependency = Q(status="LIVE")

    class Meta:
        verbose_name = _("supplier invoice posting")
        verbose_name_plural = _("supplier invoice postings")
        ordering = ["supplier_invoice", "generation"]
        constraints = [
            models.UniqueConstraint(
                fields=["supplier_invoice", "generation"],
                name="procurement_posting_generation_unique",
            ),
            # At most one live generation per invoice. This is what makes
            # "already posted" a database fact rather than a service opinion,
            # and it is what two concurrent posts of one invoice collide on
            # when both pass the status check.
            models.UniqueConstraint(
                fields=["supplier_invoice"],
                condition=Q(status="LIVE"),
                name="procurement_one_live_posting_per_invoice",
            ),
            # And at most one live generation per match. A match is one agreed
            # answer; posting it twice would clear the same GRNI twice. Nulls
            # do not collide in a Postgres unique index, which is the right
            # answer here: two direct-charge invoices share no evidence.
            models.UniqueConstraint(
                fields=["purchase_match"],
                condition=Q(status="LIVE", purchase_match__isnull=False),
                name="procurement_one_live_posting_per_match",
            ),
            # Goods figures and a match arrive together or not at all.
            models.CheckConstraint(
                condition=Q(purchase_match__isnull=False)
                | Q(goods_cleared_value=0, invoice_matched_value=0, price_variance=0),
                name="procurement_posting_goods_need_a_match",
            ),
            models.CheckConstraint(
                condition=Q(generation__gte=1),
                name="procurement_posting_generation_is_positive",
            ),
            # The variance is its own arithmetic, asserted here as it is on the
            # allocation rows, so no path can store a difference its own
            # components do not support.
            models.CheckConstraint(
                condition=Q(
                    price_variance=models.F("invoice_matched_value")
                    - models.F("goods_cleared_value")
                ),
                name="procurement_posting_variance_is_the_difference",
            ),
            # The payable is the whole invoice: direct charges plus the
            # invoiced value of the matched goods. A full match makes the
            # second term exactly the inventory lines' net total, because the
            # final allocation against a line takes the exact remainder.
            models.CheckConstraint(
                condition=Q(
                    payable_value=models.F("direct_charge_value")
                    + models.F("invoice_matched_value")
                ),
                name="procurement_posting_payable_is_the_whole_invoice",
            ),
            models.CheckConstraint(
                condition=Q(goods_cleared_value__gte=0)
                & Q(invoice_matched_value__gte=0)
                & Q(direct_charge_value__gte=0)
                & Q(payable_value__gt=0),
                name="procurement_posting_values_are_not_negative",
            ),
            # A reversal is complete or it did not happen.
            models.CheckConstraint(
                condition=~Q(status="REVERSED")
                | Q(
                    reversal_journal_entry__isnull=False,
                    reversed_by__isnull=False,
                    reversed_at__isnull=False,
                )
                & ~Q(reversal_reason=""),
                name="procurement_posting_reversal_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(status="REVERSED")
                | Q(
                    reversal_journal_entry__isnull=True,
                    reversed_by__isnull=True,
                    reversed_at__isnull=True,
                    reversal_reason="",
                ),
                name="procurement_posting_live_carries_no_reversal",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="posting_org_status_idx"),
            models.Index(fields=["supplier_invoice", "status"], name="posting_invoice_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.supplier_invoice.supplier_invoice_number} · {self.generation}"

    @property
    def is_live(self) -> bool:
        return self.status == SupplierInvoicePostingStatus.LIVE


# ---------------------------------------------------------------------------
# Supplier returns (Task 2.13)
# ---------------------------------------------------------------------------


class SupplierReturnStatus(models.TextChoices):
    """
    Three states, and the same shape as every other posting document here.

    `POSTED` is the moment stock leaves and the ledger moves. `REVERSED` is the
    correction — never an edit, never a delete, because the movement it made is
    append-only by design.
    """

    DRAFT = "DRAFT", _("مسودة")
    POSTED = "POSTED", _("مرحّل")
    REVERSED = "REVERSED", _("معكوس")


class SupplierReturn(TimeStampedModel):
    """
    Goods going back out to the supplier they came from.

    The fourth procurement event, and the first that sends stock *out*. Task
    2.0 §10 gives the header; the lines below it are Task 2.13's addition,
    because §10's sketch has nowhere to record which delivery line came back
    or how much of it — and without that there is no quantity to move, no value
    to post, and no way to stop the same fifty kilograms being returned twice.

    ## What it posts, and what it deliberately does not

        Dr  SUPPLIER_RETURN_CLEARING     the book value that left
            Cr  Inventory control        the same figure

    and nothing else. **No variance at the return.** The supplier has not yet
    said what they will credit, and booking a gain or a loss against an
    expectation would put a figure on the profit and loss that nobody has
    agreed to. The difference between this book value and the credit that
    eventually arrives is `PURCHASE_RETURN_VARIANCE`, recognised by Task 2.14's
    credit note. Until then the clearing balance *is* the claim outstanding —
    which is what the breakdown's "supplier-credit-expected state" amounts to.

    **No payable and no GRNI.** Both were considered and both are wrong here.
    Debiting the payable would move a liability with no document stating its
    amount; debiting GRNI would make the return's accounting depend on whether
    an invoice had arrived, and Task 2.13 depends on 2.9, not on the invoice —
    goods go back before anyone has agreed a price often enough that the
    breakdown says so in as many words.

    ## Valuation

    Stock leaves at the **standing moving average**, like every other outbound,
    and a return that empties a position surrenders its whole remaining book
    value (ADR-018's full-depletion rule, PRC-048). Not at the receipt price:
    that would need a cost layer this system does not keep, and under a moving
    average it would be a fiction — if 100 kg arrived at 1,000 and 100 kg at
    2,000, there is no kilogram in the warehouse that *is* the cheap one.

    The consequence is the thing that will look like a bug, and ADR-022 §2
    exists to explain it: what leaves inventory and what the supplier credits
    are different numbers, on purpose.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="supplier_returns",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="supplier_returns",
        verbose_name=_("branch"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="returns",
        verbose_name=_("supplier"),
    )
    #: The delivery being sent back. Header-level as Task 2.0 §10 specifies,
    #: and every line must cite a line of *this* receipt — one return, one
    #: delivery, because the credit note that follows will be about one
    #: delivery too.
    receipt = models.ForeignKey(
        GoodsReceipt,
        on_delete=models.PROTECT,
        related_name="returns",
        verbose_name=_("goods receipt"),
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        related_name="supplier_returns",
        verbose_name=_("warehouse"),
    )
    location = models.ForeignKey(
        "inventory.StockLocation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_returns",
        verbose_name=_("location"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    number = models.CharField(_("number"), max_length=32, blank=True)

    returned_at = models.DateField(_("returned at"))
    business_date = models.DateField(_("business date"))
    business_date_timezone = models.CharField(
        _("business date timezone"), max_length=64, blank=True
    )
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)

    #: Why the goods went back. Reuses inventory's vocabulary rather than
    #: inventing a procurement one, exactly as Task 2.9 did for rejection.
    reason_code = models.ForeignKey(
        "inventory.InventoryReasonCode",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_returns",
        verbose_name=_("reason code"),
    )
    reason = models.TextField(_("reason"), blank=True)
    #: The supplier's own reference for the return — a collection note, a
    #: driver's signature. Evidence, never an identity.
    evidence_reference = models.CharField(_("evidence reference"), max_length=120, blank=True)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=SupplierReturnStatus.choices,
        default=SupplierReturnStatus.DRAFT,
    )

    #: What the stock ledger actually removed, summed over the lines. Stored
    #: because it is the figure the journal posted and the figure Task 2.14
    #: will clear — never re-derived from today's average, which has moved.
    posted_value = models.DecimalField(
        _("posted value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )
    stock_entry = models.ForeignKey(
        "inventory.StockLedgerEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_returns",
        verbose_name=_("stock entry"),
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_returns",
        verbose_name=_("journal entry"),
    )
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_supplier_returns",
        verbose_name=_("reversal journal entry"),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_supplier_returns",
        verbose_name=_("created by"),
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_supplier_returns",
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_supplier_returns",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    notes = models.TextField(_("notes"), blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("supplier return")
        verbose_name_plural = _("supplier returns")
        ordering = ["-id"]
        permissions = [
            ("create_supplier_return", _("Can record a supplier return")),
            ("post_supplier_return", _("Can post a supplier return")),
            ("reverse_supplier_return", _("Can reverse a posted supplier return")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="procurement_return_number_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(posted_by__isnull=True, posted_at__isnull=True)
                | Q(posted_by__isnull=False, posted_at__isnull=False),
                name="procurement_return_posting_is_complete",
            ),
            # A draft has moved nothing.
            models.CheckConstraint(
                condition=~Q(status="DRAFT")
                | Q(
                    posted_at__isnull=True,
                    stock_entry__isnull=True,
                    journal_entry__isnull=True,
                    posted_value__isnull=True,
                ),
                name="procurement_return_draft_has_moved_nothing",
            ),
            # A posted return names what it moved and what explains it. With
            # the immutability trigger this is what makes "stock out with no
            # journal" a shape the table cannot hold.
            models.CheckConstraint(
                condition=~Q(status__in=["POSTED", "REVERSED"])
                | Q(
                    posted_at__isnull=False,
                    stock_entry__isnull=False,
                    journal_entry__isnull=False,
                    posted_value__isnull=False,
                    business_date_timezone__gt="",
                    business_day_start__isnull=False,
                ),
                name="procurement_return_posted_names_what_it_moved",
            ),
            models.CheckConstraint(
                condition=~Q(status="REVERSED")
                | Q(
                    reversed_by__isnull=False,
                    reversed_at__isnull=False,
                    reversal_journal_entry__isnull=False,
                )
                & ~Q(reversal_reason=""),
                name="procurement_return_reversal_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(status="REVERSED")
                | Q(
                    reversed_by__isnull=True,
                    reversed_at__isnull=True,
                    reversal_journal_entry__isnull=True,
                    reversal_reason="",
                ),
                name="procurement_return_unreversed_carries_no_reversal",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="sret_org_status_idx"),
            models.Index(fields=["supplier", "status"], name="sret_supplier_status_idx"),
            models.Index(fields=["receipt"], name="sret_receipt_idx"),
        ]

    #: Task 2.9's reversal guard asks each dependent relation which of its rows
    #: still stand. A reversed return has given the goods back and holds the
    #: delivery hostage no longer — the same rule Task 2.11 and 2.12 declare,
    #: and declared on the model that holds the FK so the guard can read it.
    #: The `Q` is applied to a queryset of **this** model (the receipt's
    #: `returns` accessor), so it names `status` directly; the line model
    #: below reaches the same fact through its header.
    live_dependency = Q(status__in=("DRAFT", "POSTED"))

    def __str__(self) -> str:
        return self.number or f"{self.supplier.code} · {self.receipt}"

    @property
    def is_editable(self) -> bool:
        return self.status == SupplierReturnStatus.DRAFT


class SupplierReturnLine(TimeStampedModel):
    """
    One delivery line going back, in part or in whole.

    Task 2.0 §10's sketch has no line model. It cannot: a header with a receipt
    FK records *that* something was returned and never *what* — not which item,
    not how much, not from which lot. Every question Task 2.13 has to answer
    needs the line, and so does every guard: the quantity bound is per delivery
    line, the stock movement is per lot and location, and Task 2.9's reversal
    guard walks `GoodsReceiptLine`'s relations, so a header-only link would
    leave a posted return invisible to it.

    The shape is `GoodsReceiptLine`'s, deliberately: the two documents are
    mirror images and a reader who knows one should recognise the other.
    """

    supplier_return = models.ForeignKey(
        SupplierReturn,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("supplier return"),
    )
    line_uid = models.UUIDField(_("line uid"), default=uuid.uuid4, editable=False)
    sequence = models.PositiveIntegerField(_("sequence"))

    #: `PROTECT`, and the reason the guard exists: this line is a claim about
    #: that delivery, and the delivery cannot be unmade underneath it.
    goods_receipt_line = models.ForeignKey(
        GoodsReceiptLine,
        on_delete=models.PROTECT,
        related_name="supplier_return_lines",
        verbose_name=_("delivery line"),
    )
    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="supplier_return_lines",
        verbose_name=_("item"),
    )
    lot = models.ForeignKey(
        "inventory.InventoryLot",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_return_lines",
        verbose_name=_("lot"),
    )

    #: Base units, always — the delivery may have arrived in sacks and the
    #: return may be in kilograms, and only the base unit compares.
    returned_base_quantity = models.DecimalField(
        _("returned quantity (base)"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
    )

    #: What the kernel actually removed, at the standing average. Written by
    #: posting and never by a caller: only the kernel knows what a full
    #: depletion surrendered.
    posted_value = models.DecimalField(
        _("posted value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )
    #: What the supplier is expected to credit for this line. **Metadata.** It
    #: posts nothing, it is not the journal's figure, and the variance is not
    #: computed from it — an expectation is not an agreement. It exists so the
    #: screen and the report can say what is being claimed, and so Task 2.14
    #: has something to compare the credit note against.
    expected_credit_value = models.DecimalField(
        _("expected credit"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )

    movement = models.ForeignKey(
        "inventory.StockMovement",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_return_lines",
        verbose_name=_("stock movement"),
    )
    inventory_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_return_inventory_lines",
        verbose_name=_("inventory account"),
    )
    contra_account = models.ForeignKey(
        "accounting.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_return_contra_lines",
        verbose_name=_("clearing account"),
    )

    note = models.CharField(_("note"), max_length=200, blank=True)

    history = HistoricalRecords()

    #: What the receipt-side guard reads. Expressed through the header because
    #: the status lives there — Task 2.12 declared this on a model whose FK
    #: pointed at a header and the guard could never consult it, which is a
    #: mistake worth not repeating.
    live_dependency = Q(supplier_return__status__in=("DRAFT", "POSTED"))

    class Meta:
        verbose_name = _("supplier return line")
        verbose_name_plural = _("supplier return lines")
        ordering = ["supplier_return", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["supplier_return", "sequence"],
                name="procurement_return_line_sequence_unique",
            ),
            models.CheckConstraint(
                condition=Q(returned_base_quantity__gt=0),
                name="procurement_return_line_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(posted_value__isnull=True) | Q(posted_value__gte=0),
                name="procurement_return_line_posted_value_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(expected_credit_value__isnull=True) | Q(expected_credit_value__gte=0),
                name="procurement_return_line_expected_credit_not_negative",
            ),
            # A posted line names the movement it made and the two accounts it
            # moved between, or none of them.
            models.CheckConstraint(
                condition=Q(movement__isnull=True, inventory_account__isnull=True)
                | Q(movement__isnull=False, inventory_account__isnull=False),
                name="procurement_return_line_movement_names_its_account",
            ),
        ]
        indexes = [
            models.Index(fields=["goods_receipt_line"], name="sret_line_receipt_line_idx"),
            models.Index(fields=["item"], name="sret_line_item_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.supplier_return} · {self.sequence}"


class SupplierCreditNoteStatus(models.TextChoices):
    """Three states, the same shape as the return it settles."""

    DRAFT = "DRAFT", _("مسودة")
    POSTED = "POSTED", _("مرحّل")
    REVERSED = "REVERSED", _("معكوس")


class SupplierCreditNote(TimeStampedModel):
    """
    The supplier's agreed figure for a return — the paper the claim waited for.

    Task 2.13 sent goods back and parked their book value in
    `SUPPLIER_RETURN_CLEARING`; the balance *is* the claim outstanding. This
    document is the supplier's answer, and posting it is what ADR-022 §2 (as
    amended) deferred to here:

        Dr  Supplier payable            the agreed credit
            Cr  Supplier return clearing  the return's book value, the claim closed
            Cr/Dr Purchase return variance  the difference, either direction

    with the variance line **absent** when the figures agree — the kernel
    refuses a zero line and it would be noise anyway.

    ## Settlement is partial, per line, and may take several notes

    The approved rule: a note settles a return **through explicit allocations
    to its lines** (`SupplierCreditReturnAllocation`), a note may cover
    several lines, and a line may be settled by several notes across time —
    bounded so the credited quantity and the settled book value never exceed
    what the line posted. The journal's clearing credit is the sum of the
    settled book values, so the clearing balance always equals exactly the
    claims still open, with no rounding residual: a partial settlement takes
    its quantized proportional share and the final one takes the exact
    remainder.

    ## Scope, recorded rather than implied

    A Release 1 credit note **must cite a posted supplier return**. A note
    citing only an invoice (a price adjustment with no goods back) or nothing
    at all has no approved contra account anywhere — no role in Task 2.0 §15,
    no journal shape in any accepted ADR — and inventing one would put a
    figure in the ledger no approved document supports. The same reasoning
    recorded "invoice-before-receipt is not supported" at Task 2.10. Both
    cases are deferred, not designed.

    PRC-051's two outcomes are **allocation states, not accounts**: the debit
    is the payable either way. Allocated against posted invoices, the note
    reduces what those invoices still owe; unallocated, it stands as supplier
    credit — a debit the payable account carries until an invoice or Task
    2.15's payment run consumes it. Booking the unallocated remainder to a
    different asset account would break invariant 46 (open balances sum to
    the payable account) the day the first one posted.

    It never moves stock. The goods left with the return.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="supplier_credit_notes",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="supplier_credit_notes",
        verbose_name=_("branch"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="credit_notes",
        verbose_name=_("supplier"),
    )
    #: The return this note answers. Required in Release 1 — see the class
    #: docstring. What it *settles* is stated line by line in the allocations
    #: below, and may be part of the claim: the rest waits for the next note.
    supplier_return = models.ForeignKey(
        SupplierReturn,
        on_delete=models.PROTECT,
        related_name="credit_notes",
        verbose_name=_("supplier return"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    number = models.CharField(_("number"), max_length=32, blank=True)

    #: The supplier's own reference, stored exactly as they wrote it, and the
    #: folded key beside it — the same duplicate protection invoices get
    #: (PRC-052), in reverse: crediting the same note twice is the expensive
    #: direction here.
    supplier_document_number = models.CharField(_("supplier document number"), max_length=64)
    supplier_document_number_key = models.CharField(
        _("supplier document number (key)"), max_length=64, editable=False
    )

    #: The date on the supplier's paper. Theirs.
    credit_date = models.DateField(_("credit date"))
    #: The branch business date this posts on. Ours (ADR-008).
    business_date = models.DateField(_("business date"))
    business_date_timezone = models.CharField(
        _("business date timezone"), max_length=64, blank=True
    )
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)

    #: The agreed credit, stated by the supplier's document — never derived
    #: from the return's book value or the operator's expectation.
    amount = models.DecimalField(
        _("amount"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    reason = models.TextField(_("reason"), blank=True)
    notes = models.TextField(_("notes"), blank=True)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=SupplierCreditNoteStatus.choices,
        default=SupplierCreditNoteStatus.DRAFT,
    )

    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_credit_notes",
        verbose_name=_("journal entry"),
    )
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_supplier_credit_notes",
        verbose_name=_("reversal journal entry"),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_supplier_credit_notes",
        verbose_name=_("created by"),
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_supplier_credit_notes",
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_supplier_credit_notes",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    history = HistoricalRecords()

    #: What the return's reversal guard reads: a standing note pins its
    #: return, a reversed one releases it.
    live_dependency = Q(status__in=("DRAFT", "POSTED"))

    class Meta:
        verbose_name = _("supplier credit note")
        verbose_name_plural = _("supplier credit notes")
        ordering = ["-id"]
        permissions = [
            ("create_supplier_credit_note", _("Can record a supplier credit note")),
            ("post_supplier_credit_note", _("Can post a supplier credit note")),
            ("reverse_supplier_credit_note", _("Can reverse a posted supplier credit note")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="procurement_scn_number_unique_per_organization",
            ),
            # PRC-052: unique per supplier over non-reversed notes.
            models.UniqueConstraint(
                fields=["organization", "supplier", "supplier_document_number_key"],
                condition=~Q(status="REVERSED"),
                name="procurement_scn_document_unique_per_supplier",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="procurement_scn_amount_positive",
            ),
            models.CheckConstraint(
                condition=~Q(supplier_document_number="") & ~Q(supplier_document_number_key=""),
                name="procurement_scn_states_the_supplier_reference",
            ),
            models.CheckConstraint(
                condition=Q(posted_by__isnull=True, posted_at__isnull=True)
                | Q(posted_by__isnull=False, posted_at__isnull=False),
                name="procurement_scn_posting_is_complete",
            ),
            models.CheckConstraint(
                condition=~Q(status="DRAFT")
                | Q(posted_at__isnull=True, journal_entry__isnull=True),
                name="procurement_scn_draft_has_posted_nothing",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=["POSTED", "REVERSED"])
                | Q(
                    posted_at__isnull=False,
                    journal_entry__isnull=False,
                    business_date_timezone__gt="",
                    business_day_start__isnull=False,
                ),
                name="procurement_scn_posted_names_its_journal",
            ),
            models.CheckConstraint(
                condition=~Q(status="REVERSED")
                | Q(
                    reversed_by__isnull=False,
                    reversed_at__isnull=False,
                    reversal_journal_entry__isnull=False,
                )
                & ~Q(reversal_reason=""),
                name="procurement_scn_reversal_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(status="REVERSED")
                | Q(
                    reversed_by__isnull=True,
                    reversed_at__isnull=True,
                    reversal_journal_entry__isnull=True,
                    reversal_reason="",
                ),
                name="procurement_scn_unreversed_carries_no_reversal",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="scn_org_status_idx"),
            models.Index(fields=["supplier", "status"], name="scn_supplier_status_idx"),
            models.Index(fields=["supplier_return"], name="scn_return_idx"),
        ]

    def __str__(self) -> str:
        return self.number or f"{self.supplier.code} · {self.supplier_document_number}"

    @property
    def is_editable(self) -> bool:
        return self.status == SupplierCreditNoteStatus.DRAFT


class SupplierCreditAllocation(TimeStampedModel):
    """
    This much of one credit note nets against this posted invoice.

    The journal already reduced the payable by the note's whole amount; an
    allocation decides **which invoice's outstanding** that reduction settles,
    exactly the role Task 2.15's payment allocation will play. The sum of a
    note's allocations may not exceed its amount, and no allocation may exceed
    what its invoice still owes — both re-checked under locks at posting.
    The remainder stands as unallocated supplier credit (PRC-051).
    """

    credit_note = models.ForeignKey(
        SupplierCreditNote,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name=_("credit note"),
    )
    sequence = models.PositiveIntegerField(_("sequence"))
    invoice = models.ForeignKey(
        SupplierInvoice,
        on_delete=models.PROTECT,
        related_name="credit_allocations",
        verbose_name=_("invoice"),
    )
    allocated_amount = models.DecimalField(
        _("allocated amount"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    note = models.CharField(_("note"), max_length=200, blank=True)

    history = HistoricalRecords()

    #: What the invoice's reversal guard reads, through the header where the
    #: status lives — the Task 2.12 lesson, kept.
    live_dependency = Q(credit_note__status__in=("DRAFT", "POSTED"))

    class Meta:
        verbose_name = _("supplier credit allocation")
        verbose_name_plural = _("supplier credit allocations")
        ordering = ["credit_note", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["credit_note", "sequence"],
                name="procurement_scn_allocation_sequence_unique",
            ),
            # One row per (note, invoice): a second share against the same
            # invoice is an edit of the first, not a new fact.
            models.UniqueConstraint(
                fields=["credit_note", "invoice"],
                name="procurement_scn_allocation_invoice_unique",
            ),
            models.CheckConstraint(
                condition=Q(allocated_amount__gt=0),
                name="procurement_scn_allocation_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["invoice"], name="scn_alloc_invoice_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.credit_note} · {self.sequence}"


class SupplierCreditReturnAllocation(TimeStampedModel):
    """
    This much of one credit note settles this much of one return line.

    The settlement is **explicit and partial**: a note may cover several of
    its return's lines, a line may be settled by several notes across time,
    and the bound is per line — the active credited quantity may not exceed
    what the line returned, and the active settled book value may not exceed
    what it posted.

    `settled_book_value` is written by posting and never by a caller: it is
    the quantized proportional share of the line's **remaining** clearing
    value, and the allocation that takes the last of the quantity takes the
    exact remaining value — so the invariant

        active settled book values + remaining clearing
            == the line's posted book value

    holds to the fils and no rounding residual can strand in
    `SUPPLIER_RETURN_CLEARING`. The variance each allocation recognises is
    its credited amount less this settled value.
    """

    credit_note = models.ForeignKey(
        SupplierCreditNote,
        on_delete=models.CASCADE,
        related_name="return_allocations",
        verbose_name=_("credit note"),
    )
    allocation_uid = models.UUIDField(
        _("allocation uid"), default=uuid.uuid4, editable=False, unique=True
    )
    sequence = models.PositiveIntegerField(_("sequence"))
    supplier_return_line = models.ForeignKey(
        SupplierReturnLine,
        on_delete=models.PROTECT,
        related_name="credit_allocations",
        verbose_name=_("return line"),
    )

    #: How much of the line's returned quantity this note credits. Base
    #: units, 3 dp, and the handle the proportional settlement is computed
    #: from.
    credited_base_quantity = models.DecimalField(
        _("credited quantity (base)"),
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_PLACES,
    )
    #: The supplier's money for this slice — the share of the note's amount
    #: attributed to this line. The variance side of the journal.
    allocated_credit_amount = models.DecimalField(
        _("allocated credit"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
    )
    #: The book value this slice closes out of the clearing account. Written
    #: by posting under locks; NULL on a draft.
    settled_book_value = models.DecimalField(
        _("settled book value"),
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        null=True,
        blank=True,
    )
    note = models.CharField(_("note"), max_length=200, blank=True)

    history = HistoricalRecords()

    #: What the return side's guards read, through the header where the
    #: status lives: a reversed note's settlement releases everything.
    live_dependency = Q(credit_note__status__in=("DRAFT", "POSTED"))

    class Meta:
        verbose_name = _("supplier credit return allocation")
        verbose_name_plural = _("supplier credit return allocations")
        ordering = ["credit_note", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["credit_note", "sequence"],
                name="procurement_scn_ralloc_sequence_unique",
            ),
            # One row per (note, line): a second slice against the same line
            # is an edit of the first, not a new fact.
            models.UniqueConstraint(
                fields=["credit_note", "supplier_return_line"],
                name="procurement_scn_ralloc_line_unique",
            ),
            models.CheckConstraint(
                condition=Q(credited_base_quantity__gt=0),
                name="procurement_scn_ralloc_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(allocated_credit_amount__gt=0),
                name="procurement_scn_ralloc_credit_positive",
            ),
            models.CheckConstraint(
                condition=Q(settled_book_value__isnull=True) | Q(settled_book_value__gte=0),
                name="procurement_scn_ralloc_settled_not_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["supplier_return_line"], name="scn_ralloc_line_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.credit_note} · {self.sequence}"


class SupplierPaymentMethod(models.TextChoices):
    """How the money left. The account follows from this through a role."""

    CASH = "CASH", _("نقداً")
    BANK = "BANK", _("حوالة مصرفية")


class SupplierPaymentStatus(models.TextChoices):
    DRAFT = "DRAFT", _("مسودة")
    POSTED = "POSTED", _("مرحّل")
    REVERSED = "REVERSED", _("معكوس")


class SupplierPayment(TimeStampedModel):
    """
    Money leaving for a supplier — Task 2.15, and the phase's last posting
    document.

        Dr  Supplier payable    the allocated amount
        Dr  Supplier advance    the unallocated remainder, where any
            Cr  Cash or bank    the full amount

    The source account is resolved by `method` through an effective-dated
    role (PRC-056), never an id. Allocations say which invoices the money
    settles (PRC-053); the remainder is an **asset** — a prepayment — and
    never a negative payable (PRC-055). Consuming a standing advance against
    a later invoice has no approved journal shape and is deferred, not
    designed — the same discipline that scoped the credit note.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="supplier_payments",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="supplier_payments",
        verbose_name=_("branch"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="payments",
        verbose_name=_("supplier"),
    )

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    number = models.CharField(_("number"), max_length=32, blank=True)

    paid_at = models.DateField(_("paid at"))
    business_date = models.DateField(_("business date"))
    business_date_timezone = models.CharField(
        _("business date timezone"), max_length=64, blank=True
    )
    business_day_start = models.TimeField(_("business day start"), null=True, blank=True)

    method = models.CharField(_("method"), max_length=8, choices=SupplierPaymentMethod.choices)
    amount = models.DecimalField(
        _("amount"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    #: The cheque or transfer id, stored as written. Evidence, not identity.
    reference = models.CharField(_("reference"), max_length=64, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=SupplierPaymentStatus.choices,
        default=SupplierPaymentStatus.DRAFT,
    )

    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supplier_payments",
        verbose_name=_("journal entry"),
    )
    reversal_journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_supplier_payments",
        verbose_name=_("reversal journal entry"),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_supplier_payments",
        verbose_name=_("created by"),
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_supplier_payments",
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_supplier_payments",
        verbose_name=_("reversed by"),
    )
    reversed_at = models.DateTimeField(_("reversed at"), null=True, blank=True)
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    history = HistoricalRecords()

    live_dependency = Q(status__in=("DRAFT", "POSTED"))

    class Meta:
        verbose_name = _("supplier payment")
        verbose_name_plural = _("supplier payments")
        ordering = ["-id"]
        permissions = [
            ("create_supplier_payment", _("Can record a supplier payment")),
            ("post_supplier_payment", _("Can post a supplier payment")),
            ("reverse_supplier_payment", _("Can reverse a posted supplier payment")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="procurement_spay_number_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="procurement_spay_amount_positive",
            ),
            models.CheckConstraint(
                condition=Q(posted_by__isnull=True, posted_at__isnull=True)
                | Q(posted_by__isnull=False, posted_at__isnull=False),
                name="procurement_spay_posting_is_complete",
            ),
            models.CheckConstraint(
                condition=~Q(status="DRAFT")
                | Q(posted_at__isnull=True, journal_entry__isnull=True),
                name="procurement_spay_draft_has_posted_nothing",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=["POSTED", "REVERSED"])
                | Q(
                    posted_at__isnull=False,
                    journal_entry__isnull=False,
                    business_date_timezone__gt="",
                    business_day_start__isnull=False,
                ),
                name="procurement_spay_posted_names_its_journal",
            ),
            models.CheckConstraint(
                condition=~Q(status="REVERSED")
                | Q(
                    reversed_by__isnull=False,
                    reversed_at__isnull=False,
                    reversal_journal_entry__isnull=False,
                )
                & ~Q(reversal_reason=""),
                name="procurement_spay_reversal_is_complete",
            ),
            models.CheckConstraint(
                condition=Q(status="REVERSED")
                | Q(
                    reversed_by__isnull=True,
                    reversed_at__isnull=True,
                    reversal_journal_entry__isnull=True,
                    reversal_reason="",
                ),
                name="procurement_spay_unreversed_carries_no_reversal",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="spay_org_status_idx"),
            models.Index(fields=["supplier", "status"], name="spay_supplier_status_idx"),
        ]

    def __str__(self) -> str:
        return self.number or f"{self.supplier.code} · {self.amount}"

    @property
    def is_editable(self) -> bool:
        return self.status == SupplierPaymentStatus.DRAFT


class PaymentAllocation(TimeStampedModel):
    """
    This much of one payment settles this posted invoice (PRC-053).

    Bounded twice: the sum on one payment may not exceed its amount, and no
    allocation may exceed what its invoice still owes — net of credit notes
    and other payments, which is stricter than PRC-054's "its total" and
    deliberate: paying a debt a credit note already settled is the expensive
    direction to be wrong in. Both re-checked at posting under the invoice
    row locks, and again by reconciliation.
    """

    payment = models.ForeignKey(
        SupplierPayment,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name=_("payment"),
    )
    sequence = models.PositiveIntegerField(_("sequence"))
    invoice = models.ForeignKey(
        SupplierInvoice,
        on_delete=models.PROTECT,
        related_name="payment_allocations",
        verbose_name=_("invoice"),
    )
    allocated_amount = models.DecimalField(
        _("allocated amount"), max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    note = models.CharField(_("note"), max_length=200, blank=True)

    history = HistoricalRecords()

    #: What the invoice's header-level reversal guard reads.
    live_dependency = Q(payment__status__in=("DRAFT", "POSTED"))

    class Meta:
        verbose_name = _("payment allocation")
        verbose_name_plural = _("payment allocations")
        ordering = ["payment", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "sequence"],
                name="procurement_spay_allocation_sequence_unique",
            ),
            models.UniqueConstraint(
                fields=["payment", "invoice"],
                name="procurement_spay_allocation_invoice_unique",
            ),
            models.CheckConstraint(
                condition=Q(allocated_amount__gt=0),
                name="procurement_spay_allocation_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["invoice"], name="spay_alloc_invoice_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.payment} · {self.sequence}"
