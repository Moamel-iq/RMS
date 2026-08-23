"""
`manage_items` split three ways (ADR-034 §2).

The owner's example: a clerk who may register a new item and must not change
one. Registering is `create_item`, changing is `edit_item`, and the structural
acts — archive, reactivate — stay with `manage_items`. The built-in manager
holds all three, so nobody's authority changed by the split; only the
vocabulary became fine enough to say this.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse

from apps.inventory.models import InventoryItem
from apps.inventory.permissions import CREATE_ITEM, EDIT_ITEM, MANAGE_ITEMS, VIEW_ITEM
from apps.organizations.authorization import has_organization_master_data_permission
from apps.organizations.models import Branch, Organization
from apps.organizations.services import create_role_definition, grant_branch_access
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def clerk(organization: Organization, branch: Branch) -> User:
    """May see and register items; may not edit or archive them."""
    user = User.objects.create_user(username="clerk", password="pw-not-real-1234")
    post = create_role_definition(
        organization=organization,
        code="registrar",
        name_ar="مسجّل أصناف",
        permissions=[VIEW_ITEM, CREATE_ITEM],
    )
    grant_branch_access(user=user, branch=branch, role=post.key)
    return User.objects.get(pk=user.pk)


def test_the_manager_keeps_all_three_acts(manager: User, organization: Organization) -> None:
    for permission in (MANAGE_ITEMS, CREATE_ITEM, EDIT_ITEM):
        assert has_organization_master_data_permission(manager, permission, organization)


def test_a_registrar_may_open_the_new_item_screen_and_not_the_edit_screen(
    client_for: Any, clerk: User, rice: InventoryItem
) -> None:
    client = client_for(clerk)
    assert client.get(reverse("inventory:item_create")).status_code == 200
    assert client.get(reverse("inventory:item_update", args=[rice.pk])).status_code == 403
    assert client.post(reverse("inventory:item_archive", args=[rice.pk])).status_code == 403


def test_the_list_offers_the_registrar_a_new_button_and_no_edit_link(
    client_for: Any, clerk: User, manager: User, rice: InventoryItem
) -> None:
    body = client_for(clerk).get(reverse("inventory:item_list")).content.decode()
    assert reverse("inventory:item_create") in body
    assert reverse("inventory:item_update", args=[rice.pk]) not in body

    manager_body = client_for(manager).get(reverse("inventory:item_list")).content.decode()
    assert reverse("inventory:item_update", args=[rice.pk]) in manager_body


def test_registering_an_item_works_with_create_item_alone(
    client_for: Any, clerk: User, organization: Organization, leaf_category: Any, kilogram: Any
) -> None:
    response = client_for(clerk).post(
        reverse("inventory:item_create"),
        {
            "organization": organization.pk,
            "code": "STK-9001",
            "name_ar": "صنف المسجّل",
            "name_en": "Registrar item",
            "category": leaf_category.pk,
            "item_type": "RAW_MATERIAL",
            "base_unit": kilogram.pk,
            "is_active": "on",
        },
    )
    assert response.status_code in (302, 200), response.status_code
    assert InventoryItem.objects.filter(organization=organization, code="STK-9001").exists()
