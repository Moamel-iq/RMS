"""
The one way a procurement service is allowed to learn a document's status.

This module exists because the same defect appeared three times in three
tasks. A service was handed a model instance and asked it what state it was
in — and the instance had been loaded before somebody else changed it. Twice
the consequence was harmless; once it was not: `award_quotation` would have
awarded an offer that had already been declined, because the caller's copy
still said `SUBMITTED`.

The rule is therefore stated once, here, and imported rather than remembered:

    **A lifecycle transition is authorized by the database row, never by the
    object the caller passed in.**

`lock_and_require_status` does exactly two things — take the row lock and
check the persisted status — and deliberately no more. Domain validation
stays in the service that owns it: whether the request has lines, whether the
approver is the submitter, whether the quotation has expired. Folding those in
would produce a helper that every caller has to read the source of before
trusting, which is worse than the duplication it saved.

The returned row is the one to mutate. Writing back to the caller's instance
would persist whatever else was stale on it.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


def lock_and_require_status[T: models.Model](
    model: type[T],
    pk: int,
    allowed: set[str] | frozenset[str] | tuple[str, ...],
    *,
    code: str,
    message: Any = None,
) -> T:
    """
    Take a row lock, confirm the persisted status, and return the locked row.

    `allowed` is the set of statuses from which the caller's transition is
    legal. `code` is the domain error code the caller wants raised — the
    services use `request_not_editable`, `illegal_transition`,
    `order_not_editable` and so on, and keeping that choice with the caller is
    what stops this helper becoming a place where error vocabulary is decided
    for domains it knows nothing about.

    Raises `model.DoesNotExist` if the row is gone. That is deliberate: a
    document deleted underneath a transition is not a validation failure, and
    swallowing it into a `ValidationError` would hide a genuinely broken
    caller.
    """
    # `_default_manager` rather than `objects`: the bound is `models.Model`,
    # which does not declare a manager attribute, and this is the accessor
    # Django itself uses for exactly that reason.
    locked: T = model._default_manager.select_for_update().get(pk=pk)
    status: str = locked.status  # type: ignore[attr-defined]
    if status in allowed:
        return locked

    raise ValidationError(
        message
        or _("%(document)s is %(status)s and cannot change from there.")
        % {"document": str(locked), "status": status},
        code=code,
    )
