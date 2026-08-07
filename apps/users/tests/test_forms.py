"""
Admin form normalization.

The admin saves through a ModelForm, never through the manager. Without
normalization here an administrator typing `07701234567` would trip the
database CHECK constraint and be shown an IntegrityError traceback instead of
a field error.
"""

from __future__ import annotations

import pytest

from apps.users.forms import UserAdminChangeForm, UserAdminCreationForm
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"


class TestCreationForm:
    def test_normalizes_phone_on_save(self) -> None:
        form = UserAdminCreationForm(
            data={
                "username": "storekeeper",
                "phone": "0770 123 4567",
                "password1": PASSWORD,
                "password2": PASSWORD,
            }
        )
        assert form.is_valid(), form.errors
        user = form.save()
        assert user.phone == "+9647701234567"

    def test_blank_phone_is_stored_as_null(self) -> None:
        form = UserAdminCreationForm(
            data={
                "username": "no-phone",
                "phone": "",
                "password1": PASSWORD,
                "password2": PASSWORD,
            }
        )
        assert form.is_valid(), form.errors
        assert form.save().phone is None

    def test_invalid_phone_is_a_field_error_not_a_crash(self) -> None:
        form = UserAdminCreationForm(
            data={
                "username": "bad-phone",
                "phone": "12345",
                "password1": PASSWORD,
                "password2": PASSWORD,
            }
        )
        assert not form.is_valid()
        assert "phone" in form.errors


class TestChangeForm:
    def test_normalizes_phone_on_edit(self) -> None:
        user = User.objects.create_user(username="editme", password=PASSWORD)
        form = UserAdminChangeForm(
            instance=user,
            data={
                "username": user.username,
                "phone": "07701234567",
                "password": user.password,
                "date_joined": user.date_joined,
                "is_active": True,
            },
        )
        assert form.is_valid(), form.errors
        assert form.save().phone == "+9647701234567"
