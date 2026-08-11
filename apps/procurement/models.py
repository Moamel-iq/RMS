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

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TimeStampedModel
from apps.core.money import MONEY_PLACES, UNIT_PRICE_PLACES
from apps.core.quantity import QUANTITY_PLACES

#: Supplier codes. The same shape inventory item codes use, and canonicalised
#: to uppercase before storage so uniqueness is case-insensitive in effect
#: without a functional index.
CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._-]*$"

#: Room for a credit limit in IQD, a currency with large nominal amounts.
MONEY_MAX_DIGITS = MONEY_PLACES + 18
UNIT_PRICE_MAX_DIGITS = UNIT_PRICE_PLACES + 15
QUANTITY_MAX_DIGITS = QUANTITY_PLACES + 15


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
