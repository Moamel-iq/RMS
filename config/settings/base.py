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
#   Task 0.2  apps.users
#   Task 0.3  apps.organizations
#   Task 0.4  apps.units
#   Task 0.5  apps.core (audit foundation)
#   Task 0.6  apps.accounting
LOCAL_APPS: list[str] = []

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


# ---------------------------------------------------------------------------
# Custom user model
# ---------------------------------------------------------------------------

# AUTH_USER_MODEL = "users.User"
#
# DELIBERATELY NOT SET YET. It is delivered by Task 0.2, and the first
# `migrate` must not run before it exists. Setting it here while apps.users
# is absent would break `manage.py check`.


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # LocaleMiddleware must sit after SessionMiddleware (it reads the session
    # language) and before CommonMiddleware (which relies on the active locale).
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Records the acting user on django-simple-history rows.
    "simple_history.middleware.HistoryRequestMiddleware",
]

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
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
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

LANGUAGE_CODE = "en"
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

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"


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
