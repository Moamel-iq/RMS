"""Audit trail queries. Reads only."""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import QuerySet

from apps.core.models import AuditEvent

#: The audit log is unbounded and grows forever, so it is always paginated.
AUDIT_PAGE_SIZE = 50


def audit_events() -> QuerySet[AuditEvent]:
    """Every recorded event, newest first."""
    return AuditEvent.objects.select_related("actor", "branch").all()


def audit_trail_for(instance: models.Model) -> QuerySet[AuditEvent]:
    """Every recorded action against one object, newest first."""
    target_type = f"{instance._meta.app_label}.{instance._meta.object_name}"
    return AuditEvent.objects.filter(
        target_type=target_type, target_id=str(instance.pk)
    ).select_related("actor", "branch")


def events_for_correlation(correlation_id: uuid.UUID | str) -> QuerySet[AuditEvent]:
    """
    Every event produced by one request or job, oldest first.

    The view an operator needs when asking "what did that import actually do".
    """
    return (
        AuditEvent.objects.filter(correlation_id=correlation_id)
        .select_related("actor", "branch")
        .order_by("occurred_at", "id")
    )


def events_for_actor(user_id: int) -> QuerySet[AuditEvent]:
    return AuditEvent.objects.filter(actor_id=user_id).select_related("branch")
