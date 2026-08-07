"""
Root Django Ninja API.

Versioned under /api/v1/. Routers from the foundation apps are registered here
as their tasks deliver them.
"""

from django.db import DatabaseError, connection
from django.http import HttpRequest
from ninja import NinjaAPI, Schema, Status

from config import __version__

api = NinjaAPI(
    title="Khan Mandi RMS API",
    version="1.0.0",
    description="Khan Mandi Restaurant Management System — internal API.",
    urls_namespace="api_v1",
)


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
