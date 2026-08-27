"""Django Admin is a superuser-only break-glass surface."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.users.models import User

pytestmark = pytest.mark.django_db


def test_staff_account_cannot_open_django_admin() -> None:
    staff = User.objects.create_user(username="staff-only", password="pw", is_staff=True)
    client = Client()
    client.force_login(staff)

    response = client.get(reverse("admin:index"), follow=True)

    assert response.status_code == 403


def test_superuser_can_open_django_admin() -> None:
    superuser = User.objects.create_superuser(username="break-glass", password="pw")
    client = Client()
    client.force_login(superuser)

    assert client.get(reverse("admin:index")).status_code == 200
