"""The dedicated item form must not silently fall back to the generic form."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.users.models import User

pytestmark = pytest.mark.django_db


def test_item_create_renders_the_guided_item_form(
    manager: User, client_for: Callable[[User], Client], organization: Any
) -> None:
    response = client_for(manager).get(reverse("inventory:item_create"))

    body = response.content.decode()
    assert response.status_code == 200
    assert "data-inventory-item-form" in body
    assert 'id="item-identity-title"' in body
    assert 'id="item-tracking-title"' in body
    assert "css/erp-design-system.css" in body
    assert "css/inventory.css" not in body
    assert "js/inventory-htmx.js" in body
