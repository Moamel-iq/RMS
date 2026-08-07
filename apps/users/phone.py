"""
Iraqi mobile number normalization.

A phone number is an identity in this system: it is a login credential and it
is unique. That only holds if one physical number has exactly one stored
representation. `07701234567`, `+964 770 123 4567`, and `009647701234567` are
the same subscriber, so they must normalize to the same string before they
reach the database.

Canonical stored form is E.164: +9647XXXXXXXXX.

ASSUMPTION — not yet confirmed by an SRS: every user of this system holds an
Iraqi mobile number. Landlines and foreign numbers are rejected. If a supplier
contact or a foreign owner ever needs an account, this rule must be revisited.
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

#: Canonical stored form. Iraqi mobile numbers are +964 followed by 7 and nine digits.
CANONICAL_PATTERN = r"^\+9647\d{9}$"

_CANONICAL_RE = re.compile(CANONICAL_PATTERN)

#: Characters humans type into phone fields that carry no information.
_SEPARATORS = re.compile(r"[\s\-().]")


def normalize_iraqi_mobile(value: str) -> str:
    """
    Return the E.164 form of an Iraqi mobile number.

    Accepts the shapes people actually type:
        07701234567, 7701234567, +9647701234567, 009647701234567,
        and any of those with spaces, dashes, dots, or parentheses.

    Raises ValidationError if the result is not a valid Iraqi mobile number.
    """
    if not isinstance(value, str):
        raise ValidationError(_("Phone number must be text."), code="invalid_phone")

    digits = _SEPARATORS.sub("", value.strip())
    if not digits:
        raise ValidationError(_("Phone number must not be empty."), code="invalid_phone")

    if digits.startswith("00964"):
        candidate = "+964" + digits[5:]
    elif digits.startswith("+964"):
        candidate = digits
    elif digits.startswith("964"):
        candidate = "+964" + digits[3:]
    elif digits.startswith("0"):
        # National form: the trunk prefix 0 is replaced by the country code.
        candidate = "+964" + digits[1:]
    elif digits.startswith("7"):
        # Subscriber number typed without trunk prefix or country code.
        candidate = "+964" + digits
    else:
        raise ValidationError(
            _("%(value)s is not a valid Iraqi mobile number."),
            code="invalid_phone",
            params={"value": value},
        )

    if not _CANONICAL_RE.match(candidate):
        raise ValidationError(
            _("%(value)s is not a valid Iraqi mobile number."),
            code="invalid_phone",
            params={"value": value},
        )

    return candidate


def validate_iraqi_mobile(value: str) -> None:
    """Field validator. Rejects anything `normalize_iraqi_mobile` cannot canonicalise."""
    normalize_iraqi_mobile(value)


def try_normalize_iraqi_mobile(value: str) -> str | None:
    """
    Normalize, or return None if the input is not a phone number at all.

    Used on the login path, where the submitted identifier may legitimately be
    a username rather than a phone number.
    """
    try:
        return normalize_iraqi_mobile(value)
    except ValidationError:
        return None
