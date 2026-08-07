"""
Authentication backend accepting either a phone number or a username.

Subclasses ModelBackend so permission resolution, `user_can_authenticate`, and
the inactive-user rule keep Django's behaviour. Only identifier lookup changes.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q
from django.http import HttpRequest

from apps.users.phone import try_normalize_iraqi_mobile


class PhoneOrUsernameBackend(ModelBackend):
    """Resolve the submitted identifier as a username or an Iraqi mobile number."""

    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> Any:
        user_model = get_user_model()

        if username is None:
            username = kwargs.get(user_model.USERNAME_FIELD)

        if not username or password is None:
            return None

        identifier = username.strip()
        lookup = Q(username__iexact=identifier)

        normalized_phone = try_normalize_iraqi_mobile(identifier)
        if normalized_phone is not None:
            lookup |= Q(phone=normalized_phone)

        try:
            user = user_model.objects.get(lookup)
        except user_model.DoesNotExist:
            # Run the hasher anyway. Returning early here would make a
            # non-existent account measurably faster to probe than a real one.
            user_model().set_password(password)
            return None
        except user_model.MultipleObjectsReturned:
            # One identifier resolving to two accounts is ambiguous. Fail closed
            # rather than picking one.
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
