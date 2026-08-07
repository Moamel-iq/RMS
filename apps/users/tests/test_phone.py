"""
Phone normalization.

A phone number is a login credential and a unique key. If two spellings of the
same number produce two stored values, one person can hold two accounts and
uniqueness means nothing.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from hypothesis import given
from hypothesis import strategies as st

from apps.users.phone import (
    normalize_iraqi_mobile,
    try_normalize_iraqi_mobile,
    validate_iraqi_mobile,
)

CANONICAL = "+9647701234567"


class TestAcceptedForms:
    """Every shape a person might type for one number maps to one stored value."""

    @pytest.mark.parametrize(
        "written",
        [
            "+9647701234567",  # already canonical
            "009647701234567",  # international prefix
            "9647701234567",  # country code, no plus
            "07701234567",  # national, trunk prefix
            "7701234567",  # subscriber only
            "+964 770 123 4567",  # spaces
            "0770-123-4567",  # dashes
            "0770 123 4567",
            "(0770) 123.4567",  # parentheses and dots
            "  07701234567  ",  # surrounding whitespace
        ],
    )
    def test_all_spellings_collapse_to_one_canonical_value(self, written: str) -> None:
        assert normalize_iraqi_mobile(written) == CANONICAL

    def test_normalization_is_idempotent(self) -> None:
        assert normalize_iraqi_mobile(normalize_iraqi_mobile("07701234567")) == CANONICAL

    @pytest.mark.parametrize("prefix", ["70", "75", "77", "78", "79"])
    def test_all_operator_prefixes_are_accepted(self, prefix: str) -> None:
        assert normalize_iraqi_mobile(f"0{prefix}01234567") == f"+964{prefix}01234567"


class TestRejectedForms:
    @pytest.mark.parametrize(
        "written",
        [
            "",
            "   ",
            "abcdefghijk",
            "0770123456",  # one digit short
            "077012345678",  # one digit long
            "+9647701234",  # too short with country code
            "+15551234567",  # foreign number
            "+9641234567890",  # Iraqi country code but not a mobile prefix
            "01234567890",  # landline-shaped, not starting with 7
            "++9647701234567",
            "+964 770 123 456A",  # letter in the subscriber number
        ],
    )
    def test_invalid_numbers_are_rejected(self, written: str) -> None:
        with pytest.raises(ValidationError):
            normalize_iraqi_mobile(written)

    def test_non_string_input_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            normalize_iraqi_mobile(7701234567)  # type: ignore[arg-type]

    def test_validator_raises_on_invalid(self) -> None:
        with pytest.raises(ValidationError):
            validate_iraqi_mobile("not-a-phone")

    def test_validator_accepts_valid(self) -> None:
        validate_iraqi_mobile("07701234567")  # must not raise


class TestTryNormalize:
    """The login path must tolerate a username being submitted."""

    def test_returns_none_for_a_username(self) -> None:
        assert try_normalize_iraqi_mobile("ahmed.hassan") is None

    def test_returns_canonical_for_a_phone(self) -> None:
        assert try_normalize_iraqi_mobile("07701234567") == CANONICAL


class TestPhoneProperties:
    @given(
        prefix=st.sampled_from(["70", "71", "72", "73", "74", "75", "76", "77", "78", "79"]),
        rest=st.text(alphabet="0123456789", min_size=8, max_size=8),
    )
    def test_national_and_international_forms_always_agree(self, prefix: str, rest: str) -> None:
        """0XXXXXXXXXX and +964XXXXXXXXXX describe the same subscriber."""
        subscriber = f"{prefix}{rest}"
        assert normalize_iraqi_mobile(f"0{subscriber}") == normalize_iraqi_mobile(
            f"+964{subscriber}"
        )

    @given(
        prefix=st.sampled_from(["70", "75", "77", "78", "79"]),
        rest=st.text(alphabet="0123456789", min_size=8, max_size=8),
    )
    def test_output_always_matches_canonical_shape(self, prefix: str, rest: str) -> None:
        result = normalize_iraqi_mobile(f"0{prefix}{rest}")
        assert result.startswith("+9647")
        assert len(result) == 14
        assert result[1:].isdigit()
