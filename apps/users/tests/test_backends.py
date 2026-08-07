"""
Authentication by phone number or username.

The login screen offers one field for both, so the backend must resolve either
without letting an attacker learn which accounts exist.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import authenticate

from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"


@pytest.fixture
def user() -> User:
    return User.objects.create_user(username="a.hassan", password=PASSWORD, phone="07701234567")


class TestSuccessfulAuthentication:
    def test_by_username(self, user: User) -> None:
        assert authenticate(username="a.hassan", password=PASSWORD) == user

    def test_by_username_is_case_insensitive(self, user: User) -> None:
        assert authenticate(username="A.Hassan", password=PASSWORD) == user

    def test_by_canonical_phone(self, user: User) -> None:
        assert authenticate(username="+9647701234567", password=PASSWORD) == user

    def test_by_national_phone(self, user: User) -> None:
        """The form people actually type: 07701234567."""
        assert authenticate(username="07701234567", password=PASSWORD) == user

    @pytest.mark.parametrize(
        "written", ["0770 123 4567", "0770-123-4567", "009647701234567", "7701234567"]
    )
    def test_by_any_spelling_of_the_phone(self, user: User, written: str) -> None:
        assert authenticate(username=written, password=PASSWORD) == user

    def test_surrounding_whitespace_is_tolerated(self, user: User) -> None:
        assert authenticate(username="  a.hassan  ", password=PASSWORD) == user


class TestRejectedAuthentication:
    def test_wrong_password(self, user: User) -> None:
        assert authenticate(username="a.hassan", password="wrong-password") is None

    def test_wrong_password_by_phone(self, user: User) -> None:
        assert authenticate(username="07701234567", password="wrong-password") is None

    def test_unknown_username(self) -> None:
        assert authenticate(username="nobody", password=PASSWORD) is None

    def test_unknown_phone(self) -> None:
        assert authenticate(username="07709999999", password=PASSWORD) is None

    def test_inactive_user_cannot_authenticate(self, user: User) -> None:
        user.is_active = False
        user.save(update_fields=["is_active"])
        assert authenticate(username="a.hassan", password=PASSWORD) is None

    def test_empty_identifier(self) -> None:
        assert authenticate(username="", password=PASSWORD) is None

    def test_missing_password(self, user: User) -> None:
        assert authenticate(username="a.hassan", password=None) is None

    def test_user_with_unusable_password_is_rejected(self) -> None:
        account = User.objects.create_user(username="sso-only")
        account.set_unusable_password()
        account.save(update_fields=["password"])
        assert authenticate(username="sso-only", password="") is None


class TestAmbiguousIdentifier:
    def test_identifier_matching_two_accounts_fails_closed(self) -> None:
        """
        One account's username is another account's phone number. Picking either
        would be a guess, so the backend refuses both.
        """
        User.objects.create_user(username="+9647701234567", password=PASSWORD)
        User.objects.create_user(username="real.person", password=PASSWORD, phone="07701234567")
        assert authenticate(username="+9647701234567", password=PASSWORD) is None


class TestNoUserEnumeration:
    def test_unknown_identifier_and_wrong_password_are_indistinguishable(self, user: User) -> None:
        """
        Both return None. The backend also runs the password hasher on the
        unknown-user path so the two cases cost roughly the same time; this
        test pins the observable behaviour, not the timing.
        """
        assert authenticate(username="does-not-exist", password=PASSWORD) is None
        assert authenticate(username="a.hassan", password="wrong-password") is None
