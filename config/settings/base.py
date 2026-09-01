"""
Base settings shared by every environment.

Environment-specific modules (local, test, production) import from here and
override only what differs. No secret ever has a hardcoded fallback: a missing
required variable must raise at startup, not silently degrade at runtime.
"""

from pathlib import Path
from typing import Any

import environ

# Repository root: config/settings/base.py -> config/settings -> config -> root
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

# Read .env when present. In production the platform supplies real environment
# variables and this file is absent, which is correct.
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    env.read_env(str(_env_file))


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

# No default. Missing DJANGO_SECRET_KEY raises ImproperlyConfigured at import.
SECRET_KEY = env.str("DJANGO_SECRET_KEY")

DEBUG = env.bool("DJANGO_DEBUG", default=False)

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "simple_history",
]

# Foundation apps are added by their own tasks:
#   Task 0.6  apps.accounting
# apps.core holds abstract models and the quantity precision policy; Task 0.5
# expands it into the full audit foundation.
LOCAL_APPS: list[str] = [
    "apps.core",
    "apps.users",
    "apps.organizations",
    "apps.units",
    "apps.accounting",
    "apps.inventory",
    "apps.procurement",
    "apps.supplier_quotes",
    "apps.kitchen",
    "apps.sales",
    "apps.hr",
    "apps.insights",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


# ---------------------------------------------------------------------------
# Custom user model
# ---------------------------------------------------------------------------

AUTH_USER_MODEL = "users.User"

# Users sign in with either a username or an Iraqi mobile number. The backend
# subclasses ModelBackend, so permission resolution is unchanged.
AUTHENTICATION_BACKENDS = [
    "apps.users.backends.PhoneOrUsernameBackend",
]

LOGIN_URL = "users:login"
LOGIN_REDIRECT_URL = "users:home"
LOGOUT_REDIRECT_URL = "users:login"


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves the immutable files built by collectstatic in production. It must
    # follow SecurityMiddleware so security headers apply to static responses.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Measure dynamic requests and database calls, then compress their
    # responses. WhiteNoise handles immutable static files before either one.
    "config.middleware.RequestPerformanceMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Must sit after SessionMiddleware and before CommonMiddleware, which
    # relies on the active locale. Replaces django.middleware.locale.
    # LocaleMiddleware so the browser's Accept-Language cannot flip an
    # Arabic RTL interface into a left-to-right layout.
    "config.middleware.ExplicitLocaleMiddleware",
    # Full pages and HTMX fragments share URLs but never cache entries.
    "config.middleware.HtmxVaryMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Records the acting user on django-simple-history rows.
    "simple_history.middleware.HistoryRequestMiddleware",
    # Binds actor and correlation id for the audit trail. Must follow
    # AuthenticationMiddleware, or request.user is not resolved yet and every
    # event would record no actor.
    "apps.core.middleware.AuditContextMiddleware",
]

# Safe request/DB timing. The middleware records route names and numeric
# timings only; SQL, bound parameters, paths and request bodies are never kept.
PERFORMANCE_MONITORING_ENABLED = env.bool("DJANGO_PERFORMANCE_MONITORING", default=True)
PERFORMANCE_LOG_ALL_REQUESTS = env.bool("DJANGO_PERFORMANCE_LOG_ALL", default=True)
PERFORMANCE_SLOW_REQUEST_MS = env.int("DJANGO_SLOW_REQUEST_MS", default=500)
PERFORMANCE_SLOW_DB_QUERY_MS = env.int("DJANGO_SLOW_DB_QUERY_MS", default=100)

ROOT_URLCONF = "config.urls"

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "builtins": [
                "apps.core.templatetags.money_tags",
                "apps.core.templatetags.quantity_tags",
            ],
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
                # Before `shell`, and separate from it: `shell` returns nothing
                # for an anonymous request, and the login page is exactly where
                # the product has to name itself.
                "apps.core.branding.branding",
                "apps.core.context_processors.shell",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
#
# PostgreSQL only. SQLite is never acceptable for this project: the financial
# invariants rely on database-level CHECK/UNIQUE constraints and transactional
# behaviour that must be identical in development, CI, and production.

_database_url = env.str("DATABASE_URL", default="")
if _database_url:
    # Managed platforms such as Render provide one private connection URL.
    # Keep the explicit variables below as the local-development fallback.
    DATABASES = {"default": env.db("DATABASE_URL")}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env.str("DB_NAME"),
            "USER": env.str("DB_USER"),
            "PASSWORD": env.str("DB_PASSWORD"),
            "HOST": env.str("DB_HOST", default="127.0.0.1"),
            "PORT": env.str("DB_PORT", default="5432"),
            "ATOMIC_REQUESTS": False,
            "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", default=0),
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# Internationalization and time
# ---------------------------------------------------------------------------
#
# Timestamps are stored in UTC. Asia/Baghdad is the display timezone.
# The restaurant *business date* is a separate concept delivered in a later
# task; it is never derived as date(timestamp).

# Arabic is the primary language of the business, so it is the default and the
# source language for message IDs. English is the translation target.
LANGUAGE_CODE = "ar"
TIME_ZONE = "Asia/Baghdad"

USE_I18N = True
USE_TZ = True

LANGUAGES = [
    ("en", "English"),
    ("ar", "Arabic"),
]

LOCALE_PATHS = [BASE_DIR / "locale"]


# ---------------------------------------------------------------------------
# Static and media files
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env.str("DJANGO_MEDIA_ROOT", default=str(BASE_DIR / "media")))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
#
# Never log SQL parameters or environment values: they carry passwords,
# payroll figures, and supplier pricing.

LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": env.str("DJANGO_LOG_LEVEL", default="INFO"),
    },
    "loggers": {
        "django.db.backends": {
            "handlers": ["console"],
            # DEBUG here would echo every query, including bound parameters.
            "level": "INFO",
            "propagate": False,
        },
    },
}
