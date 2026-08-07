"""Custom user model and its database-level guarantees."""

from __future__ import annotations

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction

from apps.users.models import User

pytestmark = pytest.mark.django_db


class TestUserModelIsWiredUp:
    def test_auth_user_model_points_at_our_model(self) -> None:
        assert settings.AUTH_USER_MODEL == "users.User"

    def test_get_user_model_returns_our_model(self) -> None:
        """A stray import of django.contrib.auth.models.User would break this."""
        assert get_user_model() is User


class TestUserCreation:
    def test_create_user(self) -> None:
        user = User.objects.create_user(username="storekeeper", password="pw-not-real-1234")
        assert user.is_active
        assert not user.is_staff
        assert not user.is_superuser
        assert user.check_password("pw-not-real-1234")

    def test_create_superuser(self) -> None:
        user = User.objects.create_superuser(username="admin", password="pw-not-real-1234")
        assert user.is_staff
        assert user.is_superuser

    def test_password_is_hashed_not_stored(self) -> None:
        user = User.objects.create_user(username="cashier", password="pw-not-real-1234")
        assert user.password != "pw-not-real-1234"
        assert user.password.startswith(("pbkdf2_", "argon2", "bcrypt", "md5$"))

    def test_user_without_phone_is_permitted(self) -> None:
        user = User.objects.create_user(username="no-phone", password="pw-not-real-1234")
        assert user.phone is None

    def test_str_prefers_full_name_then_username(self) -> None:
        named = User.objects.create_user(
            username="a.hassan", password="pw-not-real-1234", first_name="Ahmed", last_name="Hassan"
        )
        anonymous = User.objects.create_user(username="plain", password="pw-not-real-1234")
        assert str(named) == "Ahmed Hassan"
        assert str(anonymous) == "plain"


class TestPhoneNormalizationOnCreate:
    def test_manager_normalizes_phone_on_create_user(self) -> None:
        user = User.objects.create_user(
            username="storekeeper", password="pw-not-real-1234", phone="0770 123 4567"
        )
        assert user.phone == "+9647701234567"

    def test_manager_normalizes_phone_on_create_superuser(self) -> None:
        user = User.objects.create_superuser(
            username="admin", password="pw-not-real-1234", phone="07701234567"
        )
        assert user.phone == "+9647701234567"

    def test_empty_phone_becomes_null_not_empty_string(self) -> None:
        """Empty strings would collide on the unique index. NULL does not."""
        user = User.objects.create_user(username="no-phone", password="pw-not-real-1234", phone="")
        assert user.phone is None


class TestPhoneUniqueness:
    def test_duplicate_phone_is_rejected(self) -> None:
        User.objects.create_user(username="first", password="pw-not-real-1234", phone="07701234567")
        with pytest.raises(IntegrityError):
            User.objects.create_user(
                username="second", password="pw-not-real-1234", phone="07701234567"
            )

    def test_same_number_written_differently_is_still_a_duplicate(self) -> None:
        """The whole point of normalising before storing."""
        User.objects.create_user(username="first", password="pw-not-real-1234", phone="07701234567")
        with pytest.raises(IntegrityError):
            User.objects.create_user(
                username="second", password="pw-not-real-1234", phone="+964 770 123 4567"
            )

    def test_many_users_may_have_no_phone(self) -> None:
        User.objects.create_user(username="one", password="pw-not-real-1234")
        User.objects.create_user(username="two", password="pw-not-real-1234")
        assert User.objects.filter(phone__isnull=True).count() == 2


class TestDatabaseConstraints:
    """
    The Python validator is bypassed by bulk operations, raw SQL, and data
    migrations. These prove the database refuses bad data on its own.
    """

    def test_database_rejects_a_non_canonical_phone(self) -> None:
        user = User.objects.create_user(username="raw", password="pw-not-real-1234")
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE users_user SET phone = %s WHERE id = %s",
                    ["07701234567", user.id],
                )

    def test_database_rejects_an_empty_string_phone(self) -> None:
        user = User.objects.create_user(username="raw", password="pw-not-real-1234")
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("UPDATE users_user SET phone = %s WHERE id = %s", ["", user.id])

    def test_database_accepts_a_canonical_phone(self) -> None:
        user = User.objects.create_user(username="raw", password="pw-not-real-1234")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE users_user SET phone = %s WHERE id = %s",
                ["+9647701234567", user.id],
            )
        user.refresh_from_db()
        assert user.phone == "+9647701234567"

    def test_expected_constraints_exist_in_the_database(self) -> None:
        names = {c.name for c in User._meta.constraints}
        assert names == {
            "users_phone_is_canonical_iraqi_mobile",
            "users_phone_not_empty_string",
        }
