"""Durable automation primitives: outbox, retry worker, exceptions and tasks.

The worker deliberately owns *delivery*, never business approval.  A handler
may collect evidence, create a draft, or surface an exception, but it may not
post a payment, journal, adjustment, refund, or payroll action.  Domain
services remain the only path that can make those financial changes.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Min, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.context import audit_context, get_actor, get_correlation_id
from apps.core.models import (
    AuditAction,
    AutomationDataSensitivity,
    AutomationException,
    AutomationExceptionStatus,
    AutomationOutboxAttempt,
    AutomationOutboxEvent,
    AutomationSeverity,
    AutomationTask,
    AutomationTaskStatus,
    OutboxAttemptStatus,
    OutboxEventStatus,
)
from apps.core.services import record_audit_event, snapshot
from apps.organizations.authorization import (
    branches_with_permission,
    has_branch_permission,
    has_organization_master_data_permission,
    organizations_with_permission,
    require_organization_permission,
    roles_at_branch,
    roles_in_organization,
)
from apps.organizations.models import Role

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
RETRY_BASE_SECONDS = 30
STALE_CLAIM_AFTER = datetime.timedelta(minutes=15)

# The exact message is recorded in a restricted audit/attempt row, but handler
# failures can contain an upstream URL or credentials.  The user-facing task
# therefore gets only a stable code and never the exception text.
MAX_STORED_ERROR_LENGTH = 1000
_SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "api_key",
        "access_key",
        "private_key",
        "raw_file",
        "file_bytes",
        "document_bytes",
    }
)

Handler = Callable[[AutomationOutboxEvent], None]
HANDLERS: dict[str, Handler] = {}


def register_handler(event_type: str) -> Callable[[Handler], Handler]:
    """Register exactly one safe-to-retry handler for a named event."""

    def decorate(handler: Handler) -> Handler:
        prior = HANDLERS.get(event_type)
        if prior is not None and prior is not handler:
            raise RuntimeError(f"An automation handler already owns {event_type}.")
        HANDLERS[event_type] = handler
        return handler

    return decorate


def _json_value(value: Any, *, path: str = "payload") -> Any:
    """Return a JSON-safe value while refusing secrets and binary documents."""

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key.lower() in _SENSITIVE_PAYLOAD_KEYS:
                raise ValidationError(
                    _("Automation payloads cannot contain credentials or raw documents."),
                    code="automation_payload_sensitive",
                )
            result[key] = _json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, list | tuple):
        return [_json_value(item, path=path) for item in value]
    if isinstance(value, bytes | bytearray | memoryview):
        raise ValidationError(
            _("Automation payloads cannot contain raw documents."),
            code="automation_payload_binary",
        )
    if isinstance(value, str | int | bool | type(None)):
        return value
    raise ValidationError(
        _("Automation payload contains an unsupported value."),
        code="automation_payload_invalid",
    )


def _payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _assert_branch_scope(*, organization: Any, branch: Any | None) -> None:
    if branch is not None and branch.organization_id != organization.pk:
        raise ValidationError(
            _("The branch must belong to the automation event's organization."),
            code="automation_branch_organization_mismatch",
        )


def enqueue_event(
    *,
    organization: Any,
    event_type: str,
    idempotency_key: str,
    payload: dict[str, Any],
    branch: Any | None = None,
    source: Any | None = None,
) -> AutomationOutboxEvent:
    """
    Put a message in the same transaction as its source change.

    Repeating the same call returns the original event.  Reusing an
    idempotency key for *different* content is refused because that is a
    programming or upstream-source error, not a retry.
    """

    cleaned_type = event_type.strip()
    cleaned_key = idempotency_key.strip()
    if not cleaned_type or not cleaned_key:
        raise ValidationError(
            _("An automation event type and idempotency key are required."),
            code="automation_event_identity_required",
        )
    _assert_branch_scope(organization=organization, branch=branch)
    safe_payload = _json_value(payload)
    if not isinstance(safe_payload, dict):  # pragma: no cover - typed input keeps this true
        raise ValidationError(_("Automation payload must be an object."))
    digest = _payload_hash(safe_payload)
    target_type = ""
    target_id = ""
    if source is not None:
        target_type = f"{source._meta.app_label}.{source._meta.object_name}"
        target_id = str(source.pk)

    # An IntegrityError must be isolated in a savepoint so the caller's
    # surrounding business transaction remains usable when another worker or
    # request wins the idempotency race.
    try:
        with transaction.atomic():
            event = AutomationOutboxEvent.objects.create(
                organization=organization,
                branch=branch,
                event_type=cleaned_type,
                idempotency_key=cleaned_key,
                payload=safe_payload,
                payload_hash=digest,
                correlation_id=get_correlation_id(),
                source_type=target_type,
                source_id=target_id,
                created_by=get_actor(),
            )
    except IntegrityError:
        event = AutomationOutboxEvent.objects.select_for_update().get(
            organization=organization,
            event_type=cleaned_type,
            idempotency_key=cleaned_key,
        )
        if event.payload_hash != digest:
            raise ValidationError(
                _("The automation idempotency key was reused with different content."),
                code="automation_event_idempotency_conflict",
            ) from None
        return event

    record_audit_event(
        action=AuditAction.CREATED,
        target=event,
        branch=branch,
        organization=organization,
        new_state=snapshot(event),
        metadata={"automation_event_type": cleaned_type},
    )
    return event


def _error_text(error: Exception) -> str:
    """Bound the persisted worker error without logging a traceback payload."""

    return f"{type(error).__name__}: {str(error)}"[:MAX_STORED_ERROR_LENGTH]


def claim_next_event(
    *, worker_id: uuid.UUID, now: datetime.datetime | None = None
) -> AutomationOutboxEvent | None:
    """Claim one due message with PostgreSQL row locking; no two workers win."""

    current = now or timezone.now()
    with transaction.atomic():
        event = (
            AutomationOutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(
                status__in=[OutboxEventStatus.PENDING, OutboxEventStatus.RETRY],
                available_at__lte=current,
            )
            .order_by("available_at", "id")
            .first()
        )
        if event is None:
            return None
        event.status = OutboxEventStatus.PROCESSING
        event.claimed_by = worker_id
        event.claimed_at = current
        event.attempt_count += 1
        event.save(
            update_fields=["status", "claimed_by", "claimed_at", "attempt_count", "updated_at"]
        )
        AutomationOutboxAttempt.objects.create(
            event=event,
            attempt_number=event.attempt_count,
            worker_id=worker_id,
            status=OutboxAttemptStatus.PROCESSING,
            started_at=current,
        )
        return event


def _finish_success(*, event_id: int, worker_id: uuid.UUID, duration_ms: int) -> None:
    with transaction.atomic():
        event = AutomationOutboxEvent.objects.select_for_update().get(pk=event_id)
        if event.status != OutboxEventStatus.PROCESSING or event.claimed_by != worker_id:
            return
        now = timezone.now()
        attempt = AutomationOutboxAttempt.objects.select_for_update().get(
            event=event, attempt_number=event.attempt_count
        )
        event.status = OutboxEventStatus.COMPLETED
        event.completed_at = now
        event.last_error = ""
        event.save(update_fields=["status", "completed_at", "last_error", "updated_at"])
        attempt.status = OutboxAttemptStatus.SUCCEEDED
        attempt.finished_at = now
        attempt.duration_ms = duration_ms
        attempt.save(update_fields=["status", "finished_at", "duration_ms", "updated_at"])
        record_audit_event(
            action=AuditAction.POSTED,
            target=event,
            branch=event.branch,
            organization=event.organization,
            metadata={"automation_event_type": event.event_type, "attempt": event.attempt_count},
        )


def _finish_failure(
    *, event_id: int, worker_id: uuid.UUID, duration_ms: int, error: Exception
) -> AutomationOutboxEvent | None:
    with transaction.atomic():
        event = AutomationOutboxEvent.objects.select_for_update().get(pk=event_id)
        if event.status != OutboxEventStatus.PROCESSING or event.claimed_by != worker_id:
            return None
        now = timezone.now()
        message = _error_text(error)
        terminal = event.attempt_count >= MAX_ATTEMPTS
        event.status = OutboxEventStatus.DEAD_LETTER if terminal else OutboxEventStatus.RETRY
        event.last_error = message
        event.claimed_by = None
        event.claimed_at = None
        if not terminal:
            delay = RETRY_BASE_SECONDS * (2 ** max(event.attempt_count - 1, 0))
            event.available_at = now + datetime.timedelta(seconds=delay)
        event.save(
            update_fields=[
                "status",
                "last_error",
                "claimed_by",
                "claimed_at",
                "available_at",
                "updated_at",
            ]
        )
        attempt = AutomationOutboxAttempt.objects.select_for_update().get(
            event=event, attempt_number=event.attempt_count
        )
        attempt.status = OutboxAttemptStatus.FAILED
        attempt.finished_at = now
        attempt.duration_ms = duration_ms
        attempt.error = message
        attempt.save(update_fields=["status", "finished_at", "duration_ms", "error", "updated_at"])
        record_audit_event(
            action=AuditAction.POSTING_FAILED,
            target=event,
            branch=event.branch,
            organization=event.organization,
            reason=message,
            metadata={
                "automation_event_type": event.event_type,
                "attempt": event.attempt_count,
                "dead_letter": terminal,
            },
        )
        return event


def process_claimed_event(*, event: AutomationOutboxEvent, worker_id: uuid.UUID) -> bool:
    """Run an already claimed message and mark its attempt exactly once."""

    started = time.perf_counter()
    try:
        handler = HANDLERS.get(event.event_type)
        if handler is None:
            raise LookupError(f"No handler is registered for {event.event_type}.")
        with audit_context(actor=None, correlation_id=event.correlation_id):
            handler(event)
    except Exception as error:  # handlers must be isolated from the worker loop
        duration_ms = int((time.perf_counter() - started) * 1000)
        failed = _finish_failure(
            event_id=event.pk, worker_id=worker_id, duration_ms=duration_ms, error=error
        )
        logger.warning(
            "automation_outbox_handler_failed",
            extra={
                "event_id": event.pk,
                "event_type": event.event_type,
                "organization_id": event.organization_id,
                "correlation_id": str(event.correlation_id),
                "dead_letter": bool(failed and failed.status == OutboxEventStatus.DEAD_LETTER),
            },
        )
        return False
    duration_ms = int((time.perf_counter() - started) * 1000)
    _finish_success(event_id=event.pk, worker_id=worker_id, duration_ms=duration_ms)
    logger.info(
        "automation_outbox_handler_completed",
        extra={
            "event_id": event.pk,
            "event_type": event.event_type,
            "organization_id": event.organization_id,
            "correlation_id": str(event.correlation_id),
            "duration_ms": duration_ms,
        },
    )
    return True


def requeue_stale_claims(
    *, now: datetime.datetime | None = None, stale_after: datetime.timedelta = STALE_CLAIM_AFTER
) -> int:
    """Recover messages claimed by a worker that stopped before finishing."""

    current = now or timezone.now()
    threshold = current - stale_after
    recovered = 0
    with transaction.atomic():
        stale = (
            AutomationOutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(status=OutboxEventStatus.PROCESSING, claimed_at__lt=threshold)
            .order_by("claimed_at", "id")
        )
        for event in stale:
            attempt = (
                AutomationOutboxAttempt.objects.select_for_update()
                .filter(event=event, attempt_number=event.attempt_count)
                .first()
            )
            if attempt is not None and attempt.status == OutboxAttemptStatus.PROCESSING:
                attempt.status = OutboxAttemptStatus.ABANDONED
                attempt.finished_at = current
                attempt.error = "Worker claim expired before completion."
                attempt.save(update_fields=["status", "finished_at", "error", "updated_at"])
            event.status = OutboxEventStatus.RETRY
            event.claimed_by = None
            event.claimed_at = None
            event.available_at = current
            event.last_error = "Worker claim expired before completion."
            event.save(
                update_fields=[
                    "status",
                    "claimed_by",
                    "claimed_at",
                    "available_at",
                    "last_error",
                    "updated_at",
                ]
            )
            recovered += 1
    return recovered


def process_due_events(*, worker_id: uuid.UUID | None = None, limit: int = 100) -> dict[str, int]:
    """Process a bounded batch. Intended for a dedicated worker or scheduler."""

    if limit < 1:
        raise ValueError("limit must be positive")
    identity = worker_id or uuid.uuid4()
    recovered = requeue_stale_claims()
    processed = succeeded = failed = 0
    while processed < limit:
        event = claim_next_event(worker_id=identity)
        if event is None:
            break
        processed += 1
        if process_claimed_event(event=event, worker_id=identity):
            succeeded += 1
        else:
            failed += 1
    return {
        "recovered": recovered,
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
    }


def replay_dead_letter(*, event: AutomationOutboxEvent, actor: Any) -> AutomationOutboxEvent:
    """Put a dead-letter message back in the queue after an authorized review."""

    require_organization_permission(actor, "core.replay_automation_outbox", event.organization)
    with transaction.atomic():
        locked = AutomationOutboxEvent.objects.select_for_update().get(pk=event.pk)
        if locked.status != OutboxEventStatus.DEAD_LETTER:
            raise ValidationError(
                _("Only a dead-letter automation message can be replayed."),
                code="automation_event_not_dead_letter",
            )
        previous = snapshot(locked)
        locked.status = OutboxEventStatus.PENDING
        locked.available_at = timezone.now()
        locked.claimed_by = None
        locked.claimed_at = None
        locked.completed_at = None
        locked.last_error = ""
        locked.save(
            update_fields=[
                "status",
                "available_at",
                "claimed_by",
                "claimed_at",
                "completed_at",
                "last_error",
                "updated_at",
            ]
        )
        record_audit_event(
            action=AuditAction.SUBMITTED,
            target=locked,
            previous_state=previous,
            new_state=snapshot(locked),
            branch=locked.branch,
            organization=locked.organization,
            reason="Authorized dead-letter replay.",
        )
        return locked


def outbox_metrics(
    *, organization: Any | None = None, organizations: Any | None = None
) -> dict[str, Any]:
    """Small, source-free monitoring projection for dashboards and logs."""

    if organization is not None and organizations is not None:
        raise ValueError("Pass one organization or an organization queryset, not both.")
    rows = AutomationOutboxEvent.objects.all()
    if organization is not None:
        rows = rows.filter(organization=organization)
    elif organizations is not None:
        rows = rows.filter(organization__in=organizations)
    now = timezone.now()
    counts = dict(rows.values("status").annotate(total=Count("id")).values_list("status", "total"))
    pending = rows.filter(status__in=[OutboxEventStatus.PENDING, OutboxEventStatus.RETRY])
    oldest = pending.aggregate(value=Min("created_at"))["value"]
    completed = AutomationOutboxAttempt.objects.filter(
        status=OutboxAttemptStatus.SUCCEEDED, duration_ms__isnull=False
    )
    if organization is not None:
        completed = completed.filter(event__organization=organization)
    elif organizations is not None:
        completed = completed.filter(event__organization__in=organizations)
    avg_ms = completed.aggregate(value=Avg("duration_ms"))["value"]
    return {
        "pending": counts.get(OutboxEventStatus.PENDING, 0)
        + counts.get(OutboxEventStatus.RETRY, 0),
        "processing": counts.get(OutboxEventStatus.PROCESSING, 0),
        "dead_letters": counts.get(OutboxEventStatus.DEAD_LETTER, 0),
        "completed": counts.get(OutboxEventStatus.COMPLETED, 0),
        "oldest_pending_seconds": int((now - oldest).total_seconds()) if oldest else None,
        "average_processing_ms": int(avg_ms) if avg_ms is not None else None,
        "retry_count": rows.filter(status=OutboxEventStatus.RETRY).count(),
    }


def _scope_key(branch: Any | None) -> str:
    return f"branch:{branch.pk}" if branch is not None else "organization"


def _task_dedupe_key(*, exception: AutomationException) -> str:
    return f"exception:{exception.pk}"


def _assert_owner_scope(*, organization: Any, branch: Any | None, owner_user: Any | None) -> None:
    if owner_user is None:
        return
    allowed = (
        has_branch_permission(owner_user, "core.view_automation_task", branch)
        if branch is not None
        else has_organization_master_data_permission(
            owner_user, "core.view_automation_task", organization
        )
    )
    if not allowed:
        raise ValidationError(
            _("The task owner is outside the organization or branch task scope."),
            code="automation_task_owner_out_of_scope",
        )


def open_exception(
    *,
    organization: Any,
    branch: Any | None,
    code: str,
    target: Any,
    severity: str,
    is_blocking: bool,
    sensitivity: str = AutomationDataSensitivity.OPERATIONAL,
    amount: Decimal | None = None,
    owner_role: str = "",
    owner_user: Any | None = None,
    details: dict[str, Any] | None = None,
    source_event: AutomationOutboxEvent | None = None,
    title: str = "",
    summary: str = "",
    due_at: datetime.datetime | None = None,
) -> AutomationException:
    """Create or refresh one unresolved condition and its single active task."""

    _assert_branch_scope(organization=organization, branch=branch)
    _assert_owner_scope(organization=organization, branch=branch, owner_user=owner_user)
    cleaned_code = code.strip()
    target_type = f"{target._meta.app_label}.{target._meta.object_name}"
    target_id = str(target.pk)
    target_organization_id = getattr(target, "organization_id", None)
    target_branch_id = getattr(target, "branch_id", None)
    if target_organization_id is not None and target_organization_id != organization.pk:
        raise ValidationError(
            _("The exception target must belong to the same organization."),
            code="automation_exception_target_organization_mismatch",
        )
    if branch is not None and target_branch_id is not None and target_branch_id != branch.pk:
        raise ValidationError(
            _("The exception target must belong to the same branch."),
            code="automation_exception_target_branch_mismatch",
        )
    safe_details = _json_value(details or {})
    if not isinstance(safe_details, dict):  # pragma: no cover - literal dict input
        raise ValidationError(_("Exception details must be an object."))
    if severity not in AutomationSeverity.values:
        raise ValidationError(_("Unknown automation severity."), code="automation_severity_invalid")
    if sensitivity not in AutomationDataSensitivity.values:
        raise ValidationError(
            _("Unknown automation data sensitivity."), code="automation_sensitivity_invalid"
        )
    if not cleaned_code:
        raise ValidationError(
            _("An exception code is required."), code="automation_exception_code_required"
        )

    now = timezone.now()
    with transaction.atomic():
        exception = (
            AutomationException.objects.select_for_update()
            .filter(
                organization=organization,
                scope_key=_scope_key(branch),
                code=cleaned_code,
                target_type=target_type,
                target_id=target_id,
                status=AutomationExceptionStatus.OPEN,
            )
            .first()
        )
        created_exception = exception is None
        if created_exception:
            # The partial unique index is the final dedupe authority.  A
            # second detector can still observe no row before the first
            # commits, so recover its insert race rather than sending the
            # entire source event to retry merely because the same condition
            # was found at the same instant.
            try:
                with transaction.atomic():
                    exception = AutomationException.objects.create(
                        organization=organization,
                        branch=branch,
                        scope_key=_scope_key(branch),
                        code=cleaned_code,
                        target_type=target_type,
                        target_id=target_id,
                        amount=amount,
                        severity=severity,
                        is_blocking=is_blocking,
                        sensitivity=sensitivity,
                        owner_role=owner_role,
                        owner_user=owner_user,
                        source_event=source_event,
                        details=safe_details,
                        first_detected_at=now,
                        last_detected_at=now,
                    )
            except IntegrityError:
                exception = AutomationException.objects.select_for_update().get(
                    organization=organization,
                    scope_key=_scope_key(branch),
                    code=cleaned_code,
                    target_type=target_type,
                    target_id=target_id,
                    status=AutomationExceptionStatus.OPEN,
                )
                created_exception = False
        if exception is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("Automation exception creation returned no record.")
        if created_exception:
            record_audit_event(
                action=AuditAction.CREATED,
                target=exception,
                branch=branch,
                organization=organization,
                new_state=snapshot(exception),
            )
        else:
            exception.last_detected_at = now
            exception.seen_count += 1
            exception.amount = amount
            exception.severity = severity
            exception.is_blocking = is_blocking
            exception.sensitivity = sensitivity
            exception.owner_role = owner_role
            exception.owner_user = owner_user
            exception.source_event = source_event or exception.source_event
            exception.details = safe_details
            exception.save(
                update_fields=[
                    "last_detected_at",
                    "seen_count",
                    "amount",
                    "severity",
                    "is_blocking",
                    "sensitivity",
                    "owner_role",
                    "owner_user",
                    "source_event",
                    "details",
                    "updated_at",
                ]
            )

        task = (
            AutomationTask.objects.select_for_update()
            .filter(
                organization=organization,
                deduplication_key=_task_dedupe_key(exception=exception),
                status__in=[AutomationTaskStatus.OPEN, AutomationTaskStatus.ACKNOWLEDGED],
            )
            .first()
        )
        created = task is None
        if created:
            try:
                with transaction.atomic():
                    task = AutomationTask.objects.create(
                        organization=organization,
                        branch=branch,
                        exception=exception,
                        source_event=source_event,
                        task_type=cleaned_code,
                        deduplication_key=_task_dedupe_key(exception=exception),
                        target_type=target_type,
                        target_id=target_id,
                        severity=severity,
                        sensitivity=sensitivity,
                        title=title or cleaned_code,
                        summary=summary,
                        payload={"exception_id": exception.pk, "code": cleaned_code},
                        assignee_role=owner_role,
                        assignee_user=owner_user,
                        due_at=due_at,
                    )
            except IntegrityError:
                task = AutomationTask.objects.select_for_update().get(
                    organization=organization,
                    deduplication_key=_task_dedupe_key(exception=exception),
                    status__in=[AutomationTaskStatus.OPEN, AutomationTaskStatus.ACKNOWLEDGED],
                )
                created = False
        # A task can be acknowledged but the exceptional condition can worsen;
        # preserve the acknowledgement while refreshing the non-decision facts.
        if not created:
            if task is None:  # pragma: no cover - defensive invariant
                raise RuntimeError("Automation task lookup returned no record.")
            task.source_event = source_event or task.source_event
            task.severity = severity
            task.sensitivity = sensitivity
            task.title = title or task.title
            task.summary = summary
            task.assignee_role = owner_role
            task.assignee_user = owner_user
            task.due_at = due_at
            task.payload = {"exception_id": exception.pk, "code": cleaned_code}
            task.save(
                update_fields=[
                    "source_event",
                    "severity",
                    "sensitivity",
                    "title",
                    "summary",
                    "assignee_role",
                    "assignee_user",
                    "due_at",
                    "payload",
                    "updated_at",
                ]
            )
        return exception


def resolve_exception(
    *,
    organization: Any,
    branch: Any | None,
    code: str,
    target: Any,
    resolution: str,
) -> bool:
    """Resolve the persistent condition and its task when the source is clean."""

    _assert_branch_scope(organization=organization, branch=branch)
    target_type = f"{target._meta.app_label}.{target._meta.object_name}"
    target_id = str(target.pk)
    with transaction.atomic():
        exception = (
            AutomationException.objects.select_for_update()
            .filter(
                organization=organization,
                scope_key=_scope_key(branch),
                code=code,
                target_type=target_type,
                target_id=target_id,
                status=AutomationExceptionStatus.OPEN,
            )
            .first()
        )
        if exception is None:
            return False
        now = timezone.now()
        previous = snapshot(exception)
        exception.status = AutomationExceptionStatus.RESOLVED
        exception.resolved_at = now
        exception.resolution = resolution
        exception.save(update_fields=["status", "resolved_at", "resolution", "updated_at"])
        AutomationTask.objects.filter(
            exception=exception,
            status__in=[AutomationTaskStatus.OPEN, AutomationTaskStatus.ACKNOWLEDGED],
        ).update(
            status=AutomationTaskStatus.RESOLVED,
            resolved_at=now,
            resolution=resolution,
            updated_at=now,
        )
        record_audit_event(
            action=AuditAction.APPROVED,
            target=exception,
            previous_state=previous,
            new_state=snapshot(exception),
            branch=branch,
            organization=organization,
            reason=resolution,
        )
        return True


def _task_roles_for(actor: Any, task: AutomationTask) -> set[str]:
    if task.branch is not None:
        return roles_at_branch(actor, task.branch)
    return roles_in_organization(actor, task.organization)


def tasks_for_actor(*, actor: Any, include_resolved: bool = False) -> QuerySet[AutomationTask]:
    """Tenant- and role-scoped task queryset; never use a bare Task.objects list."""

    if not actor.is_authenticated or not actor.is_active:
        return AutomationTask.objects.none()
    base = AutomationTask.objects.select_related(
        "organization", "branch", "exception", "assignee_user"
    )
    if not include_resolved:
        base = base.filter(
            status__in=[AutomationTaskStatus.OPEN, AutomationTaskStatus.ACKNOWLEDGED]
        )
    if actor.is_superuser:
        return base

    # The queryset is built from the actor's reachable scope first.  Role
    # matching happens in Python only after this query remains tenant-bound;
    # task counts are small and a generic JSON role condition would be less
    # portable and harder to audit than the explicit role predicate below.
    branches = branches_with_permission(actor, "core.view_automation_task")
    organizations = organizations_with_permission(actor, "core.view_automation_task")
    candidate = base.filter(
        Q(branch__in=branches) | Q(branch__isnull=True, organization__in=organizations)
    )
    visible_ids: list[int] = []
    for task in candidate:
        roles = _task_roles_for(actor, task)
        if task.sensitivity == AutomationDataSensitivity.HR_RESTRICTED and not (
            roles & {Role.OWNER, Role.ACCOUNTING_MANAGER}
        ):
            continue
        if (
            Role.OWNER in roles
            or task.assignee_user_id == actor.pk
            or not task.assignee_role
            or task.assignee_role in roles
        ):
            visible_ids.append(task.pk)
    return base.filter(pk__in=visible_ids)


def acknowledge_task(*, task: AutomationTask, actor: Any) -> AutomationTask:
    """Record responsibility without allowing a task to waive its exception."""

    if not tasks_for_actor(actor=actor).filter(pk=task.pk).exists():
        raise PermissionDenied(_("You may not acknowledge this automation task."))
    if task.branch is not None:
        if not has_branch_permission(actor, "core.acknowledge_automation_task", task.branch):
            raise PermissionDenied(_("You may not acknowledge this automation task."))
    elif not has_organization_master_data_permission(
        actor, "core.acknowledge_automation_task", task.organization
    ):
        raise PermissionDenied(_("You may not acknowledge this automation task."))
    with transaction.atomic():
        locked = AutomationTask.objects.select_for_update().get(pk=task.pk)
        if locked.status == AutomationTaskStatus.ACKNOWLEDGED:
            return locked
        if locked.status != AutomationTaskStatus.OPEN:
            raise ValidationError(_("Only an open task can be acknowledged."), code="task_not_open")
        previous = snapshot(locked)
        locked.status = AutomationTaskStatus.ACKNOWLEDGED
        locked.acknowledged_by = actor
        locked.acknowledged_at = timezone.now()
        locked.save(update_fields=["status", "acknowledged_by", "acknowledged_at", "updated_at"])
        record_audit_event(
            action=AuditAction.SUBMITTED,
            target=locked,
            previous_state=previous,
            new_state=snapshot(locked),
            branch=locked.branch,
            organization=locked.organization,
        )
        return locked


__all__ = [
    "HANDLERS",
    "MAX_ATTEMPTS",
    "acknowledge_task",
    "claim_next_event",
    "enqueue_event",
    "open_exception",
    "outbox_metrics",
    "process_due_events",
    "process_claimed_event",
    "register_handler",
    "replay_dead_letter",
    "resolve_exception",
    "tasks_for_actor",
]
