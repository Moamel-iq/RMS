"""
Configuration invariants for Phase 0.

These are cheap tests guarding expensive mistakes: a SQLite fallback, a
timezone drift, or a production deployment that boots with a dev secret.
"""

import os
import subprocess
import sys
from pathlib import Path

import environ
import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


class TestDatabaseConfiguration:
    def test_engine_is_postgresql(self) -> None:
        """SQLite must never be reachable. The invariants need real constraints."""
        assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"

    def test_no_sqlite_file_exists_in_repository(self) -> None:
        """A stray db.sqlite3 means something bypassed the configured database."""
        assert not list(BASE_DIR.glob("*.sqlite3"))


class TestTimeConfiguration:
    def test_timezone_is_baghdad(self) -> None:
        assert settings.TIME_ZONE == "Asia/Baghdad"

    def test_timezone_support_is_enabled(self) -> None:
        """Naive datetimes would make the business-date rule unimplementable."""
        assert settings.USE_TZ is True


class TestInternationalization:
    def test_i18n_enabled(self) -> None:
        assert settings.USE_I18N is True

    def test_arabic_and_english_are_configured(self) -> None:
        codes = {code for code, _ in settings.LANGUAGES}
        assert codes == {"en", "ar"}

    def test_locale_middleware_ordered_correctly(self) -> None:
        """LocaleMiddleware must follow Session and precede Common."""
        order = list(settings.MIDDLEWARE)
        session = order.index("django.contrib.sessions.middleware.SessionMiddleware")
        locale = order.index("django.middleware.locale.LocaleMiddleware")
        common = order.index("django.middleware.common.CommonMiddleware")
        assert session < locale < common


class TestSecretHandling:
    def test_missing_variable_raises_immediately(self) -> None:
        """The env accessor has no silent default. This is the fail-fast contract."""
        env = environ.Env()
        with pytest.raises(ImproperlyConfigured):
            env.str("KHAN_MANDI_DEFINITELY_NOT_SET_9F3A")

    def test_test_settings_do_not_use_the_insecure_generated_key(self) -> None:
        assert not settings.SECRET_KEY.startswith("django-insecure-")


def _run_check_with_production_settings(
    overrides: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run `manage.py check` under production settings with env overrides applied."""
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.production"
    env.update(overrides)
    return subprocess.run(  # noqa: S603
        [sys.executable, "manage.py", "check"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestProductionGuards:
    """Production settings must refuse to boot in an unsafe configuration."""

    def test_debug_true_is_rejected(self) -> None:
        result = _run_check_with_production_settings({"DJANGO_DEBUG": "True"})
        assert result.returncode != 0
        assert "DJANGO_DEBUG must be false" in result.stderr

    def test_wildcard_allowed_hosts_is_rejected(self) -> None:
        result = _run_check_with_production_settings(
            {"DJANGO_DEBUG": "False", "DJANGO_ALLOWED_HOSTS": "*"}
        )
        assert result.returncode != 0
        assert "Wildcard ALLOWED_HOSTS" in result.stderr

    def test_insecure_development_secret_is_rejected(self) -> None:
        result = _run_check_with_production_settings(
            {
                "DJANGO_DEBUG": "False",
                "DJANGO_ALLOWED_HOSTS": "khanmandi.example.com",
                "DJANGO_SECRET_KEY": "django-insecure-abc123",
            }
        )
        assert result.returncode != 0
        assert "development SECRET_KEY" in result.stderr
