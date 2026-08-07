"""
Production settings.

Fails loudly at import time rather than degrading quietly. Every check below
exists because the silent-failure version of it is a security incident.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env

DEBUG = False

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

_REQUIRED = (
    "DJANGO_SECRET_KEY",
    "DJANGO_ALLOWED_HOSTS",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DB_HOST",
)

_missing = [name for name in _REQUIRED if not env.str(name, default="")]
if _missing:
    raise ImproperlyConfigured(
        "Missing required production environment variables: " + ", ".join(_missing)
    )

if env.bool("DJANGO_DEBUG", default=False):
    raise ImproperlyConfigured("DJANGO_DEBUG must be false in production.")

if "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("Wildcard ALLOWED_HOSTS is not permitted in production.")

if SECRET_KEY.startswith("django-insecure-"):  # noqa: F405
    raise ImproperlyConfigured("The development SECRET_KEY must not be used in production.")


# ---------------------------------------------------------------------------
# Transport and cookie security
# ---------------------------------------------------------------------------

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# HSTS. Raised to a year once the deployment domain is confirmed stable.
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=3600)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Set only when running behind a proxy that terminates TLS.
if env.bool("DJANGO_USE_PROXY_SSL_HEADER", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Persistent connections; the pool is sized by the deployment platform.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)  # noqa: F405
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True  # noqa: F405
