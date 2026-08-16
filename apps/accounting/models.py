"""
The accounting kernel.

Two things live here and nothing else: the structures money is recorded
against (settings, periods, accounts, cost centres) and the ledger itself
(journal entries and lines). No module-specific posting rules — purchases,
sales, payroll, and inventory will call this kernel, never extend it.

See docs/specs/accounting-kernel-invariants.md for the twelve invariants this
must satisfy, and ADR-012 through ADR-015 for the decisions behind them.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TimeStampedModel
from apps.core.money import MONEY_PLACES

#: Amounts are stored at posted precision (ADR-012). Fifteen integer digits is
#: far beyond any plausible IQD figure and leaves the column comfortable.
AMOUNT_MAX_DIGITS = MONEY_PLACES + 15

#: Account code shapes, one per level of the C-GG-SS-AAA hierarchy (ADR-014).
#: The code carries the level, so a detail account is recognisable without a
#: separate field that could disagree with it.
CLASS_CODE_PATTERN = r"^[1-9]$"
GROUP_CODE_PATTERN = r"^[1-9]-[0-9]{2}$"
SUBGROUP_CODE_PATTERN = r"^[1-9]-[0-9]{2}-[0-9]{2}$"
DETAIL_CODE_PATTERN = r"^[1-9]-[0-9]{2}-[0-9]{2}-[0-9]{3}$"

ANY_ACCOUNT_CODE_PATTERN = r"^[1-9](-[0-9]{2}(-[0-9]{2}(-[0-9]{3})?)?)?$"

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_-]*$"


class AccountClass(models.TextChoices):
    """The first segment of an account code (ADR-014)."""

    ASSET = "1", _("الأصول")
    LIABILITY = "2", _("الالتزامات")
    EQUITY = "3", _("حقوق الملكية")
    REVENUE = "4", _("الإيرادات")
    COST_OF_SALES = "5", _("كلفة المبيعات")
    OPERATING_EXPENSE = "6", _("المصروفات التشغيلية")
    OTHER = "7", _("إيرادات ومصروفات أخرى")
    CLEARING = "8", _("حسابات وسيطة ورقابية")
    MEMO = "9", _("إحصائية")


#: Which classes require a cost centre by default (ADR-015). Revenue, COGS,
#: and operating expenses drive managerial analysis; balance-sheet control
#: accounts have no meaningful answer and a forced value would corrupt it.
CLASSES_REQUIRING_COST_CENTER = frozenset(
    {
        AccountClass.REVENUE,
        AccountClass.COST_OF_SALES,
        AccountClass.OPERATING_EXPENSE,
    }
)


class AccountingSettings(TimeStampedModel):
    """
    Per-organization accounting configuration.

    Organization-level rather than a global constant, so a second organization
    can differ (ADR-013).
    """

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="accounting_settings",
        verbose_name=_("organization"),
    )
    fiscal_year_start_month = models.PositiveSmallIntegerField(
        _("fiscal year start month"),
        default=1,
        help_text=_("Changing this re-buckets every posted entry. See ADR-013."),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("accounting settings")
        verbose_name_plural = _("accounting settings")
        constraints = [
            models.CheckConstraint(
                condition=Q(fiscal_year_start_month__gte=1) & Q(fiscal_year_start_month__lte=12),
                name="accounting_settings_month_in_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.code} accounting settings"


class PeriodState(models.TextChoices):
    OPEN = "OPEN", _("مفتوحة")
    SOFT_CLOSED = "SOFT_CLOSED", _("مغلقة مبدئياً")
    CLOSED = "CLOSED", _("مغلقة")


class FiscalYear(TimeStampedModel):
    """A financial year for one organization."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="fiscal_years",
        verbose_name=_("organization"),
    )
    year = models.PositiveSmallIntegerField(_("year"))
    start_date = models.DateField(_("start date"))
    end_date = models.DateField(_("end date"))

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("fiscal year")
        verbose_name_plural = _("fiscal years")
        ordering = ["organization__code", "-year"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "year"], name="fiscal_year_unique_per_organization"
            ),
            models.CheckConstraint(
                condition=Q(end_date__gt=models.F("start_date")),
                name="fiscal_year_ends_after_it_starts",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.code} {self.year}"

    @property
    def is_closed(self) -> bool:
        """
        Derived, never stored.

        A stored flag would be a second source of truth that could disagree
        with the periods it summarises — and the periods are the ones postings
        are actually checked against. A year is closed when every period in it
        is closed, and not a moment before.
        """
        periods = self.periods.all()
        return bool(periods) and all(period.state == PeriodState.CLOSED for period in periods)


class AccountingPeriod(TimeStampedModel):
    """
    One of twelve monthly buckets in a fiscal year (ADR-013).

    A journal keeps its real accounting date; the period is a bucket over
    those dates, never a replacement for one.
    """

    fiscal_year = models.ForeignKey(
        FiscalYear,
        on_delete=models.PROTECT,
        related_name="periods",
        verbose_name=_("fiscal year"),
    )
    period_number = models.PositiveSmallIntegerField(_("period number"))
    start_date = models.DateField(_("start date"))
    end_date = models.DateField(_("end date"))
    state = models.CharField(
        _("state"), max_length=16, choices=PeriodState.choices, default=PeriodState.OPEN
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("accounting period")
        verbose_name_plural = _("accounting periods")
        ordering = ["fiscal_year__year", "period_number"]
        permissions = [
            ("soft_close_period", _("Can soft-close an accounting period")),
            ("close_period", _("Can close an accounting period")),
            ("reopen_period", _("Can reopen a closed accounting period")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["fiscal_year", "period_number"],
                name="period_unique_per_fiscal_year",
            ),
            models.CheckConstraint(
                condition=Q(period_number__gte=1) & Q(period_number__lte=12),
                name="period_number_in_range",
            ),
            models.CheckConstraint(
                condition=Q(end_date__gte=models.F("start_date")),
                name="period_ends_after_it_starts",
            ),
        ]

    @property
    def is_open(self) -> bool:
        return self.state == PeriodState.OPEN

    @property
    def accepts_normal_postings(self) -> bool:
        return self.state == PeriodState.OPEN

    def __str__(self) -> str:
        return f"{self.fiscal_year.year}-{self.period_number:02d}"


class CostCenter(TimeStampedModel):
    """
    A managerial dimension, owned by the organization rather than a branch
    (ADR-015).

    Kitchen, Hall, Warehouse, Delivery, Administration, and HR exist at every
    branch, so branch ownership would force a duplicate per branch and make
    cross-branch analysis impossible. If particular branches ever need to be
    restricted to particular cost centres, that becomes an explicit
    applicability model — not a change of ownership.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="cost_centers",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=20)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200)
    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("cost center")
        verbose_name_plural = _("cost centers")
        ordering = ["organization__code", "code"]
        permissions = [
            ("manage_cost_centers", _("Can create and archive cost centers")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="cost_center_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="cost_center_code_format"
            ),
            models.CheckConstraint(
                condition=~Q(name_ar="") & ~Q(name_en=""),
                name="cost_center_names_not_empty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


class Account(TimeStampedModel):
    """
    A chart-of-accounts node.

    The code carries the level: `1` is a class, `1-01` a group, `1-01-01` a
    subgroup, `1-01-01-001` a posting account. `is_postable` is stored so the
    database can enforce "only leaf accounts accept journal lines", and a
    constraint pins it to agree with the code shape — the two can never drift.

    Codes are strings. Leading zeros are significant and arithmetic on an
    account code is always a bug.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="accounts",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=20)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200)

    account_class = models.CharField(_("class"), max_length=1, choices=AccountClass.choices)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        verbose_name=_("parent account"),
    )

    is_postable = models.BooleanField(
        _("accepts journal lines"),
        default=False,
        help_text=_("True only for detail accounts. Parents are reporting rollups."),
    )
    requires_cost_center = models.BooleanField(
        _("requires a cost center"),
        default=False,
        help_text=_("Revenue, COGS, and operating expenses; not balance-sheet accounts."),
    )
    is_active = models.BooleanField(_("active"), default=True)

    #: Optional mapping to a statutory chart, e.g. the Iraqi Unified
    #: Accounting System. Never affects posting (ADR-014).
    external_accounting_system = models.CharField(
        _("external accounting system"), max_length=50, blank=True
    )
    external_account_code = models.CharField(_("external account code"), max_length=50, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("account")
        verbose_name_plural = _("accounts")
        ordering = ["organization__code", "code"]
        permissions = [
            ("manage_accounts", _("Can create and archive accounts")),
        ]
        constraints = [
            # Per organization, not global: two organizations may each run a
            # 1-01-01-001 and neither constrains the other's naming.
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="account_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=ANY_ACCOUNT_CODE_PATTERN),
                name="account_code_format",
            ),
            # is_postable can never disagree with the code's level.
            models.CheckConstraint(
                condition=(
                    (Q(is_postable=True) & Q(code__regex=DETAIL_CODE_PATTERN))
                    | (Q(is_postable=False) & ~Q(code__regex=DETAIL_CODE_PATTERN))
                ),
                name="account_postable_iff_detail_code",
            ),
            models.CheckConstraint(
                condition=~Q(name_ar="") & ~Q(name_en=""),
                name="account_names_not_empty",
            ),
            # A rollup cannot require a dimension it never receives.
            models.CheckConstraint(
                condition=Q(requires_cost_center=False) | Q(is_postable=True),
                name="account_only_postable_requires_cost_center",
            ),
            models.CheckConstraint(
                condition=(
                    Q(external_accounting_system="", external_account_code="")
                    | (~Q(external_accounting_system="") & ~Q(external_account_code=""))
                ),
                name="account_external_mapping_is_complete_or_absent",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


class JournalEntryStatus(models.TextChoices):
    DRAFT = "DRAFT", _("مسودة")
    POSTED = "POSTED", _("مرحّل")
    REVERSED = "REVERSED", _("معكوس")


class SourceEvent(models.TextChoices):
    """
    The upstream economic event that caused a journal to exist.

    Deliberately a closed set, enforced by a check constraint as well as by
    these choices. The source identity is what stops a retried purchase
    invoice posting twice, and a free-text field would let a single typo —
    `POSTEED` — slip past a uniqueness guarantee whose entire job is to catch
    that retry.

    Not the same thing as `JournalEntry.status`. Status is the lifecycle of
    the journal itself; a source event is a fact about the document upstream
    and never changes once recorded. `PURCHASE_INVOICE / 145 / POSTED` names
    the original posting for invoice 145 forever, whatever later happens to
    the journal that carries it.

    Extend intentionally, with code and tests, when a module genuinely needs
    another distinct accounting event for the same source object. VOIDED,
    ADJUSTED, PAID, and SETTLED are deliberately absent until something
    actually requires them.
    """

    POSTED = "POSTED", _("مرحّل")
    REVERSED = "REVERSED", _("معكوس")


class JournalNumberSequence(models.Model):
    """
    Gapless per-organization, per-year journal numbering.

    A separate row taken with select_for_update, rather than MAX(number)+1,
    because two concurrent postings reading the same maximum would produce two
    entries claiming the same number.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="journal_sequences",
    )
    year = models.PositiveSmallIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = _("journal number sequence")
        verbose_name_plural = _("journal number sequences")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "year"], name="journal_sequence_unique_scope"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.code} {self.year}: {self.last_number}"


class JournalEntry(TimeStampedModel):
    """
    A balanced set of debits and credits.

    Once POSTED it is immutable — enforced by a database trigger, not by
    convention. Corrections are reversals: the original stays, a mirrored
    entry is appended, and the replacement is posted separately.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="journal_entries",
        verbose_name=_("organization"),
    )
    period = models.ForeignKey(
        AccountingPeriod,
        on_delete=models.PROTECT,
        related_name="journal_entries",
        verbose_name=_("accounting period"),
    )

    entry_number = models.CharField(_("entry number"), max_length=32)
    accounting_date = models.DateField(_("accounting date"), db_index=True)
    document_date = models.DateField(_("document date"))

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=JournalEntryStatus.choices,
        default=JournalEntryStatus.DRAFT,
    )
    is_adjustment = models.BooleanField(
        _("year-end adjustment"),
        default=False,
        help_text=_("Posted on the fiscal year-end date instead of a thirteenth period."),
    )

    # --- Source identity -------------------------------------------------
    # Together with `organization` these three name the upstream economic
    # event this journal records: KM / PURCHASE_INVOICE / 145 / POSTED. All
    # three or none of them — a half-populated identity is not an identity,
    # and the uniqueness guarantee below would not cover it.
    #
    # `source_document_type` and `source_document_id` are the source type and
    # source id. They keep their original names because `AuditEvent` already
    # uses them and one vocabulary across the two is worth more than a
    # shorter one here.
    source_document_type = models.CharField(_("source document type"), max_length=100, blank=True)
    source_document_id = models.CharField(_("source document id"), max_length=64, blank=True)
    source_event = models.CharField(
        _("source event"),
        max_length=16,
        blank=True,
        choices=SourceEvent.choices,
        help_text=_("Blank for a manual journal, which has no upstream document."),
    )

    # --- Idempotency ------------------------------------------------------
    #: A retried posting command must not create a second entry.
    #:
    #: Unique **per organization**, not globally. A global unique key is a
    #: tenancy leak in two directions: one organization's choice of key would
    #: block another's, and a replay lookup that matched on the key alone would
    #: hand back a journal belonging to somebody else entirely.
    idempotency_key = models.CharField(_("idempotency key"), max_length=128)

    #: A digest of the command that produced this entry — its dates, its source
    #: identity, and its lines. A replay is only a replay if it asks for the
    #: same thing; matching on the key alone would let a caller reuse a key
    #: with different lines and silently receive the earlier journal instead of
    #: the one they asked for.
    idempotency_fingerprint = models.CharField(
        _("idempotency fingerprint"), max_length=64, blank=True
    )

    posting_rule_version = models.CharField(_("posting rule version"), max_length=32, blank=True)
    narration = models.TextField(_("narration"), blank=True)

    reverses = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_by",
        verbose_name=_("reverses entry"),
    )

    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_journal_entries",
        verbose_name=_("posted by"),
    )

    class Meta:
        verbose_name = _("journal entry")
        verbose_name_plural = _("journal entries")
        ordering = ["-accounting_date", "-id"]
        permissions = [
            ("view_journal", _("Can read journal entries and lines")),
            ("create_draft", _("Can create a draft journal entry")),
            ("edit_draft", _("Can amend a draft journal entry")),
            ("post_journal", _("Can post a journal entry to the ledger")),
            ("reverse_journal", _("Can reverse a posted journal entry")),
            (
                "post_soft_closed_adjustment",
                _("Can post an adjustment into a soft-closed period"),
            ),
            (
                "reverse_in_soft_closed_period",
                _("Can reverse into a soft-closed period"),
            ),
        ]
        constraints = [
            # Partial: a draft holds no entry number, because an abandoned
            # draft must not burn one. Journal numbering is gapless, and a gap
            # is indistinguishable from a deleted entry to anyone auditing it.
            models.UniqueConstraint(
                fields=["organization", "entry_number"],
                condition=~Q(entry_number=""),
                name="journal_entry_number_unique_per_organization",
            ),
            # ...and the number is taken the moment it leaves draft.
            models.CheckConstraint(
                condition=Q(status=JournalEntryStatus.DRAFT) | ~Q(entry_number=""),
                name="journal_entry_numbered_once_posted",
            ),
            # A posted entry must record when and by whom.
            models.CheckConstraint(
                condition=~Q(status=JournalEntryStatus.POSTED) | Q(posted_at__isnull=False),
                name="journal_entry_posted_has_timestamp",
            ),
            # Per organization. Two organizations may each use "invoice-145"
            # as a key without one blocking or revealing the other's journal.
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                name="journal_entry_idempotency_key_unique_per_organization",
            ),
            # The closed enum, at the database. A value the application does
            # not know is not merely unexpected — it is a source identity that
            # the uniqueness index below silently fails to constrain.
            models.CheckConstraint(
                condition=Q(source_event__in=["", *SourceEvent.values]),
                name="journal_entry_source_event_is_known",
            ),
            # All three or none. A partially populated identity would fall
            # outside the unique index and defeat the whole guarantee.
            models.CheckConstraint(
                condition=(
                    Q(source_document_type="", source_document_id="", source_event="")
                    | (
                        ~Q(source_document_type="")
                        & ~Q(source_document_id="")
                        & ~Q(source_event="")
                    )
                ),
                name="journal_entry_source_identity_complete_or_absent",
            ),
            # The durable protection against double posting: one economic
            # event, one journal, per organization. Partial so that manual
            # journals — which carry no source identity at all — do not
            # collide with each other on three empty strings.
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "source_document_type",
                    "source_document_id",
                    "source_event",
                ],
                condition=~Q(source_event=""),
                name="journal_entry_source_event_unique_per_organization",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="journal_org_status_idx"),
            models.Index(
                fields=["source_document_type", "source_document_id"], name="journal_source_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.entry_number} ({self.get_status_display()})"


class AccountRoleDomain(models.TextChoices):
    """
    Which module's posting rules refer to a role.

    Closed and extended intentionally: Sales and Payroll add their own values
    when their posting rules arrive, never before. `PURCHASING` arrived with
    Task 2.10, which is the first task whose posting rules are about what the
    organization **owes** rather than what it holds — a supplier payable is
    not an inventory concept and filing it under `INVENTORY` would make the
    domain column a label rather than a fact.
    """

    INVENTORY = "INVENTORY", _("المخزون")
    PURCHASING = "PURCHASING", _("المشتريات")


class AccountRoleMappingScope(models.TextChoices):
    """
    How specifically a role may be mapped.

    `ORGANIZATION` — the organization default is the only mapping; the concept
    has no meaningful per-item answer (opening equity, in-transit).

    `ITEM` — the owning domain may override the organization default per item
    or per item category. The override rows live in that domain's own app;
    this model never references them.
    """

    ORGANIZATION = "ORGANIZATION", _("افتراضي المؤسسة فقط")
    ITEM = "ITEM", _("قابل للتخصيص حسب الصنف")


class AccountRole(TimeStampedModel):
    """
    A named economic purpose an account can serve — the vocabulary posting
    rules speak (ADR-019).

    **Global and system-owned, not organization data.** `INVENTORY_CONTROL`
    means the same thing in every organization; which *account* carries it is
    the organization's decision, recorded in `OrganizationAccountMapping`.
    Posting services refer to these codes and never to an account primary key
    or account code — that is the entire point of the indirection.

    System codes are locale-independent technical identifiers: never renamed,
    never deleted, reserved forever. A database trigger backs this up.
    """

    code = models.CharField(_("code"), max_length=64, unique=True)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200)
    domain = models.CharField(_("domain"), max_length=20, choices=AccountRoleDomain.choices)
    mapping_scope = models.CharField(
        _("mapping scope"),
        max_length=16,
        choices=AccountRoleMappingScope.choices,
        default=AccountRoleMappingScope.ORGANIZATION,
    )
    #: Seeded by migration, never by a user. A user-creatable "system" role
    #: would let an ordinary label claim the protections of the vocabulary.
    is_system = models.BooleanField(_("system role"), default=False)
    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("account role")
        verbose_name_plural = _("account roles")
        ordering = ["domain", "code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="account_role_code_format"
            ),
            models.CheckConstraint(
                condition=~Q(name_ar="") & ~Q(name_en=""),
                name="account_role_names_not_empty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


#: The approved inventory roles. Constants rather than an enum so a posting
#: service can name one without importing the model, and so the seed migration
#: and the services provably spell them identically.
INVENTORY_CONTROL = "INVENTORY_CONTROL"
INVENTORY_OPENING_EQUITY = "INVENTORY_OPENING_EQUITY"
INVENTORY_COUNT_VARIANCE = "INVENTORY_COUNT_VARIANCE"
INVENTORY_WASTE_EXPENSE = "INVENTORY_WASTE_EXPENSE"
INVENTORY_IN_TRANSIT = "INVENTORY_IN_TRANSIT"
INVENTORY_SHORTAGE_LOSS = "INVENTORY_SHORTAGE_LOSS"
INVENTORY_ADJUSTMENT = "INVENTORY_ADJUSTMENT"
#: Added by Task 1.4, one per operational document that needs a second side.
GOODS_RECEIVED_NOT_INVOICED = "GOODS_RECEIVED_NOT_INVOICED"
INVENTORY_CONSUMPTION = "INVENTORY_CONSUMPTION"
#: Added by Task 1.5 for the cross-branch half of a transfer receipt.
INTER_BRANCH_CLEARING = "INTER_BRANCH_CLEARING"

#: `(code, name_ar, name_en, mapping_scope)` for the seed migration and the
#: fresh-database test.
#:
#: Two roles are item-overridable. `INVENTORY_CONTROL`, because it is the one
#: role whose account carries standing stock value. `INVENTORY_CONSUMPTION`,
#: because what a thing is consumed *as* is a property of the thing —
#: packaging, cleaning materials, and direct ingredients belong in different
#: expense accounts, and one organization-wide answer would make the
#: consumption figures useless for costing.
#:
#: `GOODS_RECEIVED_NOT_INVOICED` is deliberately organization-only: it is a
#: liability-side clearing account waiting for an invoice, and which item
#: arrived says nothing about who is owed for it.
SYSTEM_INVENTORY_ROLES: tuple[tuple[str, str, str, str], ...] = (
    (INVENTORY_CONTROL, "مخزون - حساب المراقبة", "Inventory control", "ITEM"),
    (
        INVENTORY_OPENING_EQUITY,
        "أرصدة افتتاحية - مخزون",
        "Inventory opening equity",
        "ORGANIZATION",
    ),
    (INVENTORY_COUNT_VARIANCE, "فروقات الجرد", "Inventory count variance", "ORGANIZATION"),
    (INVENTORY_WASTE_EXPENSE, "هالك المخزون", "Inventory waste expense", "ORGANIZATION"),
    (INVENTORY_IN_TRANSIT, "بضاعة بالطريق", "Inventory in transit", "ORGANIZATION"),
    (INVENTORY_SHORTAGE_LOSS, "عجز التحويلات", "Inventory shortage loss", "ORGANIZATION"),
    (INVENTORY_ADJUSTMENT, "تسويات المخزون", "Inventory adjustment", "ORGANIZATION"),
    (
        GOODS_RECEIVED_NOT_INVOICED,
        "بضاعة مستلمة غير مفوترة",
        "Goods received not invoiced",
        "ORGANIZATION",
    ),
    (INVENTORY_CONSUMPTION, "استهلاك المخزون", "Inventory consumption", "ITEM"),
    # Organization-only, and necessarily so: it is the account that makes each
    # branch's standalone trial balance sum to zero when goods cross between
    # them, and per-item answers would leave one branch's clearing entry facing
    # a different account from the other's, netting to nothing at all.
    (
        INTER_BRANCH_CLEARING,
        "حساب وسيط بين الفروع",
        "Inter-branch clearing",
        "ORGANIZATION",
    ),
)

#: Added by Task 2.10 — the credit side of every supplier invoice, and the
#: account a payment will later clear (Task 2.0 §15).
SUPPLIER_PAYABLE = "SUPPLIER_PAYABLE"

#: Added by Task 2.12 — where an invoice-versus-receipt price difference is
#: parked. A **clearing** account, not an expense one, and that is a decision
#: rather than a detail.
#:
#: Task 2.0 §15 proposed `5-02-01-001`, a cost-of-sales code. That is superseded
#: (ADR-022, amended at Task 2.12) for two independent reasons. Mechanically,
#: class 5 sets `requires_cost_center` and a supplier invoice has no cost centre
#: to give: the document belongs to a branch, not to a department, and
#: `SupplierInvoiceLine.cost_center` is constrained to direct-account lines.
#: Substantively, ADR-022 already rejects "post the variance to cost of goods
#: sold directly" — it "conflates a purchasing outcome with a consumption
#: outcome", and food cost would then move for reasons that have nothing to do
#: with the kitchen.
#:
#: So the difference is **parked, not classified**. It sits in a clearing
#: account until a later, explicitly specified period-end process splits it
#: between inventory still on hand and cost of sales for what has been
#: consumed — a split that must take its branch and cost centre from inventory
#: ownership and consumption, never from the supplier invoice. Task 2.12 does
#: not build that process and does not guess at it.
PURCHASE_PRICE_VARIANCE = "PURCHASE_PRICE_VARIANCE"

#: Added by Task 2.13 — where the book value of goods sent back to a supplier
#: waits for the credit note that settles it.
#:
#: A **clearing** account, and this time the word is exact: the balance is a
#: claim in flight. Stock has left the warehouse and the supplier has not yet
#: agreed what it is worth, so there is a real obligation to somebody and no
#: document stating its amount. Task 2.14's credit note is what clears it, and
#: the difference between the book value that left and the credit that arrives
#: is `PURCHASE_RETURN_VARIANCE` — recognised there, not here.
#:
#: The physical return deliberately posts no variance. An expected credit is
#: commercial metadata until the supplier says otherwise, and booking a gain
#: or a loss from an expectation would put a number on the profit and loss
#: that nobody has agreed to.
SUPPLIER_RETURN_CLEARING = "SUPPLIER_RETURN_CLEARING"

#: Added by Task 2.13 as vocabulary, and posted to by Task 2.14.
#:
#:     PURCHASE_RETURN_VARIANCE = credit the supplier allows
#:                              - inventory book value that left
#:
#: Deliberately **not** `PURCHASE_PRICE_VARIANCE`, which is a different fact:
#: invoice value less receipt value on goods coming *in*. Merging them would
#: make it impossible to tell a supplier's pricing differences from the
#: consequence of having averaged two deliveries together and then unwound one
#: of them — and ADR-022's worked example exists precisely because the second
#: looks like a bug to anyone seeing it for the first time.
#:
#: Seeded here without a mapping and without a posting rule, which is a
#: departure from this module's usual practice and is the narrower of two
#: evils: Task 2.13's returns are meaningless without the credit note that
#: follows, and naming the destination now is what stops Task 2.14 quietly
#: reusing the price-variance account.
PURCHASE_RETURN_VARIANCE = "PURCHASE_RETURN_VARIANCE"

#: Added by Task 2.15 — money leaving for a supplier. The cash and bank
#: sources are resolved by the payment's `method` through these roles, never
#: a hard-coded account id (PRC-056); when Phase 5 makes cashboxes and bank
#: accounts first-class, the payment names one and the role becomes its
#: default — the model widens, nothing already posted moves.
SUPPLIER_PAYMENT_CASH = "SUPPLIER_PAYMENT_CASH"
SUPPLIER_PAYMENT_BANK = "SUPPLIER_PAYMENT_BANK"

#: Added by Task 2.15 — a payment's unallocated remainder. An **asset**: cash
#: handed over before an invoice exists to net it against, which is a
#: different economic event from a credit note's standing credit (that one is
#: the supplier owing money back, and lives in the payable as a debit).
#: Netting a prepayment against a payable that does not exist yet would make
#: the aging report lie about both (PRC-055).
SUPPLIER_ADVANCE = "SUPPLIER_ADVANCE"

#: The purchasing vocabulary, same shape as `SYSTEM_INVENTORY_ROLES`.
#:
#: Organization-only, and necessarily so. Which item was bought says nothing
#: about who is owed for it: one supplier's invoice covering rice, chicken and
#: a delivery charge is one debt to one company, and a per-item payable would
#: split a single obligation across three accounts that no statement could
#: reassemble.
#:
#: `SUPPLIER_ADVANCE` and the two payment-source roles are specified in Task
#: 2.0 §15 and are deliberately **not** seeded here. A role with no posting
#: rule behind it is a grant nobody can audit — the same mistake
#: `import_opening_draft` records in inventory. Each arrives with the task
#: that posts to it: the advance and the payment sources with 2.15.
SYSTEM_PURCHASING_ROLES: tuple[tuple[str, str, str, str], ...] = (
    (SUPPLIER_PAYABLE, "ذمم الموردين", "Supplier payable", "ORGANIZATION"),
    (
        PURCHASE_PRICE_VARIANCE,
        "تسوية فروقات أسعار المشتريات",
        "Purchase price variance clearing",
        "ORGANIZATION",
    ),
    (
        SUPPLIER_RETURN_CLEARING,
        "تسوية مرتجعات الموردين",
        "Supplier return clearing",
        "ORGANIZATION",
    ),
    (
        PURCHASE_RETURN_VARIANCE,
        "فروقات إرجاع المشتريات",
        "Purchase return variance",
        "ORGANIZATION",
    ),
    (SUPPLIER_PAYMENT_CASH, "دفعات الموردين نقداً", "Supplier payments — cash", "ORGANIZATION"),
    (
        SUPPLIER_PAYMENT_BANK,
        "دفعات الموردين عبر المصرف",
        "Supplier payments — bank",
        "ORGANIZATION",
    ),
    (SUPPLIER_ADVANCE, "سلف الموردين", "Supplier advances", "ORGANIZATION"),
)


class OrganizationAccountMapping(TimeStampedModel):
    """
    The organization's effective-dated default: this role posts to this
    account, from this date to that one.

    Item- and category-specific overrides deliberately do **not** live here.
    They belong to the domain that owns the item concept (`apps.inventory`),
    which imports accounting — never the reverse (ADR-019).

    A mapping that has been used by a posting is immutable except for closing
    its effective range; the correction is a new version. The rows a posting
    snapshotted stay readable forever, which is what makes "which account did
    this movement post to" answerable years later.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="account_mappings",
        verbose_name=_("organization"),
    )
    account_role = models.ForeignKey(
        AccountRole,
        on_delete=models.PROTECT,
        related_name="organization_mappings",
        verbose_name=_("account role"),
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="role_mappings",
        verbose_name=_("account"),
    )
    effective_from = models.DateField(_("effective from"))
    effective_to = models.DateField(_("effective to"), null=True, blank=True)
    #: Incremented per `(organization, role)` when a mapping is superseded, so
    #: the history reads as a sequence of decisions rather than a pile of rows.
    version = models.PositiveIntegerField(_("version"), default=1)
    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("organization account mapping")
        verbose_name_plural = _("organization account mappings")
        ordering = ["organization__code", "account_role__code", "-effective_from"]
        permissions = [
            ("manage_account_mappings", _("Can manage account role mappings")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="org_account_mapping_period_is_ordered",
            ),
            models.UniqueConstraint(
                fields=["organization", "account_role", "version"],
                name="org_account_mapping_version_unique",
            ),
            # The overlap rule itself needs a range type and is added as an
            # EXCLUDE constraint by the migration, as the conversion overlap was.
        ]
        indexes = [
            models.Index(
                fields=["organization", "account_role", "is_active"],
                name="org_mapping_role_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.organization.code}: {self.account_role.code} -> {self.account.code} "
            f"v{self.version}"
        )

    def covers(self, on_date: datetime.date) -> bool:
        """Whether this mapping is in effect on the given date."""
        if on_date < self.effective_from:
            return False
        return self.effective_to is None or on_date <= self.effective_to


class JournalLine(models.Model):
    """
    One debit or credit.

    Exactly one of debit and credit is non-zero: a line that is both is
    ambiguous, and a line that is neither is noise in a ledger that has to
    reconcile.
    """

    entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("journal entry"),
    )
    #: Also the deterministic tie-break key for proportional allocation.
    line_number = models.PositiveIntegerField(_("line number"))

    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="journal_lines",
        verbose_name=_("account"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="journal_lines",
        verbose_name=_("branch"),
    )
    cost_center = models.ForeignKey(
        CostCenter,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="journal_lines",
        verbose_name=_("cost center"),
    )

    debit = models.DecimalField(
        _("debit"), max_digits=AMOUNT_MAX_DIGITS, decimal_places=MONEY_PLACES, default=Decimal("0")
    )
    credit = models.DecimalField(
        _("credit"), max_digits=AMOUNT_MAX_DIGITS, decimal_places=MONEY_PLACES, default=Decimal("0")
    )

    narration = models.CharField(_("narration"), max_length=255, blank=True)

    class Meta:
        verbose_name = _("journal line")
        verbose_name_plural = _("journal lines")
        ordering = ["entry_id", "line_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["entry", "line_number"], name="journal_line_number_unique_per_entry"
            ),
            models.CheckConstraint(
                condition=Q(debit__gte=Decimal("0")), name="journal_line_debit_not_negative"
            ),
            models.CheckConstraint(
                condition=Q(credit__gte=Decimal("0")), name="journal_line_credit_not_negative"
            ),
            # Exactly one side carries the amount.
            models.CheckConstraint(
                condition=(
                    (Q(debit__gt=Decimal("0")) & Q(credit=Decimal("0")))
                    | (Q(credit__gt=Decimal("0")) & Q(debit=Decimal("0")))
                ),
                name="journal_line_exactly_one_side",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "branch"], name="journal_line_account_idx"),
        ]

    def __str__(self) -> str:
        side = f"Dr {self.debit}" if self.debit else f"Cr {self.credit}"
        return f"{self.account.code} {side}"

    @property
    def amount(self) -> Decimal:
        """The signed movement: positive for a debit, negative for a credit."""
        return self.debit - self.credit
