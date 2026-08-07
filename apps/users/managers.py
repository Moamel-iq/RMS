"""User manager.

Normalisation happens here so that every creation path — management command,
admin, fixture loader, future import — stores the same canonical phone form.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import UserManager as DjangoUserManager

from apps.users.phone import normalize_iraqi_mobile

if TYPE_CHECKING:
    from apps.users.models import User


class UserManager(DjangoUserManager["User"]):
    """Manager that canonicalises the phone number on every create path."""

    @staticmethod
    def _normalize_phone(extra_fields: dict[str, Any]) -> None:
        phone = extra_fields.get("phone")
        if phone:
            extra_fields["phone"] = normalize_iraqi_mobile(phone)
        elif "phone" in extra_fields:
            # Empty string would collide on the unique index; NULL does not.
            extra_fields["phone"] = None

    def create_user(
        self,
        username: str,
        email: str | None = None,
        password: str | None = None,
        **extra_fields: Any,
    ) -> User:
        self._normalize_phone(extra_fields)
        return super().create_user(username, email, password, **extra_fields)

    def create_superuser(
        self,
        username: str,
        email: str | None = None,
        password: str | None = None,
        **extra_fields: Any,
    ) -> User:
        self._normalize_phone(extra_fields)
        return super().create_superuser(username, email, password, **extra_fields)
