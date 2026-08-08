"""
Ambient audit context: who is acting, and under which request.

Held in ContextVars rather than passed through every function signature,
because the actor and correlation id have to reach the bottom of a posting
call stack that has no business knowing about HTTP.

ContextVars are safe here where thread-locals would not be: they are isolated
per asyncio task as well as per thread, so a concurrent request cannot read
another's actor.

Set by `apps.core.middleware.AuditContextMiddleware` for web requests, and by
`audit_context()` explicitly for management commands, imports, and tests. Work
that runs without either — a bare shell — records no actor, which is honest
rather than convenient.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.users.models import User

_correlation_id: ContextVar[uuid.UUID | None] = ContextVar("correlation_id", default=None)
_actor: ContextVar[User | None] = ContextVar("audit_actor", default=None)


def get_correlation_id() -> uuid.UUID:
    """
    The current correlation id, generating one if nothing set it.

    Never returns None: every audit event must be attributable to some unit of
    work, even one started from a shell. A generated id groups that work
    together rather than leaving the column empty.
    """
    current = _correlation_id.get()
    if current is None:
        current = uuid.uuid4()
        _correlation_id.set(current)
    return current


def get_actor() -> User | None:
    """
    The acting user, or None for system work.

    None is a real answer — a scheduled closing job has no user — and must not
    be faked with a placeholder account, which would make the audit trail lie
    about who did something.
    """
    return _actor.get()


@contextmanager
def audit_context(
    *,
    actor: User | None = None,
    correlation_id: uuid.UUID | None = None,
) -> Iterator[uuid.UUID]:
    """
    Run a block with a known actor and correlation id.

    Restores the previous values on exit, including when the block raises, so
    a failed import cannot leak its actor into whatever runs next.
    """
    resolved = correlation_id or uuid.uuid4()
    correlation_token = _correlation_id.set(resolved)
    actor_token = _actor.set(actor)
    try:
        yield resolved
    finally:
        _correlation_id.reset(correlation_token)
        _actor.reset(actor_token)
