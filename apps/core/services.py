"""
Audit recording.

One way in. Every audited action calls `record_audit_event`, which fills the
actor and correlation id from the ambient context so a caller cannot forget
them or supply the wrong ones.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db import models

from apps.core.context import get_actor, get_correlation_id
from apps.core.models import AuditAction, AuditEvent

if TYPE_CHECKING:
    # Type-check only. A runtime import would make apps.core depend on
    # apps.organizations, and core must stay beneath every domain app.
    from apps.organizations.models import Branch, Organization

#: Fields never copied into an audit snapshot. A password hash in the audit log
#: would survive every rotation and defeat the point of rotating it.
NEVER_SNAPSHOT = frozenset({"password", "session_key", "secret", "token", "api_key"})


def _json_safe(value: Any) -> Any:
    """
    Render a value for JSON storage without losing exactness.

    Decimals become strings, never floats: json would serialise Decimal("0.1")
    through float and store 0.1000000000000000055511151231257827, which makes
    the audit trail disagree with the ledger it is auditing.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, models.Model):
        return str(value.pk)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    if isinstance(value, str | int | bool | type(None)):
        return value
    return str(value)


def snapshot(instance: models.Model, *, fields: list[str] | None = None) -> dict[str, Any]:
    """
    Capture a model instance as a JSON-safe dict.

    Concrete fields only — no reverse relations, which could pull an unbounded
    amount of data into a log row. Sensitive fields are dropped.
    """
    names = fields or [field.name for field in instance._meta.concrete_fields]
    return {
        name: _json_safe(getattr(instance, name, None))
        for name in names
        if name not in NEVER_SNAPSHOT
    }


def record_audit_event(
    *,
    action: AuditAction | str,
    target: models.Model | None = None,
    target_type: str = "",
    target_id: str = "",
    branch: Branch | None = None,
    organization: Organization | None = None,
    previous_state: dict[str, Any] | None = None,
    new_state: dict[str, Any] | None = None,
    reason: str = "",
    source_document_type: str = "",
    source_document_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """
    Append one event to the audit trail.

    Actor and correlation id come from the ambient context rather than from
    arguments, so a caller cannot attribute an action to the wrong user by
    passing the wrong value.

    The actor is also written as text, because a user renamed or deactivated
    years later must not change what the trail says about what they did.
    """
    if target is not None:
        target_type = target_type or f"{target._meta.app_label}.{target._meta.object_name}"
        target_id = target_id or str(target.pk)

    if not target_type:
        raise ValueError("record_audit_event requires either target or target_type")

    # The audit boundary must not depend on which module happened to call the
    # recorder.  Prefer an explicit organization; otherwise derive it from a
    # branch or a normal organization/branch relation on the target.
    if organization is None and branch is not None:
        organization = branch.organization
    if organization is None and target is not None:
        candidate = getattr(target, "organization", None)
        if candidate is not None:
            organization = candidate
        elif (target_branch := getattr(target, "branch", None)) is not None:
            organization = target_branch.organization
        elif target._meta.label_lower == "organizations.organization":
            organization = target  # type: ignore[assignment]

    actor = get_actor()

    return AuditEvent.objects.create(
        action=action,
        actor=actor,
        actor_label=str(actor) if actor is not None else "",
        branch=branch,
        organization=organization,
        correlation_id=get_correlation_id(),
        target_type=target_type,
        target_id=target_id,
        previous_state=_json_safe(previous_state) if previous_state is not None else None,
        new_state=_json_safe(new_state) if new_state is not None else None,
        reason=reason,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        metadata=_json_safe(metadata or {}),
    )
