"""
The insight kernel: a stable identity, immutable observations, and an
append-only lifecycle.

## The contradiction this shape exists to resolve

An analytical finding wants to be two incompatible things at once. It is a
*living case* — somebody acknowledges it, it gets worse, it goes away and comes
back — and it is also a *historical record*, because a figure shown to an owner
last Tuesday must still read the same next year. A single mutable row cannot be
both: every re-detection would overwrite the evidence that justified what was
said, and the case would silently rewrite its own past.

So the three concerns are three tables:

    Insight             *who this is*   — one row per logical condition, forever
    InsightObservation  *what was seen* — one immutable row per run
    InsightEvent        *what happened* — one immutable row per transition

The identity never changes, the observations never change, the events never
change. "Current severity" and "last seen" are **derived from the latest
observation**, and "current status" is derived from the latest event. Nothing
is mirrored onto `Insight`, because a mirror is a second truth that drifts.

## Why runs and outcomes are separate rows too

A run that found nothing is a fact worth keeping — it is the difference between
"we looked and the condition is gone" and "nobody has looked since Tuesday",
and only the first may resolve a case. `InsightDetectorOutcome` therefore
records **two independent things** per detector: whether the execution finished
(`SUCCEEDED` / `SKIPPED` / `FAILED`) and how much of the population it could
actually see (`COMPLETE` / `PARTIAL` / `INSUFFICIENT`). A detector that ran
perfectly over half the data is `SUCCEEDED` + `PARTIAL`, and that combination
is precisely the one that must never auto-resolve anything.

## Evidence is a snapshot, not a ledger

Observation evidence is a photograph of what the numbers were, taken at a named
cutoff. It is never consulted as a source of business truth — live figures are
always re-derived from the authoritative ledgers. Its only job is to let a
reader reproduce and audit a claim that was made at a moment.

Every Decimal in it is stored as an **exact locale-independent string**, and
floats are rejected recursively, for the reason the whole repository forbids
them: `0.1 + 0.2` is not `0.3`, and an evidence file that cannot be re-added is
not evidence.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class InsightDomain(models.TextChoices):
    """Which part of the business a finding is about."""

    DATA_QUALITY = "DATA_QUALITY", _("جودة البيانات")
    INVENTORY = "INVENTORY", _("المخزون")
    PROCUREMENT = "PROCUREMENT", _("المشتريات")
    SALES = "SALES", _("المبيعات")
    KITCHEN = "KITCHEN", _("المطبخ والكلفة")
    ACCOUNTING = "ACCOUNTING", _("المحاسبة")
    HR = "HR", _("الموارد البشرية")
    CONDUCT = "CONDUCT", _("سلوك الاستخدام")


class InsightSeverity(models.TextChoices):
    """
    Mirrors `AutomationSeverity` deliberately.

    A HIGH finding is bridged into the existing task inbox, and two severity
    vocabularies that had to be translated between each other would eventually
    disagree about what "urgent" means.
    """

    LOW = "LOW", _("منخفضة")
    MEDIUM = "MEDIUM", _("متوسطة")
    HIGH = "HIGH", _("عالية")
    CRITICAL = "CRITICAL", _("حرجة")


class InsightConfidence(models.TextChoices):
    """
    How far the reader should trust the number, given what could be seen.

    Separate from severity, because they answer different questions: severity
    is *how bad if true*, confidence is *how sure we are*. A finding may be
    HIGH severity and LOW confidence, and collapsing the two would let a
    half-seen population shout.
    """

    LOW = "LOW", _("منخفضة")
    MEDIUM = "MEDIUM", _("متوسطة")
    HIGH = "HIGH", _("عالية")


class InsightSensitivity(models.TextChoices):
    """Mirrors `AutomationDataSensitivity` for the same reason severity does."""

    OPERATIONAL = "OPERATIONAL", _("تشغيلية")
    FINANCIAL = "FINANCIAL", _("مالية")
    HR_RESTRICTED = "HR_RESTRICTED", _("مقيّدة — موارد بشرية")


class InsightEventType(models.TextChoices):
    """The five transitions a case can make. Closed on purpose."""

    OPENED = "OPENED", _("فُتحت")
    ACKNOWLEDGED = "ACKNOWLEDGED", _("جرى الاطلاع")
    RESOLVED = "RESOLVED", _("انتهت")
    DISMISSED = "DISMISSED", _("صُرف النظر")
    REOPENED = "REOPENED", _("أُعيد فتحها")


class RunTrigger(models.TextChoices):
    MANUAL = "MANUAL", _("يدوي")
    SCHEDULED = "SCHEDULED", _("مجدول")


class DetectorOutcome(models.TextChoices):
    """
    Did the execution finish? Independent of how much it could see.

    `SUCCEEDED` covers a run that legitimately found nothing — that is an
    answer, not an absence, and it is the only kind of answer allowed to
    resolve an open case.
    """

    SUCCEEDED = "SUCCEEDED", _("نجح")
    SKIPPED = "SKIPPED", _("تُخطّي")
    FAILED = "FAILED", _("أخفق")


class DetectorCoverage(models.TextChoices):
    """
    How much of the intended population the detector could actually evaluate.

    The middle value is the one that matters. `PARTIAL` means the arithmetic
    was sound over the rows it could see and blind to the rest — so its
    findings may be shown with reduced confidence, and its silence proves
    nothing about the rows it never reached.
    """

    COMPLETE = "COMPLETE", _("كاملة")
    PARTIAL = "PARTIAL", _("جزئية")
    INSUFFICIENT = "INSUFFICIENT", _("غير كافية")


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class InsightRun(TimeStampedModel):
    """
    One execution request, and the cutoffs it read the world at.

    Immutable except for `finished_at`, enforced by a database trigger with an
    **allowlist** rather than a blocklist — the repository learned that lesson
    in `accounting/0005`, where a forgotten column in a blocklist was a hole
    nobody could see.

    The window is half-open `[period_start, period_end)` and both are business
    dates. `source_cutoffs` records the high-watermark each authoritative
    ledger was read at, so two detectors in one run can never quietly combine
    different views of the same ledger — and so a reader a year later can ask
    what the data looked like when the claim was made.
    """

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="insight_runs",
        verbose_name=_("organization"),
    )
    #: NULL means the run covered the organization as a whole. It does not mean
    #: "any branch": an organization-wide finding needs organization-wide scope
    #: to read, which the selectors enforce.
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="insight_runs",
        verbose_name=_("branch"),
    )
    requested_detectors = models.JSONField(_("requested detectors"), default=list)
    period_start = models.DateField(_("period start"))
    period_end = models.DateField(_("period end (exclusive)"))
    trigger = models.CharField(
        _("trigger"), max_length=16, choices=RunTrigger.choices, default=RunTrigger.MANUAL
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="insight_runs",
        verbose_name=_("actor"),
        help_text=_("فارغ للتشغيل المجدول."),
    )
    settings_version = models.PositiveIntegerField(_("settings version"), default=0)
    source_cutoffs = models.JSONField(_("source cutoffs"), default=dict)
    started_at = models.DateTimeField(_("started at"))
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)

    class Meta:
        verbose_name = _("insight run")
        verbose_name_plural = _("insight runs")
        ordering = ["-started_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(period_end__gt=models.F("period_start")),
                name="insights_run_period_is_half_open",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "-started_at"], name="insights_run_org_started_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.code} · {self.period_start} → {self.period_end}"


class InsightDetectorOutcome(TimeStampedModel):
    """
    What one detector did in one run — append-only.

    Written even when nothing was found, because "we looked and it is clean" is
    the only evidence that may close a case, and it is indistinguishable from
    "nobody ran it" unless it is recorded.

    One failed detector must never erase its siblings' recorded results, which
    is why this is a row per detector rather than a field on the run.
    """

    run = models.ForeignKey(
        InsightRun,
        on_delete=models.PROTECT,
        related_name="outcomes",
        verbose_name=_("run"),
    )
    detector_code = models.CharField(_("detector code"), max_length=100)
    outcome = models.CharField(_("outcome"), max_length=16, choices=DetectorOutcome.choices)
    coverage = models.CharField(_("coverage"), max_length=16, choices=DetectorCoverage.choices)
    #: The fingerprints this detector explicitly evaluated. Auto-resolution
    #: reads this and nothing else: a case may only be closed by a run that
    #: *looked at it*, never by one that merely did not mention it.
    evaluated_fingerprints = models.JSONField(_("evaluated fingerprints"), default=list)
    evaluated_scope_count = models.PositiveIntegerField(_("evaluated scopes"), default=0)
    candidate_count = models.PositiveIntegerField(_("candidates"), default=0)
    #: A short stable code plus a safe one-line summary. Never a traceback:
    #: the technical detail goes to the logger, and a stack trace on a screen
    #: is an information leak wearing a diagnostic hat.
    error_code = models.CharField(_("error code"), max_length=100, blank=True)
    error_summary = models.CharField(_("error summary"), max_length=300, blank=True)
    notes = models.JSONField(_("limitations"), default=dict)
    finished_at = models.DateTimeField(_("finished at"))

    class Meta:
        verbose_name = _("insight detector outcome")
        verbose_name_plural = _("insight detector outcomes")
        ordering = ["run", "detector_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "detector_code"], name="insights_outcome_unique_per_run"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.detector_code} · {self.outcome} · {self.coverage}"


# ---------------------------------------------------------------------------
# The case
# ---------------------------------------------------------------------------


class Insight(TimeStampedModel):
    """
    One logical condition, for its whole life.

    The row says *who this is* and nothing about how it is doing. No severity,
    no status, no last-seen date — all three change, and a changing column on
    an identity row is how a case starts disagreeing with its own history.

    `fingerprint` is the identity, and it may contain only stable technical
    facts: ids, and the scope they live in. Never a translated label (which
    changes with the locale), never the period (which rolls), never the
    severity or the measured value (which are the very things being tracked).
    Put any of those in and the same condition becomes a new case every week,
    and nobody can acknowledge anything.
    """

    public_id = models.UUIDField(_("public id"), default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="insights",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="insights",
        verbose_name=_("branch"),
    )
    #: A human-readable rendering of the scope, for display and grouping only.
    #: Never part of the identity.
    scope_key = models.CharField(_("scope key"), max_length=200)
    detector_code = models.CharField(_("detector code"), max_length=100)
    fingerprint = models.CharField(_("fingerprint"), max_length=300)
    domain = models.CharField(_("domain"), max_length=20, choices=InsightDomain.choices)
    sensitivity = models.CharField(
        _("sensitivity"),
        max_length=20,
        choices=InsightSensitivity.choices,
        default=InsightSensitivity.OPERATIONAL,
    )
    first_run = models.ForeignKey(
        InsightRun,
        on_delete=models.PROTECT,
        related_name="opened_insights",
        verbose_name=_("first run"),
    )

    if TYPE_CHECKING:
        # Populated by `selectors.visible_insights` as queryset annotations,
        # never stored: current status comes from the latest event and the
        # rest from the latest observation. Declared here so the type checker
        # knows what a row from that selector carries — a stored column would
        # be a second truth that drifts from the history behind it.
        status: str
        latest_severity: str
        latest_confidence: str
        latest_coverage: str
        latest_title: str
        last_seen_at: Any

    class Meta:
        verbose_name = _("insight")
        verbose_name_plural = _("insights")
        ordering = ["-created_at", "-id"]
        # `view_insight` is Django's own default permission on this model.
        # The other three are declared because they are different kinds of
        # trust: deciding about a case, starting an analysis, and moving the
        # threshold that decides what counts as a finding at all.
        permissions = [
            ("manage_insight", _("Can acknowledge, dismiss and reopen a finding")),
            ("run_insights", _("Can start an analysis run")),
            ("configure_insights", _("Can change detector thresholds")),
        ]
        constraints = [
            # The deduplication authority. Not a service-layer check: two
            # concurrent runs would both look, both find nothing, and both
            # insert.
            models.UniqueConstraint(
                fields=["organization", "detector_code", "fingerprint"],
                name="insights_identity_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "detector_code"], name="insights_org_detector_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.detector_code} · {self.fingerprint}"


class InsightObservation(TimeStampedModel):
    """
    What one run saw about one case — immutable.

    Re-detection **appends**. It never rewrites severity, narrative, evidence
    or a `last_seen_at` column, because the whole point of keeping the history
    is being able to say "this was MEDIUM in August and HIGH in September" and
    show the figures behind both.

    Everything a reader is shown must exist here. If the screen displays a
    number the evidence does not contain, the claim cannot be audited and the
    screen is lying by omission.
    """

    insight = models.ForeignKey(
        Insight,
        on_delete=models.PROTECT,
        related_name="observations",
        verbose_name=_("insight"),
    )
    run = models.ForeignKey(
        InsightRun,
        on_delete=models.PROTECT,
        related_name="observations",
        verbose_name=_("run"),
    )
    period_start = models.DateField(_("period start"))
    period_end = models.DateField(_("period end (exclusive)"))
    source_cutoffs = models.JSONField(_("source cutoffs"), default=dict)
    detector_version = models.CharField(_("detector version"), max_length=32)
    settings_version = models.PositiveIntegerField(_("settings version"), default=0)
    severity = models.CharField(_("severity"), max_length=16, choices=InsightSeverity.choices)
    confidence = models.CharField(_("confidence"), max_length=16, choices=InsightConfidence.choices)
    coverage = models.CharField(_("coverage"), max_length=16, choices=DetectorCoverage.choices)
    title_ar = models.CharField(_("title"), max_length=240)
    narrative_ar = models.TextField(_("narrative"))
    recommendation_ar = models.TextField(_("recommendation"))
    evidence = models.JSONField(_("evidence"), default=dict)
    observed_at = models.DateTimeField(_("observed at"))

    class Meta:
        verbose_name = _("insight observation")
        verbose_name_plural = _("insight observations")
        ordering = ["-observed_at", "-id"]
        constraints = [
            # Storage idempotency: reprocessing one run cannot double its
            # observations, however many times the writer is called.
            models.UniqueConstraint(
                fields=["insight", "run"], name="insights_observation_unique_per_run"
            ),
            models.CheckConstraint(
                condition=Q(period_end__gt=models.F("period_start")),
                name="insights_observation_period_is_half_open",
            ),
        ]
        indexes = [
            models.Index(fields=["insight", "-observed_at"], name="insights_obs_latest_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.insight_id} · {self.observed_at:%Y-%m-%d}"


class InsightEvent(TimeStampedModel):
    """
    One lifecycle transition — immutable.

    Current status is the latest event's type. Deliberately not a column on
    `Insight`: a status column and an event log are two claims about the same
    thing, and the day they disagree is the day nobody can tell which one the
    business acted on.
    """

    insight = models.ForeignKey(
        Insight,
        on_delete=models.PROTECT,
        related_name="events",
        verbose_name=_("insight"),
    )
    event_type = models.CharField(_("event type"), max_length=20, choices=InsightEventType.choices)
    #: Set for a human action, null for a system one. Exactly one of the two
    #: is always present — enforced below.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="insight_events",
        verbose_name=_("actor"),
    )
    run = models.ForeignKey(
        InsightRun,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="events",
        verbose_name=_("run"),
    )
    reason = models.TextField(_("reason"), blank=True)
    occurred_at = models.DateTimeField(_("occurred at"))

    class Meta:
        verbose_name = _("insight event")
        verbose_name_plural = _("insight events")
        ordering = ["occurred_at", "id"]
        constraints = [
            # Somebody or something did this. A transition with neither an
            # actor nor a run is an unattributable change to a case history.
            models.CheckConstraint(
                condition=Q(actor__isnull=False) | Q(run__isnull=False),
                name="insights_event_has_an_origin",
            ),
            # Dismissal and reopening are decisions, and a decision without a
            # stated reason is not reviewable.
            models.CheckConstraint(
                condition=~Q(event_type__in=["DISMISSED", "REOPENED"]) | ~Q(reason=""),
                name="insights_event_decision_needs_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["insight", "-occurred_at"], name="insights_event_latest_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.insight_id} · {self.event_type}"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class DetectorSetting(TimeStampedModel):
    """
    One organization's override of one detector's thresholds — append-only.

    Versioned rather than edited, so an observation can name the settings
    version it was judged under. Without that, lowering a threshold in
    November would silently make every August finding look wrong.

    Defaults live in code. A row exists only where somebody deliberately
    departed from them.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="detector_settings",
        verbose_name=_("organization"),
    )
    detector_code = models.CharField(_("detector code"), max_length=100)
    version = models.PositiveIntegerField(_("version"))
    #: Validated against the detector's declared schema before it is written.
    #: Every numeric value is an exact Decimal string.
    payload = models.JSONField(_("settings"), default=dict)
    effective_from = models.DateField(_("effective from"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="detector_settings",
        verbose_name=_("created by"),
    )
    reason = models.TextField(_("reason"), blank=True)

    class Meta:
        verbose_name = _("detector setting")
        verbose_name_plural = _("detector settings")
        ordering = ["organization", "detector_code", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "detector_code", "version"],
                name="insights_setting_version_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.detector_code} v{self.version}"


__all__ = [
    "DetectorCoverage",
    "DetectorOutcome",
    "DetectorSetting",
    "Insight",
    "InsightConfidence",
    "InsightDetectorOutcome",
    "InsightDomain",
    "InsightEvent",
    "InsightEventType",
    "InsightObservation",
    "InsightRun",
    "InsightSensitivity",
    "InsightSeverity",
    "RunTrigger",
]
