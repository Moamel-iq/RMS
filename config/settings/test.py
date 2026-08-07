"""
Test settings.

Deterministic by design: no external services, no network, fast hashing.
The database is still PostgreSQL — tests must exercise the same constraints,
transaction semantics, and collation as production.
"""

from .base import *  # noqa: F403
from .base import LOGGING

DEBUG = False

ALLOWED_HOSTS = ["testserver", "127.0.0.1", "localhost"]

# Fast, insecure hashing. Acceptable ONLY here.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Deterministic language for assertions on translated strings.
LANGUAGE_CODE = "en"

# Keep test output readable; raise the level to debug a specific failure.
LOGGING["root"]["level"] = "WARNING"
