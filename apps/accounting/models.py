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

    source_document_type = models.CharField(_("source document type"), max_length=100, blank=True)
    source_document_id = models.CharField(_("source document id"), max_length=64, blank=True)

    #: A retried posting command must not create a second entry.
    idempotency_key = models.CharField(_("idempotency key"), max_length=128, unique=True)

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
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "entry_number"],
                name="journal_entry_number_unique_per_organization",
            ),
            # A posted entry must record when and by whom.
            models.CheckConstraint(
                condition=~Q(status=JournalEntryStatus.POSTED) | Q(posted_at__isnull=False),
                name="journal_entry_posted_has_timestamp",
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
