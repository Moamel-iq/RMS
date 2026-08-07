"""Local development settings."""

from .base import *  # noqa: F403
from .base import env

DEBUG = env.bool("DJANGO_DEBUG", default=True)

ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS",
    default=["127.0.0.1", "localhost"],
)

# Developer-friendly: mail is printed to the console rather than sent.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
