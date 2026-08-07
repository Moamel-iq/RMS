"""
Health endpoint contract.

This endpoint proves that URL routing, Django Ninja, typed schemas, settings,
and the PostgreSQL connection are wired together. It is also the first thing
an operator looks at, so its shape must stay stable and its body must stay
free of configuration detail.
"""

import pytest
from django.conf import settings
from django.db import DatabaseError
from django.test import Client

HEALTH_URL = "/api/v1/health"

EXPECTED_KEYS = {"status", "version", "database"}


@pytest.mark.django_db
class TestHealthEndpoint:
    def test_returns_200_when_database_is_reachable(self, client: Client) -> None:
        response = client.get(HEALTH_URL)
        assert response.status_code == 200

    def test_response_schema_is_stable(self, client: Client) -> None:
        payload = client.get(HEALTH_URL).json()
        assert set(payload) == EXPECTED_KEYS

    def test_reports_healthy_status(self, client: Client) -> None:
        payload = client.get(HEALTH_URL).json()
        assert payload["status"] == "ok"
        assert payload["database"] == "up"

    def test_reports_application_version(self, client: Client) -> None:
        from config import __version__

        assert client.get(HEALTH_URL).json()["version"] == __version__

    def test_body_leaks_no_configuration_detail(self, client: Client) -> None:
        """The payload must never carry secrets, credentials, or host detail."""
        body = client.get(HEALTH_URL).content.decode().lower()

        forbidden = [
            settings.SECRET_KEY,
            settings.DATABASES["default"]["PASSWORD"],
            settings.DATABASES["default"]["USER"],
            settings.DATABASES["default"]["NAME"],
            settings.DATABASES["default"]["HOST"],
            str(settings.BASE_DIR),
        ]
        for value in forbidden:
            if value:
                assert value.lower() not in body

    def test_no_traceback_is_exposed(self, client: Client) -> None:
        body = client.get(HEALTH_URL).content.decode().lower()
        assert "traceback" not in body
        assert "django.db" not in body


@pytest.mark.django_db
def test_health_requires_no_authentication(client: Client) -> None:
    """An unauthenticated probe must still work; it is called by infrastructure."""
    assert client.get(HEALTH_URL).status_code == 200


class TestHealthEndpointWhenDatabaseIsDown:
    """
    The failure path.

    A driver error message names the host, port, and role — exactly the detail
    that must never reach an unauthenticated endpoint.
    """

    LEAKY_MESSAGE = (
        'connection to server at "10.20.30.40", port 5432 failed: '
        'password authentication failed for user "khan_mandi_dev"'
    )

    @pytest.fixture
    def broken_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raise_database_error(*args: object, **kwargs: object) -> None:
            raise DatabaseError(self.LEAKY_MESSAGE)

        monkeypatch.setattr("config.api.connection.cursor", raise_database_error)

    def test_returns_503(self, client: Client, broken_connection: None) -> None:
        assert client.get(HEALTH_URL).status_code == 503

    def test_reports_degraded_status(self, client: Client, broken_connection: None) -> None:
        payload = client.get(HEALTH_URL).json()
        assert payload["status"] == "degraded"
        assert payload["database"] == "down"

    def test_schema_is_unchanged_on_failure(self, client: Client, broken_connection: None) -> None:
        """Operators parse this payload. Its shape must not change under failure."""
        assert set(client.get(HEALTH_URL).json()) == EXPECTED_KEYS

    def test_driver_error_detail_is_not_leaked(
        self, client: Client, broken_connection: None
    ) -> None:
        body = client.get(HEALTH_URL).content.decode()
        assert "password authentication" not in body
        assert "10.20.30.40" not in body
        assert "khan_mandi_dev" not in body
