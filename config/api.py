"""
Root Django Ninja API.

Versioned under /api/v1/. Routers from the foundation apps are registered here
as their tasks deliver them.

Errors are translated once, here, so every router reports the same shapes:
403 for insufficient authority, 404 for a record that does not exist, 409 for
a state conflict, 422 for anything the caller could fix by sending different
input.
"""

from typing import Any

from django.core.exceptions import (
    ObjectDoesNotExist,
    PermissionDenied,
    ValidationError,
)
from django.db import DatabaseError, connection
from django.http import HttpRequest
from ninja import NinjaAPI, Schema, Status
from ninja.security import django_auth

from apps.accounting.api import router as accounting_router
from apps.inventory.api import router as inventory_router
from config import __version__

api = NinjaAPI(
    title="Khan Mandi RMS API",
    version="1.0.0",
    description="Khan Mandi Restaurant Management System — internal API.",
    urls_namespace="api_v1",
    # Authenticated by default, so a new endpoint is private unless its author
    # says otherwise. The opposite default eventually ships something open.
    #
    # `django_auth` is a cookie-backed scheme, and django-ninja's
    # `APIKeyCookie` runs Django's CSRF check for it by default — there is no
    # separate `csrf=` argument on NinjaAPI to set, and adding one back would
    # be a TypeError rather than extra safety.
    auth=django_auth,
)

api.add_router("", accounting_router)
api.add_router("/inventory", inventory_router)


#: Domain errors that describe a state conflict rather than a bad request.
#: "You already reversed this" is not the caller malforming input; it is the
#: world having moved on, and a client retrying a 422 forever would be right
#: to be confused by it.
CONFLICT_CODES = frozenset(
    {
        "already_reversed",
        "cannot_reverse_a_reversal",
        "idempotency_key_conflict",
        "not_a_draft",
        "source_event_already_posted",
    }
)


class ErrorSchema(Schema):
    """A domain error a client can branch on without parsing prose."""

    code: str
    message: str


def _error_code(exc: ValidationError) -> str:
    """
    The domain code from a Django ValidationError, however it was raised.

    `raise ValidationError(msg, code="x")` puts it on the exception; a
    `full_clean()` failure buries it one or two levels down instead. A caller
    should not have to care which happened.
    """
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    for error in getattr(exc, "error_list", []) or []:
        if getattr(error, "code", None):
            return str(error.code)
    for errors in (getattr(exc, "error_dict", None) or {}).values():
        for error in errors:
            if getattr(error, "code", None):
                return str(error.code)
    return "invalid"


@api.exception_handler(PermissionDenied)
def on_permission_denied(request: HttpRequest, exc: PermissionDenied) -> Any:
    """
    403 — reachable, but not permitted.

    The caller is inside the organization or branch and lacks the authority
    for this particular act: they may read this journal, they may not post it.
    Nothing is disclosed that they were not already entitled to see.

    Being outside the scope entirely is a different answer — `OutOfScope` is
    an `ObjectDoesNotExist` and lands on the 404 handler below.
    """
    return api.create_response(
        request,
        {"code": "forbidden", "message": str(exc) or "Not permitted."},
        status=403,
    )


@api.exception_handler(ValidationError)
def on_validation_error(request: HttpRequest, exc: ValidationError) -> Any:
    code = _error_code(exc)
    return api.create_response(
        request,
        {"code": code, "message": "; ".join(exc.messages)},
        status=409 if code in CONFLICT_CODES else 422,
    )


@api.exception_handler(ObjectDoesNotExist)
def on_missing(request: HttpRequest, exc: ObjectDoesNotExist) -> Any:
    """
    404 — it does not exist, as far as this caller is concerned.

    Covers both a genuinely absent row and one outside the caller's tenancy,
    deliberately with the same code and the same wording. Answering 403 for
    the second confirms the record is real, which turns an id-guessing loop
    into a census of another organization's documents and invoice numbers.
    """
    return api.create_response(request, {"code": "not_found", "message": str(exc)}, status=404)


class HealthSchema(Schema):
    """Health payload. Deliberately carries no configuration detail."""

    status: str
    version: str
    database: str


def _database_is_reachable() -> bool:
    """Issue the cheapest possible round trip to the database."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        # The exception text can name the host, user, and port. Never surface it.
        return False
    return True


@api.get(
    "/health",
    response={200: HealthSchema, 503: HealthSchema},
    auth=None,
    tags=["system"],
    summary="Liveness and database readiness probe",
)
def health(request: HttpRequest) -> Status[dict[str, str]]:
    """
    Report application and database readiness.

    Returns 200 when both are healthy and 503 when the database is unreachable,
    so a load balancer can act on it without parsing the body.
    """
    database_up = _database_is_reachable()
    payload = {
        "status": "ok" if database_up else "degraded",
        "version": __version__,
        "database": "up" if database_up else "down",
    }
    return Status(200 if database_up else 503, payload)
