"""
Shared models: the timestamp base and the audit event log.

`TimeStampedModel` answers "when". `AuditEvent` answers "who, what, why, and
under which request" — they are different jobs and one does not substitute for
the other.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class TimeStampedModel(models.Model):
    """Records when a row was created and last changed.

    This is not the audit trail. It answers "when", never "who" or "why", and
    it says nothing about posted ledger entries, which are immutable by
    construction rather than by timestamp.
    """

    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        abstract = True


class AuditAction(models.TextChoices):
    """
    What happened.

    Deliberately includes the actions Task 0.6 will need — posting, reversal,
    failed posting, period close and reopen — so the accounting kernel does not
    have to extend this enum and migrate the column while building.
    """

    CREATED = "CREATED", _("إنشاء")
    UPDATED = "UPDATED", _("تعديل")
    #: A row genuinely removed. Rare and never applicable to posted ledger
    #: state, which is immutable — this is for records that never reached it,
    #: such as a discarded draft journal. Distinct from DEACTIVATED, which
    #: keeps the row and withdraws it from use.
    DELETED = "DELETED", _("حذف")
    DEACTIVATED = "DEACTIVATED", _("إيقاف")
    SUBMITTED = "SUBMITTED", _("إرسال")
    APPROVED = "APPROVED", _("اعتماد")
    REJECTED = "REJECTED", _("رفض")
    #: Abandoned before it reached the ledger, with the record kept. Distinct
    #: from REJECTED, which is somebody refusing an approval, and from DELETED,
    #: which removes the row: a cancelled physical count froze a warehouse for
    #: an afternoon, and that is a fact somebody may later have to explain.
    CANCELLED = "CANCELLED", _("إلغاء")
    POSTED = "POSTED", _("ترحيل")
    POSTING_FAILED = "POSTING_FAILED", _("فشل الترحيل")
    REVERSED = "REVERSED", _("عكس القيد")
    PERIOD_CLOSED = "PERIOD_CLOSED", _("إقفال فترة")
    PERIOD_REOPENED = "PERIOD_REOPENED", _("إعادة فتح فترة")
    ACCESS_GRANTED = "ACCESS_GRANTED", _("منح صلاحية")
    ACCESS_REVOKED = "ACCESS_REVOKED", _("سحب صلاحية")
    DOCUMENT_DOWNLOADED = "DOCUMENT_DOWNLOADED", _("تنزيل مستند حساس")
    IMPORTED = "IMPORTED", _("استيراد")
    PERMISSION_OVERRIDE = "PERMISSION_OVERRIDE", _("تجاوز صلاحية")


class AuditEvent(models.Model):
    """
    An append-only record of something that happened.

    Not a substitute for django-simple-history, and not substituted by it.
    History records *what a row looked like*; this records *that an action was
    taken, by whom, and why*. A period reopening changes no master data and so
    leaves no history row, yet it is exactly the kind of act an auditor asks
    about.

    Immutability is enforced by a PostgreSQL trigger, not by convention — see
    migration 0001. `save()` on an existing row and `delete()` both raise at
    the database, so bulk operations, raw SQL, and the admin cannot quietly
    rewrite the trail.
    """

    occurred_at = models.DateTimeField(_("occurred at"), auto_now_add=True, db_index=True)
    action = models.CharField(_("action"), max_length=32, choices=AuditAction.choices)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_events",
        verbose_name=_("actor"),
        help_text=_("Null for system actions such as a scheduled job."),
    )
    actor_label = models.CharField(
        _("actor label"),
        max_length=200,
        blank=True,
        help_text=_("Who the actor was at the time, kept even if they are later renamed."),
    )

    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_events",
        verbose_name=_("branch"),
    )
    #: The tenant boundary of this fact.  Branch is useful operational
    #: context, but organization is the security scope: organization-wide
    #: actions such as an access approval or a period close have no branch.
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_events",
        verbose_name=_("organization"),
    )

    correlation_id = models.UUIDField(
        _("correlation id"),
        db_index=True,
        help_text=_("Groups every event produced by one request or job."),
    )

    target_type = models.CharField(
        _("target type"),
        max_length=100,
        help_text=_("app_label.ModelName of the thing acted upon."),
    )
    target_id = models.CharField(_("target id"), max_length=64, blank=True)

    previous_state = models.JSONField(_("previous state"), null=True, blank=True)
    new_state = models.JSONField(_("new state"), null=True, blank=True)

    reason = models.TextField(_("reason"), blank=True)

    source_document_type = models.CharField(_("source document type"), max_length=100, blank=True)
    source_document_id = models.CharField(_("source document id"), max_length=64, blank=True)

    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("audit event")
        verbose_name_plural = _("audit events")
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["target_type", "target_id"], name="audit_target_idx"),
            models.Index(fields=["action", "-occurred_at"], name="audit_action_time_idx"),
            models.Index(fields=["organization", "-occurred_at"], name="audit_org_time_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(target_type=""),
                name="audit_target_type_not_empty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.target_type}:{self.target_id}"


# ---------------------------------------------------------------------------
# Automation foundation
# ---------------------------------------------------------------------------


class OutboxEventStatus(models.TextChoices):
    """Lifecycle of a durable message awaiting an idempotent handler."""

    PENDING = "PENDING", _("بانتظار المعالجة")
    PROCESSING = "PROCESSING", _("قيد المعالجة")
    RETRY = "RETRY", _("إعادة محاولة")
    COMPLETED = "COMPLETED", _("مكتملة")
    DEAD_LETTER = "DEAD_LETTER", _("فشلت نهائياً")


class OutboxAttemptStatus(models.TextChoices):
    """One auditable worker attempt; attempts are never overwritten."""

    PROCESSING = "PROCESSING", _("قيد المعالجة")
    SUCCEEDED = "SUCCEEDED", _("نجحت")
    FAILED = "FAILED", _("فشلت")
    ABANDONED = "ABANDONED", _("توقفت بعد انقطاع العامل")


class AutomationOutboxEvent(TimeStampedModel):
    """
    A transactional outbox message.

    Domain services call the outbox service *inside their existing database
    transaction*.  Therefore a rolled-back SalesDay, import, or approval does
    not leave a message for work that never happened.  A message is scoped to
    one organisation and, where applicable, one branch; payloads are small
    references and facts, never credentials or source files.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="automation_outbox_events",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="automation_outbox_events",
        null=True,
        blank=True,
        verbose_name=_("branch"),
    )

    event_type = models.CharField(_("event type"), max_length=100)
    idempotency_key = models.CharField(_("idempotency key"), max_length=200)
    payload = models.JSONField(_("payload"), default=dict, blank=True)
    payload_hash = models.CharField(_("payload hash"), max_length=64)
    correlation_id = models.UUIDField(_("correlation id"), db_index=True)

    source_type = models.CharField(_("source type"), max_length=100, blank=True)
    source_id = models.CharField(_("source id"), max_length=64, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_automation_outbox_events",
        verbose_name=_("created by"),
    )

    status = models.CharField(
        _("status"),
        max_length=16,
        choices=OutboxEventStatus.choices,
        default=OutboxEventStatus.PENDING,
        db_index=True,
    )
    available_at = models.DateTimeField(_("available at"), default=timezone.now, db_index=True)
    claimed_by = models.UUIDField(_("claimed by worker"), null=True, blank=True)
    claimed_at = models.DateTimeField(_("claimed at"), null=True, blank=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)
    attempt_count = models.PositiveSmallIntegerField(_("attempt count"), default=0)
    last_error = models.TextField(_("last error"), blank=True)

    class Meta:
        verbose_name = _("automation outbox event")
        verbose_name_plural = _("automation outbox events")
        ordering = ("available_at", "id")
        permissions = [
            ("view_automation_outbox", _("Can view automation outbox and dead letters")),
            ("replay_automation_outbox", _("Can replay a dead-letter automation event")),
        ]
        indexes = [
            models.Index(fields=["status", "available_at"], name="outbox_due_message_idx"),
            models.Index(
                fields=["organization", "status", "available_at"],
                name="outbox_org_status_idx",
            ),
            models.Index(fields=["source_type", "source_id"], name="outbox_source_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "event_type", "idempotency_key"],
                name="outbox_event_idempotency_unique",
            ),
            models.CheckConstraint(
                condition=~Q(event_type="") & ~Q(idempotency_key=""),
                name="outbox_event_identity_not_empty",
            ),
            models.CheckConstraint(
                condition=(~Q(status=OutboxEventStatus.COMPLETED) | Q(completed_at__isnull=False)),
                name="outbox_completed_records_time",
            ),
            models.CheckConstraint(
                condition=(~Q(status=OutboxEventStatus.DEAD_LETTER) | ~Q(last_error="")),
                name="outbox_dead_letter_records_error",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event_type}:{self.idempotency_key} ({self.status})"


class AutomationOutboxAttempt(TimeStampedModel):
    """The immutable evidence of one attempt to process an outbox event."""

    event = models.ForeignKey(
        AutomationOutboxEvent,
        on_delete=models.PROTECT,
        related_name="attempts",
        verbose_name=_("outbox event"),
    )
    attempt_number = models.PositiveSmallIntegerField(_("attempt number"))
    worker_id = models.UUIDField(_("worker id"), default=uuid.uuid4)
    status = models.CharField(_("status"), max_length=16, choices=OutboxAttemptStatus.choices)
    started_at = models.DateTimeField(_("started at"), default=timezone.now)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)
    duration_ms = models.PositiveIntegerField(_("duration (ms)"), null=True, blank=True)
    error = models.TextField(_("error"), blank=True)

    class Meta:
        verbose_name = _("automation outbox attempt")
        verbose_name_plural = _("automation outbox attempts")
        ordering = ("event", "attempt_number")
        constraints = [
            models.UniqueConstraint(
                fields=["event", "attempt_number"], name="outbox_attempt_number_unique"
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=[OutboxAttemptStatus.SUCCEEDED, OutboxAttemptStatus.FAILED])
                    | Q(finished_at__isnull=False)
                ),
                name="outbox_finished_attempt_records_time",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event_id}#{self.attempt_number} ({self.status})"


class AutomationExceptionStatus(models.TextChoices):
    OPEN = "OPEN", _("مفتوح")
    RESOLVED = "RESOLVED", _("محلول")


class AutomationTaskStatus(models.TextChoices):
    OPEN = "OPEN", _("جديدة")
    ACKNOWLEDGED = "ACKNOWLEDGED", _("تم الاستلام")
    RESOLVED = "RESOLVED", _("محلولة")
    CANCELLED = "CANCELLED", _("ملغاة")


class AutomationSeverity(models.TextChoices):
    LOW = "LOW", _("منخفضة")
    MEDIUM = "MEDIUM", _("متوسطة")
    HIGH = "HIGH", _("عالية")
    CRITICAL = "CRITICAL", _("حرجة")


class AutomationDataSensitivity(models.TextChoices):
    OPERATIONAL = "OPERATIONAL", _("تشغيلية")
    FINANCIAL = "FINANCIAL", _("مالية")
    HR_RESTRICTED = "HR_RESTRICTED", _("موارد بشرية مقيدة")


class AutomationException(TimeStampedModel):
    """
    A persistent, owned condition detected by automation.

    `scope_key` makes a branch-null condition deterministic for the partial
    dedupe index (PostgreSQL treats NULLs as distinct).  The detector updates
    `last_detected_at` and `seen_count`; it never rewrites the source close,
    statement, or posted accounting document that supplied the evidence.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="automation_exceptions",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="automation_exceptions",
        null=True,
        blank=True,
        verbose_name=_("branch"),
    )
    scope_key = models.CharField(_("scope key"), max_length=32)
    code = models.CharField(_("exception code"), max_length=100)
    target_type = models.CharField(_("target type"), max_length=100)
    target_id = models.CharField(_("target id"), max_length=64)
    amount = models.DecimalField(
        _("amount"), max_digits=21, decimal_places=3, null=True, blank=True
    )
    severity = models.CharField(_("severity"), max_length=12, choices=AutomationSeverity.choices)
    is_blocking = models.BooleanField(_("blocks controlled posting or close"), default=False)
    sensitivity = models.CharField(
        _("data sensitivity"),
        max_length=16,
        choices=AutomationDataSensitivity.choices,
        default=AutomationDataSensitivity.OPERATIONAL,
    )
    owner_role = models.CharField(_("owner role"), max_length=64, blank=True)
    owner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_automation_exceptions",
        null=True,
        blank=True,
        verbose_name=_("owner user"),
    )
    source_event = models.ForeignKey(
        AutomationOutboxEvent,
        on_delete=models.PROTECT,
        related_name="exceptions",
        null=True,
        blank=True,
        verbose_name=_("source event"),
    )
    details = models.JSONField(_("details"), default=dict, blank=True)
    status = models.CharField(
        _("status"),
        max_length=12,
        choices=AutomationExceptionStatus.choices,
        default=AutomationExceptionStatus.OPEN,
        db_index=True,
    )
    first_detected_at = models.DateTimeField(_("first detected at"), default=timezone.now)
    last_detected_at = models.DateTimeField(_("last detected at"), default=timezone.now)
    seen_count = models.PositiveIntegerField(_("times detected"), default=1)
    resolved_at = models.DateTimeField(_("resolved at"), null=True, blank=True)
    resolution = models.TextField(_("resolution"), blank=True)

    class Meta:
        verbose_name = _("automation exception")
        verbose_name_plural = _("automation exceptions")
        ordering = ("-is_blocking", "-last_detected_at", "-id")
        permissions = [
            ("view_automation_exception", _("Can view automation exceptions")),
        ]
        indexes = [
            models.Index(
                fields=["organization", "status", "severity"],
                name="automation_exception_state_idx",
            ),
            models.Index(fields=["target_type", "target_id"], name="autom_exc_target_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "scope_key", "code", "target_type", "target_id"],
                condition=Q(status=AutomationExceptionStatus.OPEN),
                name="automation_open_exception_deduped",
            ),
            models.CheckConstraint(
                condition=~Q(code="") & ~Q(target_type="") & ~Q(target_id=""),
                name="automation_exception_identity_not_empty",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status=AutomationExceptionStatus.RESOLVED) | Q(resolved_at__isnull=False)
                ),
                name="automation_resolved_exception_records_time",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.target_type}:{self.target_id} ({self.status})"


class AutomationTask(TimeStampedModel):
    """A role- or user-owned, deduplicated item in the in-app task inbox."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="automation_tasks",
        verbose_name=_("organization"),
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="automation_tasks",
        null=True,
        blank=True,
        verbose_name=_("branch"),
    )
    exception = models.ForeignKey(
        AutomationException,
        on_delete=models.PROTECT,
        related_name="tasks",
        null=True,
        blank=True,
        verbose_name=_("exception"),
    )
    source_event = models.ForeignKey(
        AutomationOutboxEvent,
        on_delete=models.PROTECT,
        related_name="tasks",
        null=True,
        blank=True,
        verbose_name=_("source event"),
    )
    task_type = models.CharField(_("task type"), max_length=100)
    deduplication_key = models.CharField(_("deduplication key"), max_length=240)
    target_type = models.CharField(_("target type"), max_length=100)
    target_id = models.CharField(_("target id"), max_length=64)
    severity = models.CharField(_("severity"), max_length=12, choices=AutomationSeverity.choices)
    sensitivity = models.CharField(
        _("data sensitivity"),
        max_length=16,
        choices=AutomationDataSensitivity.choices,
        default=AutomationDataSensitivity.OPERATIONAL,
    )
    title = models.CharField(_("title"), max_length=240)
    summary = models.TextField(_("summary"), blank=True)
    payload = models.JSONField(_("payload"), default=dict, blank=True)
    assignee_role = models.CharField(_("assignee role"), max_length=64, blank=True)
    assignee_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assigned_automation_tasks",
        null=True,
        blank=True,
        verbose_name=_("assignee user"),
    )
    due_at = models.DateTimeField(_("due at"), null=True, blank=True)
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=AutomationTaskStatus.choices,
        default=AutomationTaskStatus.OPEN,
        db_index=True,
    )
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="acknowledged_automation_tasks",
        null=True,
        blank=True,
        verbose_name=_("acknowledged by"),
    )
    acknowledged_at = models.DateTimeField(_("acknowledged at"), null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="resolved_automation_tasks",
        null=True,
        blank=True,
        verbose_name=_("resolved by"),
    )
    resolved_at = models.DateTimeField(_("resolved at"), null=True, blank=True)
    resolution = models.TextField(_("resolution"), blank=True)
    escalation_count = models.PositiveSmallIntegerField(_("escalation count"), default=0)
    last_escalated_at = models.DateTimeField(_("last escalated at"), null=True, blank=True)

    class Meta:
        verbose_name = _("automation task")
        verbose_name_plural = _("automation tasks")
        ordering = ("-severity", "due_at", "-created_at")
        permissions = [
            ("view_automation_task", _("Can view automation task inbox")),
            ("acknowledge_automation_task", _("Can acknowledge automation tasks")),
        ]
        indexes = [
            models.Index(
                fields=["organization", "status", "severity"], name="automation_task_state_idx"
            ),
            models.Index(fields=["assignee_user", "status"], name="automation_task_user_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "deduplication_key"],
                condition=Q(
                    status__in=[AutomationTaskStatus.OPEN, AutomationTaskStatus.ACKNOWLEDGED]
                ),
                name="automation_open_task_deduped",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status=AutomationTaskStatus.ACKNOWLEDGED)
                    | (Q(acknowledged_by__isnull=False) & Q(acknowledged_at__isnull=False))
                ),
                name="automation_acknowledged_task_records_actor",
            ),
            models.CheckConstraint(
                condition=(~Q(status=AutomationTaskStatus.RESOLVED) | Q(resolved_at__isnull=False)),
                name="automation_resolved_task_records_time",
            ),
            models.CheckConstraint(
                condition=~Q(task_type="") & ~Q(deduplication_key=""),
                name="automation_task_identity_not_empty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.task_type} {self.target_type}:{self.target_id} ({self.status})"
