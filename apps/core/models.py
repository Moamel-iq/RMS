"""
Shared models: the timestamp base and the audit event log.

`TimeStampedModel` answers "when". `AuditEvent` answers "who, what, why, and
under which request" — they are different jobs and one does not substitute for
the other.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
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
    DEACTIVATED = "DEACTIVATED", _("إيقاف")
    SUBMITTED = "SUBMITTED", _("إرسال")
    APPROVED = "APPROVED", _("اعتماد")
    REJECTED = "REJECTED", _("رفض")
    POSTED = "POSTED", _("ترحيل")
    POSTING_FAILED = "POSTING_FAILED", _("فشل الترحيل")
    REVERSED = "REVERSED", _("عكس القيد")
    PERIOD_CLOSED = "PERIOD_CLOSED", _("إقفال فترة")
    PERIOD_REOPENED = "PERIOD_REOPENED", _("إعادة فتح فترة")
    ACCESS_GRANTED = "ACCESS_GRANTED", _("منح صلاحية")
    ACCESS_REVOKED = "ACCESS_REVOKED", _("سحب صلاحية")
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
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(target_type=""),
                name="audit_target_type_not_empty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.target_type}:{self.target_id}"
