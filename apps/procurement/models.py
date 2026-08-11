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
from apps.core.money import MONEY_PLACES

#: Supplier codes. The same shape inventory item codes use, and canonicalised
#: to uppercase before storage so uniqueness is case-insensitive in effect
#: without a functional index.
CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._-]*$"

#: Room for a credit limit in IQD, a currency with large nominal amounts.
MONEY_MAX_DIGITS = MONEY_PLACES + 18


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
