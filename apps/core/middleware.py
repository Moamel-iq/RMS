"""Audit context middleware."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from apps.core.context import _actor, _correlation_id

#: Accepted from the caller so a correlation id can span services later. The
#: value is validated as a UUID before use — an unvalidated header would let a
#: caller write arbitrary text into the audit trail.
CORRELATION_HEADER = "HTTP_X_CORRELATION_ID"

#: Echoed back so an operator reporting a problem can quote the id.
RESPONSE_HEADER = "X-Correlation-ID"


class AuditContextMiddleware:
    """
    Bind the acting user and a correlation id to the request's context.

    Must run AFTER AuthenticationMiddleware, or `request.user` is not yet
    resolved and every event would record no actor.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        correlation_id = self._correlation_id_for(request)
        user = getattr(request, "user", None)
        actor = user if user is not None and user.is_authenticated else None

        correlation_token = _correlation_id.set(correlation_id)
        actor_token = _actor.set(actor)
        try:
            response = self.get_response(request)
        finally:
            # Reset even when the view raises, so a 500 cannot leave one
            # request's actor bound for the next one served by this worker.
            _correlation_id.reset(correlation_token)
            _actor.reset(actor_token)

        response[RESPONSE_HEADER] = str(correlation_id)
        return response

    @staticmethod
    def _correlation_id_for(request: HttpRequest) -> uuid.UUID:
        supplied = request.META.get(CORRELATION_HEADER)
        if supplied:
            try:
                return uuid.UUID(supplied)
            except ValueError, AttributeError, TypeError:
                # Malformed header: ignore it and mint our own rather than
                # rejecting the request over a diagnostic field.
                pass
        return uuid.uuid4()
