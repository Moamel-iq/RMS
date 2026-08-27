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
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
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


class ManualPostingPolicy(models.TextChoices):
    """
    Whether an account accepts a hand-written journal line (ADR-029 §2).

    Not a security setting and not a synonym for `is_active`. It answers one
    narrow question: may somebody reach this account without going through the
    document that owns it?

    `RESTRICTED` is the interesting value, and it exists because the failure it
    prevents is invisible. A manual credit to supplier payable is not an
    accounting error — it balances, it posts, the trial balance still ties. It
    silently breaks the equality that `ذمم الموردين` exists to prove, and the
    workspace then reports a discrepancy whose cause cannot be seen from the
    subledger side at all. `RESTRICTED` keeps that entry possible for somebody
    holding `post_restricted_manual_journal`, and makes it nameable afterwards.
    """

    ALLOWED = "ALLOWED", _("متاح")
    RESTRICTED = "RESTRICTED", _("مقيّد")
    FORBIDDEN = "FORBIDDEN", _("ممنوع")


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
    #: A date is safer than a switch: it leaves the control's effective window
    #: visible and prevents a later toggle from changing what earlier days were
    #: expected to satisfy.  The migration supplies its deployment date, so
    #: existing posted history is never rewritten.
    daily_close_enforced_from = models.DateField(
        _("daily close enforced from"),
        default=timezone.localdate,
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("accounting settings")
        verbose_name_plural = _("accounting settings")
        # The two reconciliation workspaces have no model of their own, and
        # that is the decision rather than an oversight (ADR-029 §4): supplier
        # liability lives in Procurement's documents and application receivable
        # in Sales's ledger, and Accounting builds no second copy of either.
        #
        # A permission still needs a table to hang on, so they hang here — the
        # per-organization accounting configuration — because both are
        # organization-scoped authorities over the accounting module as a
        # whole rather than over any one record.
        permissions = [
            (
                "view_supplier_liabilities",
                _("Can read the supplier liability reconciliation workspace"),
            ),
            (
                "view_application_receivables",
                _("Can read the delivery-application receivable workspace"),
            ),
        ]
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

    #: Whether a manual journal may name this account (ADR-029 §2). Defaults to
    #: `ALLOWED`, which is what every account meant before this column existed,
    #: so no existing row changes meaning.
    #:
    #: What it prevents: a hand-written credit to the supplier payable control
    #: account. That entry balances and posts, so nothing in the ledger objects
    #: to it, and the supplier reconciliation then reports a difference it
    #: cannot explain — the subledger side has no document to show for it.
    manual_posting_policy = models.CharField(
        _("manual posting policy"),
        max_length=16,
        choices=ManualPostingPolicy.choices,
        default=ManualPostingPolicy.ALLOWED,
        help_text=_("Whether a hand-written journal line may name this account."),
    )

    #: Seeded by `seed_chart_of_accounts`, never by a user.
    #:
    #: What it prevents: somebody renaming `2-01-01-001` from "ذمم الموردين" to
    #: "إيجارات" because the code happened to be free in their mental model.
    #: The account is the one Procurement's posting rules resolve to, and
    #: repurposing it does not move the postings that already landed there — it
    #: relabels them, which is the one correction nobody can spot in a report.
    is_system = models.BooleanField(
        _("system account"),
        default=False,
        help_text=_("Seeded reference data. A user may not repurpose it."),
    )

    #: When the account was withdrawn from use. `is_active` already carried the
    #: fact; this carries the date.
    #:
    #: What it prevents: "this account has been archived since some point in
    #: the past" as the only answer available to somebody reconciling a report
    #: that stopped including it. Null for every active account, which is every
    #: existing row's current meaning.
    archived_at = models.DateTimeField(_("archived at"), null=True, blank=True)

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
            # Phase 5 (ADR-029 §7). `manage_accounts` was Task 0.7's authority
            # over the model; these two are the authority over the *screen* and
            # its acts, and they are separate entries so a deployment can widen
            # one without widening the other.
            ("view_chart_of_accounts", _("Can read the chart of accounts")),
            ("manage_chart_of_accounts", _("Can create, amend and archive chart accounts")),
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
            # A rollup never receives a journal line, so a posting policy on
            # one is a claim about nothing — and a claim about nothing is
            # worse than silence: a reader who sees `FORBIDDEN` on `2-01`
            # concludes the whole payable branch is protected, when in fact
            # every leaf under it is still `ALLOWED`.
            models.CheckConstraint(
                condition=(
                    Q(is_postable=True) | Q(manual_posting_policy=ManualPostingPolicy.ALLOWED)
                ),
                name="account_only_postable_restricts_manual_posting",
            ),
            # If and only if, in both directions. An archived account with no
            # archive date loses when it happened; an active account carrying
            # one says it is archived while the flag every query filters on
            # says it is not, and the flag is the one that wins silently.
            models.CheckConstraint(
                condition=(
                    (Q(is_active=True) & Q(archived_at__isnull=True))
                    | (Q(is_active=False) & Q(archived_at__isnull=False))
                ),
                name="account_archived_at_iff_inactive",
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

    #: Who wrote this entry, as distinct from who released it to the ledger.
    #:
    #: The kernel recorded `posted_by` from Task 0.6 and no creator at all, so
    #: "the same person entered and posted this" was not a question the database
    #: could answer — and a maker-checker rule that cannot be asked is not a
    #: control. Phase 5 asks it (ADR-029 §2).
    #:
    #: Nullable because every row that existed before this field did has no
    #: creator to record, and a migration cannot invent one. A fabricated
    #: creator would be worse than an absent one: it would read as evidence.
    #: `post_draft` refuses a manual entry with a null creator rather than
    #: treating the gap as consent, because nothing can prove the two differ.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_journal_entries",
        verbose_name=_("created by"),
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
                "post_restricted_manual_journal",
                _("Can post a manual line to a RESTRICTED control account"),
            ),
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

    @property
    def is_manual(self) -> bool:
        """
        Whether a person wrote this entry rather than a document producing it.

        Derived from the source identity, not stored: `source_event` is blank
        exactly when there is no upstream document, and the check constraint
        `journal_entry_source_identity_complete_or_absent` guarantees the three
        source columns move together. A separate `is_manual` flag would be a
        second answer to a question the identity already settles, and the two
        could disagree.
        """
        return self.source_event == ""

    @property
    def is_editable(self) -> bool:
        """A draft a person wrote. Nothing else is editable through Accounting."""
        return self.status == JournalEntryStatus.DRAFT and self.is_manual


class AccountRoleDomain(models.TextChoices):
    """
    Which module's posting rules refer to a role.

    Closed and extended intentionally: Sales and Payroll add their own values
    when their posting rules arrive, never before. `PURCHASING` arrived with
    Task 2.10, which is the first task whose posting rules are about what the
    organization **owes** rather than what it holds — a supplier payable is
    not an inventory concept and filing it under `INVENTORY` would make the
    domain column a label rather than a fact.

    `ACCOUNTING` arrived with Task 5.0 and is the first domain whose posting
    rules are about the organization's **own financial administration** rather
    than a trading module's. Nothing buys, sells, produces or moves when an
    expense is accrued at month end, a prepayment is amortised, or a year's
    result is swept to retained earnings; the entries exist because the
    accounting period ended, not because a document arrived. Filing an expense
    accrual under `PURCHASING` because both involve a liability would make the
    domain column a label rather than a fact — the same reasoning ADR-019
    records for `PURCHASING` and ADR-027 for `SALES`.
    """

    INVENTORY = "INVENTORY", _("المخزون")
    PURCHASING = "PURCHASING", _("المشتريات")
    #: Task 4.0. The first domain whose posting rules are about what the
    #: organization **earns**, and the first with a receivable of its own. A
    #: delivery-application receivable is not a purchasing concept and filing
    #: it under `PURCHASING` because both are "somebody owes somebody" would
    #: make the domain column a label rather than a fact.
    SALES = "SALES", _("المبيعات")
    #: Task 5.0. Deferrals and the year-end result — the accounts nothing
    #: outside Accounting ever posts to, because nothing outside Accounting
    #: knows that a period has ended.
    ACCOUNTING = "ACCOUNTING", _("المحاسبة")
    PAYROLL = "PAYROLL", _("الرواتب")


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


# ---------------------------------------------------------------------------
# Sales — Task 4.0
# ---------------------------------------------------------------------------

#: What the restaurant earned, before any deduction. Gross list value, always:
#: a discount is shown as a deduction beside it rather than netted into it,
#: because a revenue figure that already has discounts inside it cannot answer
#: "what did we give away this month" (ADR-027 §2).
SALES_REVENUE = "SALES_REVENUE"

#: **Contra-revenue**, and the word is exact. A restaurant-funded discount is
#: money the restaurant chose not to collect, so it reduces what the restaurant
#: earns and belongs beside revenue rather than in class 6. Booking it as an
#: operating expense would leave gross revenue overstated and marketing spend
#: overstated by the same amount, and both figures would look defensible.
#:
#: An **application**-funded discount never reaches this account. The
#: application reimburses it, so it is part of what the application owes —
#: `DELIVERY_APP_RECEIVABLE`, not a restaurant cost (ADR-028 §3).
SALES_DISCOUNT = "SALES_DISCOUNT"

#: Reversed sales value, kept separate from `SALES_DISCOUNT` because the two
#: answer different questions. A discount is a pricing decision made before the
#: sale; a return is a sale that stopped being one afterwards. Netting them
#: would make a month of generous promotions indistinguishable from a month of
#: rejected food.
SALES_RETURNS = "SALES_RETURNS"

#: Where cash takings sit until a cashier closing counts them. The organization
#: decides which cashbox; Phase 5 makes cashboxes first-class and this role
#: becomes their default. Nothing already posted moves when it does.
SALES_CASH_ON_HAND = "SALES_CASH_ON_HAND"

#: Card takings between the sale and the acquirer's remittance. A clearing
#: asset, not cash: the money is real and the restaurant does not have it yet.
SALES_CARD_CLEARING = "SALES_CARD_CLEARING"

#: What a delivery application owes for sales it has taken. **Derived from an
#: append-only ledger, never a stored balance** (ADR-027 §5): a mutable balance
#: field is a number that can disagree with the entries that produced it, and
#: the disagreement is discovered during a settlement argument.
#:
#: Organization-scoped here, with a per-application override carried on
#: `sales.DeliveryApplication` — the same arrangement `INVENTORY_CONTROL` uses
#: for items. Accounting never learns what a delivery application is.
DELIVERY_APP_RECEIVABLE = "DELIVERY_APP_RECEIVABLE"

#: The application's cut, accrued at sale (ADR-028 §4). Accrued rather than
#: discovered at settlement, because the agreement states the rate on the day
#: the order is taken: waiting for a statement would mean a month's margin was
#: unknown until the following month, and every unexplained settlement
#: difference would look like a commission surprise.
DELIVERY_COMMISSION_EXPENSE = "DELIVERY_COMMISSION_EXPENSE"

#: Other contractually agreed application deductions accrued at sale —
#: per-order service fees and the like. Separate from commission because a
#: fixed fee per order and a percentage of value behave differently as volume
#: moves, and one account would hide which of the two changed.
DELIVERY_OTHER_FEE_EXPENSE = "DELIVERY_OTHER_FEE_EXPENSE"

#: The difference between what a settlement was expected to remit and what it
#: actually did. **Bidirectional** — a debit when the application short-paid
#: and a credit when it over-paid — which is why it sits in class 7 beside the
#: other difference accounts rather than in class 6.
#:
#: Reaching it is never automatic. An unexplained variance blocks posting until
#: somebody categorises it and states a reason (ADR-028 §7); a system that
#: silently absorbs differences into an account is a system where a
#: mis-configured commission rate is invisible for a year.
DELIVERY_SETTLEMENT_VARIANCE = "DELIVERY_SETTLEMENT_VARIANCE"

#: Where a bank remittance lands when a settlement pays out. Cash settlements
#: use `SALES_CASH_ON_HAND`; the settlement names its destination and the role
#: resolves the account, never a hard-coded id.
SALES_SETTLEMENT_BANK = "SALES_SETTLEMENT_BANK"

#: Counted cash against expected cash. Bidirectional for the same reason
#: `INVENTORY_COUNT_VARIANCE` is: a till that is over is not negative spending.
#:
#: This is the **only** thing a cashier closing may post. The sale already
#: recognised the revenue; a closing that posted sales again would double every
#: cash takings figure in the system (ADR-027 §8).
SALES_CASH_OVER_SHORT = "SALES_CASH_OVER_SHORT"

#: The sales vocabulary, same shape as the two above.
#:
#: Every one is `ORGANIZATION`-scoped. `ITEM` would be the wrong question in
#: every case here: sales roles are about a *channel*, an *application* or a
#: *tender*, and none of those is an inventory item. Where a finer answer is
#: genuinely needed — this application settles into that receivable account,
#: this channel earns into that revenue account — the override is carried by
#: the sales master data that owns the concept, exactly as inventory carries
#: its item overrides. Accounting stays ignorant of both.
SYSTEM_SALES_ROLES: tuple[tuple[str, str, str, str], ...] = (
    (SALES_REVENUE, "إيرادات المبيعات", "Sales revenue", "ORGANIZATION"),
    (SALES_DISCOUNT, "خصومات المبيعات", "Sales discounts", "ORGANIZATION"),
    (SALES_RETURNS, "مردودات المبيعات", "Sales returns", "ORGANIZATION"),
    (SALES_CASH_ON_HAND, "نقدية المبيعات", "Sales cash on hand", "ORGANIZATION"),
    (SALES_CARD_CLEARING, "تسوية مبيعات البطاقات", "Card clearing", "ORGANIZATION"),
    (
        DELIVERY_APP_RECEIVABLE,
        "ذمم تطبيقات التوصيل",
        "Delivery application receivable",
        "ORGANIZATION",
    ),
    (
        DELIVERY_COMMISSION_EXPENSE,
        "عمولات تطبيقات التوصيل",
        "Delivery commission expense",
        "ORGANIZATION",
    ),
    (
        DELIVERY_OTHER_FEE_EXPENSE,
        "رسوم تطبيقات التوصيل الأخرى",
        "Delivery other fee expense",
        "ORGANIZATION",
    ),
    (
        DELIVERY_SETTLEMENT_VARIANCE,
        "فروقات تسويات التطبيقات",
        "Delivery settlement variance",
        "ORGANIZATION",
    ),
    (
        SALES_SETTLEMENT_BANK,
        "تحصيلات التطبيقات عبر المصرف",
        "Settlement bank receipts",
        "ORGANIZATION",
    ),
    (SALES_CASH_OVER_SHORT, "فروقات الصندوق", "Cash over and short", "ORGANIZATION"),
)


# ---------------------------------------------------------------------------
# Accounting — Task 5.0
# ---------------------------------------------------------------------------

#: The credit side of an accrual: a cost incurred that no invoice has stated
#: yet (ADR-030 §4). A **liability**, and deliberately not the supplier
#: payable: nobody has named an amount, so it cannot be aged, allocated or
#: paid, and putting it in the payable would make the supplier reconciliation
#: report a difference for every accrual outstanding at month end.
ACCRUED_EXPENSES_PAYABLE = "ACCRUED_EXPENSES_PAYABLE"

#: The debit side of a prepayment, released to expense by amortization
#: (ADR-030 §5). An **asset**: rent paid for a quarter that has not happened
#: yet is a right to occupy the premises, not a cost of this month.
PREPAID_EXPENSE = "PREPAID_EXPENSE"

#: The computed equity line, and where the year-end closing journal lands its
#: result on the way through (ADR-031 §3).
#:
#: Computed, never posted monthly. Sweeping revenue and expense to equity every
#: month destroys the year-to-date income statement: once March's revenue has
#: gone to equity, "revenue for the year so far" has to be reconstructed from
#: closing journals rather than read from the accounts.
CURRENT_YEAR_EARNINGS = "CURRENT_YEAR_EARNINGS"

#: Where the year-end closing journal leaves the result (ADR-031 §4). The one
#: equity account that carries every prior year's outcome, and the reason
#: `CURRENT_YEAR_EARNINGS` is separate: a balance sheet that could not tell
#: this year's result from the accumulated ones would answer neither question.
RETAINED_EARNINGS = "RETAINED_EARNINGS"

#: The accounting vocabulary, same shape as the three above.
#:
#: Every one is `ORGANIZATION`-scoped, and necessarily so. `ITEM` is not merely
#: unnecessary here, it is meaningless: none of these four is about a thing the
#: organization holds or sells. An accrual is about a period that ended, a
#: prepayment about a period that has not started, and the two earnings
#: accounts about the whole organization's result — a per-item retained
#: earnings would be a sentence with no subject.
SYSTEM_ACCOUNTING_ROLES: tuple[tuple[str, str, str, str], ...] = (
    (
        ACCRUED_EXPENSES_PAYABLE,
        "مصروفات مستحقة الدفع",
        "Accrued expenses payable",
        "ORGANIZATION",
    ),
    (PREPAID_EXPENSE, "مصروفات مدفوعة مقدماً", "Prepaid expenses", "ORGANIZATION"),
    (CURRENT_YEAR_EARNINGS, "نتيجة السنة الحالية", "Current year earnings", "ORGANIZATION"),
    (RETAINED_EARNINGS, "الأرباح المحتجزة", "Retained earnings", "ORGANIZATION"),
)


# ---------------------------------------------------------------------------
# Payroll
# ---------------------------------------------------------------------------

PAYROLL_SALARY_EXPENSE = "PAYROLL_SALARY_EXPENSE"
PAYROLL_ALLOWANCE_EXPENSE = "PAYROLL_ALLOWANCE_EXPENSE"
PAYROLL_OVERTIME_EXPENSE = "PAYROLL_OVERTIME_EXPENSE"
PAYROLL_PAYABLE = "PAYROLL_PAYABLE"
EMPLOYEE_RECEIVABLE = "EMPLOYEE_RECEIVABLE"
PAYROLL_OTHER_LIABILITY = "PAYROLL_OTHER_LIABILITY"
PAYROLL_CASH = "PAYROLL_CASH"
PAYROLL_BANK = "PAYROLL_BANK"

SYSTEM_PAYROLL_ROLES: tuple[tuple[str, str, str, str], ...] = (
    (PAYROLL_SALARY_EXPENSE, "مصروف الرواتب والأجور", "Salary and wage expense", "ORGANIZATION"),
    (PAYROLL_ALLOWANCE_EXPENSE, "مصروف بدلات الرواتب", "Payroll allowance expense", "ORGANIZATION"),
    (PAYROLL_OVERTIME_EXPENSE, "مصروف العمل الإضافي", "Payroll overtime expense", "ORGANIZATION"),
    (PAYROLL_PAYABLE, "رواتب مستحقة الدفع", "Payroll payable", "ORGANIZATION"),
    (EMPLOYEE_RECEIVABLE, "ذمم الموظفين", "Employee receivable", "ORGANIZATION"),
    (PAYROLL_OTHER_LIABILITY, "التزامات رواتب أخرى", "Other payroll liability", "ORGANIZATION"),
    (PAYROLL_CASH, "نقدية صرف الرواتب", "Payroll cash", "ORGANIZATION"),
    (PAYROLL_BANK, "مصرف صرف الرواتب", "Payroll bank", "ORGANIZATION"),
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


# ---------------------------------------------------------------------------
# Financial-statement classification — Task 5.0, ADR-031
# ---------------------------------------------------------------------------


class StatementGroup(models.TextChoices):
    """
    Where an account's balance appears on a financial statement (ADR-031 §1).

    A closed set, and separate from `AccountClass` because the class cannot
    carry this. Class `7` is "إيرادات ومصروفات أخرى" — **both** sides of the
    income statement at once, and a class-7 account cannot be asked which one
    it belongs to. Class `1` has no current / non-current distinction, so the
    balance sheet cannot be split from it. Class `8` is clearing, of which
    GRNI is a real liability and inter-branch clearing is presentation noise.

    The rejected alternative was a code-prefix test inside the statement view.
    It hides statement behaviour where nobody looks for it, it breaks the
    moment a second organization numbers its chart differently — which ADR-014
    explicitly allows — and it cannot express the class-7 split at all without
    a second, longer prefix table that is a mapping in denial.
    """

    ASSET = "ASSET", _("الأصول")
    LIABILITY = "LIABILITY", _("الالتزامات")
    EQUITY = "EQUITY", _("حقوق الملكية")
    REVENUE = "REVENUE", _("الإيرادات")
    COST_OF_SALES = "COST_OF_SALES", _("كلفة المبيعات")
    OPERATING_EXPENSE = "OPERATING_EXPENSE", _("المصروفات التشغيلية")
    OTHER_INCOME = "OTHER_INCOME", _("إيرادات أخرى")
    OTHER_EXPENSE = "OTHER_EXPENSE", _("مصروفات أخرى")


#: The two groups a current / non-current split is a question about. Named
#: here rather than spelled out at each use, so the model constraint, the
#: service check and any later report all read the same list.
BALANCE_SHEET_GROUPS = (StatementGroup.ASSET, StatementGroup.LIABILITY)


class PresentationSection(models.TextChoices):
    """
    The balance-sheet split (ADR-031 §1).

    `NOT_APPLICABLE` is the default and is a real answer, not a missing one:
    an income-statement account has no current / non-current dimension, and
    forcing one would invent a fact about it.
    """

    CURRENT = "CURRENT", _("متداول")
    NON_CURRENT = "NON_CURRENT", _("غير متداول")
    NOT_APPLICABLE = "NOT_APPLICABLE", _("لا ينطبق")


class AccountReportMapping(TimeStampedModel):
    """
    This organization's account, in this statement group (ADR-031 §1).

    Organization-owned rather than global, for the reason ADR-014 gives for the
    chart itself: a second organization may number and structure its accounts
    differently, and a global classification would either constrain that or
    quietly misfile it.

    **Assigned to postable accounts only.** A rollup carries no balance of its
    own — its figure is the sum of its children — so classifying one would
    either double-count the branch or contradict the leaves under it. That rule
    is a fact about another row and therefore cannot be a check constraint;
    `services.set_report_mapping` enforces it, and every write goes through
    that function.

    Deactivated rather than deleted, so a statement produced last year stays
    explicable after the classification is revised.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="report_mappings",
        verbose_name=_("organization"),
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="report_mappings",
        verbose_name=_("account"),
    )
    statement_group = models.CharField(
        _("statement group"), max_length=24, choices=StatementGroup.choices
    )
    presentation_section = models.CharField(
        _("presentation section"),
        max_length=16,
        choices=PresentationSection.choices,
        default=PresentationSection.NOT_APPLICABLE,
    )
    display_order = models.PositiveIntegerField(_("display order"), default=0)
    is_active = models.BooleanField(_("active"), default=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("account report mapping")
        verbose_name_plural = _("account report mappings")
        ordering = ["organization__code", "statement_group", "display_order", "account__code"]
        permissions = [
            ("manage_report_mappings", _("Can map accounts to financial-statement groups")),
        ]
        constraints = [
            # One classification per account. Two would let the same balance
            # appear in two sections of the same statement, and the statement
            # would still add up — which is what makes it undetectable.
            models.UniqueConstraint(
                fields=["organization", "account"],
                name="report_mapping_unique_per_account",
            ),
            # A current / non-current split is a balance-sheet question. On a
            # revenue account it is not merely unused, it is false: revenue has
            # no maturity, and a reader who saw "متداول" on it would conclude
            # somebody had decided something they had not.
            models.CheckConstraint(
                condition=(
                    Q(presentation_section=PresentationSection.NOT_APPLICABLE)
                    | Q(statement_group__in=[group.value for group in BALANCE_SHEET_GROUPS])
                ),
                name="report_mapping_section_only_on_balance_sheet_groups",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "statement_group", "is_active"],
                name="report_mapping_group_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.account.code} -> {self.statement_group}"


# ---------------------------------------------------------------------------
# Cash and bank master data (ADR-030 §1)
# ---------------------------------------------------------------------------


class CashAccountBase(TimeStampedModel):
    """
    What a cashbox and a bank account have in common.

    Abstract, because the two are genuinely different records — a cashbox sits
    at one branch and a bank account may not — but every rule that matters is
    shared, and stating it twice is how the two would drift.

    **Neither carries a balance field of any kind.** Not `current_balance`, not
    `opening_balance`, not `last_reconciled_balance`. A stored balance has to be
    maintained, every maintenance path is a chance to disagree with the ledger,
    and the disagreement is silent: the page says one figure, the trial balance
    says another, and nothing is required to notice. Deriving it costs one
    aggregate query and cannot be wrong (ADR-030 §1).

    A *date* for the last reconciliation is fine and is here. An amount is not.
    """

    #: Stable across renames and re-codings, and safe to put in a URL or an
    #: export. The primary key is sequential and leaks how many exist.
    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=20)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200)
    notes = models.TextField(_("notes"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    archived_at = models.DateTimeField(_("archived at"), null=True, blank=True)
    last_reconciled_on = models.DateField(_("last reconciled on"), null=True, blank=True)

    class Meta:
        abstract = True

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


class Cashbox(CashAccountBase):
    """
    A physical drawer or safe, tied to exactly one postable cash account.

    Branch is required here and optional on a bank account, because a cashbox
    is a physical object in a specific place: somebody counts it, and "which
    branch is this drawer in" always has an answer.
    """

    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="cashboxes",
        verbose_name=_("branch"),
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="cashboxes",
        verbose_name=_("cash account"),
    )
    opened_on = models.DateField(_("in use from"))
    responsible_note = models.CharField(_("responsible person"), max_length=200, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("cashbox")
        verbose_name_plural = _("cashboxes")
        ordering = ["organization__code", "code"]
        permissions = [
            ("manage_cashboxes", _("Can create and archive cashboxes")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"], name="cashbox_code_unique_per_organization"
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="cashbox_code_format"
            ),
            models.CheckConstraint(
                condition=~Q(name_ar="") & ~Q(name_en=""), name="cashbox_names_not_empty"
            ),
            # One GL account backs at most one **active** cashbox.
            #
            # Two active cashboxes on one account produce two statements that
            # are the same movements, and an operator counting one drawer
            # against it finds it over by exactly the other drawer — with
            # nothing on either page to suggest why.
            #
            # Partial rather than total, so an archived cashbox can be replaced
            # without renumbering the account, and the archived row stays
            # readable forever.
            models.UniqueConstraint(
                fields=["organization", "account"],
                condition=Q(is_active=True),
                name="cashbox_account_unique_while_active",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(is_active=True) & Q(archived_at__isnull=True))
                    | (Q(is_active=False) & Q(archived_at__isnull=False))
                ),
                name="cashbox_archived_at_matches_state",
            ),
        ]


class BankAccount(CashAccountBase):
    """
    One bank account, tied to exactly one postable bank GL account.

    Branch is optional: an organization's operating account belongs to no
    single branch, and forcing one would record a claim nobody made.
    """

    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bank_accounts",
        verbose_name=_("branch"),
    )
    bank_name = models.CharField(_("bank"), max_length=200)
    #: **The mask, not the number.** Release 1 has no reason to hold a full
    #: account number — nothing generates a payment file from it — and a field
    #: that can hold one eventually will. What the screens need is enough to
    #: tell two accounts apart, which the last four digits give.
    masked_account_number = models.CharField(
        _("account number (masked)"),
        max_length=40,
        help_text=_("آخر أربعة أرقام فقط."),
    )
    iban = models.CharField(_("IBAN"), max_length=34, blank=True)
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="bank_accounts",
        verbose_name=_("bank account"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("bank account")
        verbose_name_plural = _("bank accounts")
        ordering = ["organization__code", "code"]
        permissions = [
            ("manage_bank_accounts", _("Can create and archive bank accounts")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"], name="bank_account_code_unique_per_organization"
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="bank_account_code_format"
            ),
            models.CheckConstraint(
                condition=~Q(name_ar="") & ~Q(name_en="") & ~Q(bank_name=""),
                name="bank_account_names_not_empty",
            ),
            models.UniqueConstraint(
                fields=["organization", "account"],
                condition=Q(is_active=True),
                name="bank_account_account_unique_while_active",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(is_active=True) & Q(archived_at__isnull=True))
                    | (Q(is_active=False) & Q(archived_at__isnull=False))
                ),
                name="bank_account_archived_at_matches_state",
            ),
        ]


# ---------------------------------------------------------------------------
# Expense vouchers (ADR-030 §3)
# ---------------------------------------------------------------------------


class FinancialDocumentStatus(models.TextChoices):
    """
    The lifecycle every Phase 5 financial document shares.

    One vocabulary rather than three near-identical ones, because the three
    documents genuinely move through the same states for the same reasons and a
    per-document copy would drift in exactly the place — what "posted" means —
    where drift is least visible.
    """

    DRAFT = "DRAFT", _("مسودة")
    APPROVED = "APPROVED", _("معتمد")
    POSTED = "POSTED", _("مرحّل")
    REVERSED = "REVERSED", _("معكوس")


class PaymentSource(models.TextChoices):
    """Where the money leaves from. Exactly one of the two, never both."""

    CASHBOX = "CASHBOX", _("صندوق")
    BANK = "BANK", _("حساب بنكي")


class ExpenseVoucher(TimeStampedModel):
    """
    A non-supplier operational expense, paid immediately.

    The electricity bill, a taxi, a municipal fee, a repair paid in cash — what
    Procurement is *not* for. Two model decisions enforce that boundary rather
    than merely stating it (ADR-030 §3):

    **No supplier foreign key.** The moment this can name a supplier it becomes
    a supplier invoice with no three-way match, no GRNI clearing, no purchase
    price variance and no credit-note path — and it will be used as one,
    because it is faster.

    **No tax field.** Release 1 has no approved Iraqi tax policy, and a field
    labelled "ضريبة" would invite one to be invented per voucher by whoever
    filled it in.

    An **unpaid** expense is not one of these. It is an accrual. Letting a
    voucher post to a generic payable would create a supplier subledger with no
    supplier — an unaged, unallocatable liability nobody can reconcile.
    """

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="expense_vouchers",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="expense_vouchers",
        verbose_name=_("branch"),
    )
    number = models.CharField(_("number"), max_length=32, blank=True)
    #: The date the ledger records. Entered, never derived from a timestamp —
    #: an expense paid at 00:30 belongs to the business day that just ended.
    business_date = models.DateField(_("business date"))
    expense_date = models.DateField(_("expense date"))

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=FinancialDocumentStatus.choices,
        default=FinancialDocumentStatus.DRAFT,
    )

    payment_source = models.CharField(_("paid from"), max_length=10, choices=PaymentSource.choices)
    cashbox = models.ForeignKey(
        Cashbox,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="expense_vouchers",
        verbose_name=_("cashbox"),
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="expense_vouchers",
        verbose_name=_("bank account"),
    )

    beneficiary = models.CharField(_("beneficiary"), max_length=200)
    reason = models.TextField(_("reason"))
    evidence_reference = models.CharField(_("evidence"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    #: The sum of the posted lines, recomputed on every line change. Never
    #: rounded independently of them (CLAUDE.md): a total that was rounded on
    #: its own would disagree with the journal it produces by a rounding unit,
    #: and the journal would then refuse to balance.
    total_amount = models.DecimalField(
        _("total"),
        max_digits=AMOUNT_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_expense_vouchers",
        verbose_name=_("created by"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_expense_vouchers",
        verbose_name=_("approved by"),
    )
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_expense_vouchers",
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)

    journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="expense_vouchers",
        verbose_name=_("journal entry"),
    )
    reversal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_expense_vouchers",
        verbose_name=_("reversal entry"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("expense voucher")
        verbose_name_plural = _("expense vouchers")
        ordering = ["-business_date", "-id"]
        permissions = [
            ("manage_expense_vouchers", _("Can create and edit expense vouchers")),
            ("approve_expense_vouchers", _("Can approve and post expense vouchers")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="expense_voucher_number_unique_per_organization",
            ),
            # Numbered once it leaves draft, like a journal: an abandoned draft
            # must not burn a number out of a gapless sequence.
            models.CheckConstraint(
                condition=Q(status=FinancialDocumentStatus.DRAFT) | ~Q(number=""),
                name="expense_voucher_numbered_once_approved",
            ),
            # Exactly one payment source, and it must match the declared kind.
            # Two would make the credit side ambiguous; zero would make it
            # absent, and the voucher would post a one-sided journal.
            models.CheckConstraint(
                condition=(
                    (
                        Q(payment_source=PaymentSource.CASHBOX)
                        & Q(cashbox__isnull=False)
                        & Q(bank_account__isnull=True)
                    )
                    | (
                        Q(payment_source=PaymentSource.BANK)
                        & Q(bank_account__isnull=False)
                        & Q(cashbox__isnull=True)
                    )
                ),
                name="expense_voucher_exactly_one_payment_source",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=Decimal("0")),
                name="expense_voucher_total_not_negative",
            ),
            models.CheckConstraint(
                condition=~Q(status=FinancialDocumentStatus.POSTED)
                | (Q(posted_at__isnull=False) & Q(journal_entry__isnull=False)),
                name="expense_voucher_posted_carries_its_journal",
            ),
            models.CheckConstraint(
                condition=~Q(status=FinancialDocumentStatus.APPROVED)
                | Q(approved_at__isnull=False),
                name="expense_voucher_approved_records_when",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "status", "business_date"],
                name="expense_voucher_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.number or f"draft expense #{self.pk}"

    @property
    def is_editable(self) -> bool:
        return self.status == FinancialDocumentStatus.DRAFT

    @property
    def payment_account(self) -> Account | None:
        """The GL account the credit side lands in, whichever source was chosen."""
        if self.cashbox is not None:
            return self.cashbox.account
        if self.bank_account is not None:
            return self.bank_account.account
        return None


class ExpenseVoucherLine(models.Model):
    """
    One expense account and what was spent on it.

    Deterministic order by `sequence`, which is also the allocation tie-break
    key — the same discipline every other line model in this project follows,
    so a total split across lines is reproducible rather than dependent on
    queryset order.
    """

    voucher = models.ForeignKey(
        ExpenseVoucher,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("voucher"),
    )
    sequence = models.PositiveIntegerField(_("sequence"))
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="expense_voucher_lines",
        verbose_name=_("account"),
    )
    cost_center = models.ForeignKey(
        CostCenter,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="expense_voucher_lines",
        verbose_name=_("cost center"),
    )
    description = models.CharField(_("description"), max_length=255, blank=True)
    amount = models.DecimalField(
        _("amount"), max_digits=AMOUNT_MAX_DIGITS, decimal_places=MONEY_PLACES
    )

    class Meta:
        verbose_name = _("expense voucher line")
        verbose_name_plural = _("expense voucher lines")
        ordering = ["voucher_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["voucher", "sequence"], name="expense_line_sequence_unique_per_voucher"
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=Decimal("0")), name="expense_line_amount_is_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.account.code} {self.amount}"


# ---------------------------------------------------------------------------
# Accruals and prepayments (ADR-030 §§4–5)
# ---------------------------------------------------------------------------


class AccrualDocument(TimeStampedModel):
    """
    An expense incurred but not yet invoiced or paid.

    `Dr Expense · Cr ACCRUED_EXPENSES_PAYABLE`.

    The hard part is not the posting. It is what happens when the real invoice
    arrives six weeks later, because both obvious behaviours are wrong: posting
    the invoice on top recognises the expense twice, and letting the accrual
    linger overstates the liability forever.

    So this carries an optional link to the `SupplierInvoice` that replaces it,
    and **linking is not creating** — Accounting never creates a supplier
    invoice; that document belongs to Procurement and arrives through
    Procurement. Clearing is then an explicit command that reverses this
    accrual's own journal, so the expense stands recognised exactly once.
    """

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="accruals",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="accruals",
        verbose_name=_("branch"),
    )
    number = models.CharField(_("number"), max_length=32, blank=True)
    business_date = models.DateField(_("business date"))
    description = models.CharField(_("description"), max_length=255)
    reason = models.TextField(_("reason"), blank=True)
    evidence_reference = models.CharField(_("evidence"), max_length=200, blank=True)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=FinancialDocumentStatus.choices,
        default=FinancialDocumentStatus.DRAFT,
    )
    total_amount = models.DecimalField(
        _("total"),
        max_digits=AMOUNT_MAX_DIGITS,
        decimal_places=MONEY_PLACES,
        default=Decimal("0"),
    )

    #: The common month-end case: the accrual exists only to land the cost in
    #: the right month and is meant to unwind on the first of the next.
    auto_reverse_on = models.DateField(_("automatic reversal date"), null=True, blank=True)

    #: The invoice that eventually replaced this accrual. A **link**, never a
    #: creation — Accounting does not write Procurement's documents.
    settled_by_invoice = models.ForeignKey(
        "procurement.SupplierInvoice",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cleared_accruals",
        verbose_name=_("settled by invoice"),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_accruals",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_accruals",
    )
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_accruals",
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)

    journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="accruals",
    )
    reversal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_accruals",
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("accrual")
        verbose_name_plural = _("accruals")
        ordering = ["-business_date", "-id"]
        permissions = [
            ("manage_accruals", _("Can create, approve and post accruals")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="accrual_number_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(status=FinancialDocumentStatus.DRAFT) | ~Q(number=""),
                name="accrual_numbered_once_approved",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=Decimal("0")),
                name="accrual_total_not_negative",
            ),
            models.CheckConstraint(
                condition=~Q(status=FinancialDocumentStatus.POSTED)
                | (Q(posted_at__isnull=False) & Q(journal_entry__isnull=False)),
                name="accrual_posted_carries_its_journal",
            ),
        ]

    def __str__(self) -> str:
        return self.number or f"draft accrual #{self.pk}"

    @property
    def is_editable(self) -> bool:
        return self.status == FinancialDocumentStatus.DRAFT


class AccrualLine(models.Model):
    """One expense account accrued, and how much."""

    accrual = models.ForeignKey(
        AccrualDocument,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("accrual"),
    )
    sequence = models.PositiveIntegerField(_("sequence"))
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="accrual_lines",
        verbose_name=_("expense account"),
    )
    cost_center = models.ForeignKey(
        CostCenter,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="accrual_lines",
        verbose_name=_("cost center"),
    )
    description = models.CharField(_("description"), max_length=255, blank=True)
    amount = models.DecimalField(
        _("amount"), max_digits=AMOUNT_MAX_DIGITS, decimal_places=MONEY_PLACES
    )

    class Meta:
        verbose_name = _("accrual line")
        verbose_name_plural = _("accrual lines")
        ordering = ["accrual_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["accrual", "sequence"], name="accrual_line_sequence_unique"
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=Decimal("0")), name="accrual_line_amount_is_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.account.code} {self.amount}"


class AmortizationFrequency(models.TextChoices):
    MONTHLY = "MONTHLY", _("شهري")
    QUARTERLY = "QUARTERLY", _("ربع سنوي")


class Prepayment(TimeStampedModel):
    """
    Payment before the expense is consumed.

    `Dr PREPAID_EXPENSE · Cr cash/bank` when paid, then one
    `Dr Expense · Cr PREPAID_EXPENSE` per schedule line as it is consumed.

    The schedule is split with `apps/core/allocation.py` and never by dividing
    the total by the period count and rounding each period. This is the ADR-006
    counterexample in a different costume: 1,000,000 over three months at three
    decimal places is 333,333.333 each, which sums to 999,999.999. The residual
    is one thousandth of a dinar and it is fatal — the prepaid account never
    reaches zero, the balance sheet carries a permanent 0.001 asset, and the
    account cannot be closed at year end without a plug.
    """

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="prepayments",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="prepayments",
        verbose_name=_("branch"),
    )
    number = models.CharField(_("number"), max_length=32, blank=True)
    business_date = models.DateField(_("business date"))
    description = models.CharField(_("description"), max_length=255)
    source_reference = models.CharField(_("source document"), max_length=200, blank=True)
    evidence_reference = models.CharField(_("evidence"), max_length=200, blank=True)

    status = models.CharField(
        _("status"),
        max_length=10,
        choices=FinancialDocumentStatus.choices,
        default=FinancialDocumentStatus.DRAFT,
    )

    total_amount = models.DecimalField(
        _("total"), max_digits=AMOUNT_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    start_date = models.DateField(_("start date"))
    end_date = models.DateField(_("end date"))
    frequency = models.CharField(
        _("frequency"),
        max_length=12,
        choices=AmortizationFrequency.choices,
        default=AmortizationFrequency.MONTHLY,
    )
    period_count = models.PositiveSmallIntegerField(_("number of periods"))

    expense_account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="prepayment_expense_of",
        verbose_name=_("expense account"),
    )
    prepaid_account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="prepayment_asset_of",
        verbose_name=_("prepaid account"),
    )
    cost_center = models.ForeignKey(
        CostCenter,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="prepayments",
        verbose_name=_("cost center"),
    )

    payment_source = models.CharField(_("paid from"), max_length=10, choices=PaymentSource.choices)
    cashbox = models.ForeignKey(
        Cashbox, on_delete=models.PROTECT, null=True, blank=True, related_name="prepayments"
    )
    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, null=True, blank=True, related_name="prepayments"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_prepayments",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_prepayments",
    )
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_prepayments",
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)

    journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="prepayments",
    )
    reversal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_prepayments",
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("prepayment")
        verbose_name_plural = _("prepayments")
        ordering = ["-business_date", "-id"]
        permissions = [
            ("manage_prepayments", _("Can create, approve and amortize prepayments")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~Q(number=""),
                name="prepayment_number_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(status=FinancialDocumentStatus.DRAFT) | ~Q(number=""),
                name="prepayment_numbered_once_approved",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gt=Decimal("0")),
                name="prepayment_total_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(end_date__gte=models.F("start_date")),
                name="prepayment_ends_after_it_starts",
            ),
            models.CheckConstraint(
                condition=Q(period_count__gte=1), name="prepayment_has_at_least_one_period"
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(payment_source=PaymentSource.CASHBOX)
                        & Q(cashbox__isnull=False)
                        & Q(bank_account__isnull=True)
                    )
                    | (
                        Q(payment_source=PaymentSource.BANK)
                        & Q(bank_account__isnull=False)
                        & Q(cashbox__isnull=True)
                    )
                ),
                name="prepayment_exactly_one_payment_source",
            ),
        ]

    def __str__(self) -> str:
        return self.number or f"draft prepayment #{self.pk}"

    @property
    def is_editable(self) -> bool:
        return self.status == FinancialDocumentStatus.DRAFT

    @property
    def payment_account(self) -> Account | None:
        if self.cashbox is not None:
            return self.cashbox.account
        if self.bank_account is not None:
            return self.bank_account.account
        return None


class ScheduleLineStatus(models.TextChoices):
    PLANNED = "PLANNED", _("مخطَّط")
    POSTED = "POSTED", _("مرحّل")
    REVERSED = "REVERSED", _("معكوس")


class PrepaymentScheduleLine(models.Model):
    """
    One period's share of a prepayment.

    A **posted** line is never rewritten when the master record changes:
    amending a prepayment re-plans its `PLANNED` lines only. Recomputing the
    whole schedule would silently disagree with journals already in the ledger.
    """

    prepayment = models.ForeignKey(
        Prepayment,
        on_delete=models.CASCADE,
        related_name="schedule_lines",
        verbose_name=_("prepayment"),
    )
    sequence = models.PositiveIntegerField(_("sequence"))
    period_start = models.DateField(_("period start"))
    period_end = models.DateField(_("period end"))
    amount = models.DecimalField(
        _("amount"), max_digits=AMOUNT_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    status = models.CharField(
        _("status"),
        max_length=10,
        choices=ScheduleLineStatus.choices,
        default=ScheduleLineStatus.PLANNED,
    )
    journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="prepayment_schedule_lines",
    )
    reversal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_prepayment_schedule_lines",
    )
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)

    class Meta:
        verbose_name = _("prepayment schedule line")
        verbose_name_plural = _("prepayment schedule lines")
        ordering = ["prepayment_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["prepayment", "sequence"], name="prepayment_line_sequence_unique"
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=Decimal("0")), name="prepayment_line_amount_is_positive"
            ),
            models.CheckConstraint(
                condition=Q(period_end__gte=models.F("period_start")),
                name="prepayment_line_period_is_ordered",
            ),
            models.CheckConstraint(
                condition=~Q(status=ScheduleLineStatus.POSTED)
                | (Q(journal_entry__isnull=False) & Q(posted_at__isnull=False)),
                name="prepayment_line_posted_carries_its_journal",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.prepayment} #{self.sequence} {self.amount}"


# ---------------------------------------------------------------------------
# Year-end close (ADR-031 §4)
# ---------------------------------------------------------------------------


class YearEndClose(TimeStampedModel):
    """
    The once-only record that a fiscal year was closed.

    Once-only is enforced **twice over**. The closing journal carries a source
    identity, and ADR-017's per-organization uniqueness on source identity makes
    a second one impossible at the database. This row additionally carries a
    partial unique constraint on `(organization, fiscal_year)` where the
    reversal is null — so a year reopened by exact reversal can be closed again,
    and a year already closed cannot.

    A **policy version** is recorded, so a year closed under one set of rules
    stays interpretable after the rules change.
    """

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="year_end_closes",
        verbose_name=_("organization"),
    )
    fiscal_year = models.ForeignKey(
        FiscalYear,
        on_delete=models.PROTECT,
        related_name="closes",
        verbose_name=_("fiscal year"),
    )
    net_result = models.DecimalField(
        _("net result"), max_digits=AMOUNT_MAX_DIGITS, decimal_places=MONEY_PLACES
    )
    policy_version = models.CharField(_("policy version"), max_length=32)
    evidence_reference = models.CharField(_("evidence"), max_length=200, blank=True)

    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="closed_fiscal_years",
    )
    closed_at = models.DateTimeField(_("closed at"), auto_now_add=True)

    journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        related_name="year_end_closes",
        verbose_name=_("closing journal"),
    )
    reversal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_year_end_closes",
        verbose_name=_("reversal journal"),
    )
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reopened_fiscal_years",
    )
    reversal_reason = models.TextField(_("reversal reason"), blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("year-end close")
        verbose_name_plural = _("year-end closes")
        ordering = ["-closed_at"]
        permissions = [
            ("close_fiscal_year", _("Can close and reopen a fiscal year")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "fiscal_year"],
                condition=Q(reversal_entry__isnull=True),
                name="year_end_close_once_while_not_reversed",
            ),
            # Reopening leaves both journals in the ledger and records why.
            # A reversal with no reason is a reversal nobody can explain later,
            # and this is the single most consequential act in the module.
            models.CheckConstraint(
                condition=Q(reversal_entry__isnull=True) | ~Q(reversal_reason=""),
                name="year_end_close_reversal_states_its_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.code} {self.fiscal_year.year}"

    @property
    def is_reversed(self) -> bool:
        return self.reversal_entry_id is not None
