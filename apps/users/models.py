"""
Custom user model.

Created before the first migration, as Django requires: swapping
AUTH_USER_MODEL after `migrate` has run is a rebuild, not a change.

Deliberately minimal. Organization and branch membership are NOT modelled here
— a user may hold access to several branches, so branch cannot be a field on
User. That relationship arrives with Task 0.3.
"""

from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.users.managers import UserManager
from apps.users.phone import CANONICAL_PATTERN, validate_iraqi_mobile


class User(AbstractUser):
    """
    A system user.

    Authenticates with either username or phone number; see
    `apps.users.backends.PhoneOrUsernameBackend`.
    """

    phone = models.CharField(
        _("phone number"),
        max_length=17,
        unique=True,
        null=True,
        blank=True,
        validators=[validate_iraqi_mobile],
        help_text=_("Iraqi mobile number, stored as +9647XXXXXXXXX."),
    )

    objects: UserManager = UserManager()  # type: ignore[misc]

    class Meta(AbstractUser.Meta):
        constraints = [
            # The Python validator can be bypassed by bulk operations, raw
            # queries, and data migrations. The database cannot.
            models.CheckConstraint(
                condition=Q(phone__isnull=True) | Q(phone__regex=CANONICAL_PATTERN),
                name="users_phone_is_canonical_iraqi_mobile",
            ),
            # A stored empty string would defeat the unique index, since many
            # rows could hold "". NULL is the only permitted "no phone" value.
            models.CheckConstraint(
                condition=Q(phone__isnull=True) | ~Q(phone=""),
                name="users_phone_not_empty_string",
            ),
        ]

    def __str__(self) -> str:
        return self.get_full_name() or self.username
