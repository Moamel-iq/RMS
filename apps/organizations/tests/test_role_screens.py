"""
The roles screens (ADR-034): staff define a post, tick its acts, retire it.

Screen tests assert on content: the matrix must list the real permissions
with their Arabic labels, a save must reach the service, and the lifecycle
refusal must reach the reader as a message rather than a blank redirect.
"""

from __future__ import annotations

from datetime import time

import pytest
from django.test import Client
from django.urls import reverse

from apps.inventory.permissions import CREATE_ITEM, EDIT_ITEM, VIEW_ITEM
from apps.organizations.models import Branch, Organization, Role, RoleDefinition
from apps.organizations.services import (
    create_branch,
    create_organization,
    create_role_definition,
    grant_branch_access,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="011",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def staff() -> Client:
    user = User.objects.create_user(username="admin", password=PASSWORD, is_staff=True)
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def registrar(organization: Organization) -> RoleDefinition:
    return create_role_definition(
        organization=organization,
        code="registrar",
        name_ar="مسجّل أصناف",
        permissions=[VIEW_ITEM, CREATE_ITEM],
    )


def test_the_screens_are_staff_business() -> None:
    user = User.objects.create_user(username="plain", password=PASSWORD)
    client = Client()
    client.force_login(user)
    assert client.get(reverse("organizations:role_list")).status_code == 403
    assert client.get(reverse("organizations:role_create")).status_code == 403


def test_the_list_shows_custom_posts_beside_the_built_in_ones(
    staff: Client, registrar: RoleDefinition
) -> None:
    body = staff.get(reverse("organizations:role_list")).content.decode()
    assert "مسجّل أصناف" in body
    assert str(Role.ACCOUNTANT.label) in body
    assert reverse("organizations:role_update", args=[registrar.pk]) in body
    assert f"?based_on={Role.MANAGER.value}" in body


def test_the_matrix_lists_real_acts_in_arabic_and_prefills_from_a_built_in(
    staff: Client,
) -> None:
    body = staff.get(reverse("organizations:role_create") + "?based_on=MANAGER").content.decode()
    # The act the owner gave as an example, by codename, with its label.
    assert f'value="{CREATE_ITEM}"' in body
    assert "عرض الأصناف" in body
    # A manager holds item registration; the box is prefilled.
    assert f'value="{CREATE_ITEM}" data-kind="create" checked' in body
    # Nothing outside the modules is offered.
    assert 'value="auth.add_user"' not in body


def test_saving_defines_the_post_through_the_service(
    staff: Client, organization: Organization
) -> None:
    response = staff.post(
        reverse("organizations:role_create"),
        {
            "organization": organization.pk,
            "code": "Registrar",
            "name_ar": "مسجّل أصناف",
            "name_en": "",
            "description": "",
            "based_on": "",
            "permissions": [VIEW_ITEM, CREATE_ITEM],
        },
    )
    assert response.status_code == 302, response.content.decode()[:500]
    definition = RoleDefinition.objects.get(organization=organization, code="registrar")
    held = {f"{p.content_type.app_label}.{p.codename}" for p in definition.permissions.all()}
    assert held == {VIEW_ITEM, CREATE_ITEM}


def test_editing_changes_the_acts_and_an_invented_code_is_refused(
    staff: Client, registrar: RoleDefinition
) -> None:
    url = reverse("organizations:role_update", args=[registrar.pk])
    body = staff.get(url).content.decode()
    assert f'value="{CREATE_ITEM}" data-kind="create" checked' in body
    assert f'value="{EDIT_ITEM}" data-kind="edit">' in body

    response = staff.post(
        url,
        {
            "name_ar": "مسجّل ومحرّر",
            "name_en": "",
            "description": "",
            "permissions": [VIEW_ITEM, EDIT_ITEM],
        },
    )
    assert response.status_code == 302
    registrar.refresh_from_db()
    assert registrar.name_ar == "مسجّل ومحرّر"
    assert {p.codename for p in registrar.permissions.all()} == {"view_item", "edit_item"}

    refused = staff.post(
        url,
        {
            "name_ar": "x",
            "name_en": "",
            "description": "",
            "permissions": ["inventory.no_such_act"],
        },
    )
    assert refused.status_code == 200
    assert {p.codename for p in registrar.permissions.all()} == {"view_item", "edit_item"}


def test_archiving_a_held_post_is_refused_with_a_message(
    staff: Client, branch: Branch, registrar: RoleDefinition
) -> None:
    holder = User.objects.create_user(username="clerk", password=PASSWORD)
    grant_branch_access(user=holder, branch=branch, role=registrar.key)

    response = staff.post(
        reverse("organizations:role_archive", args=[registrar.pk]),
        {"reason": "لم يعد مطلوباً"},
        follow=True,
    )
    assert "اسحب الصلاحيات أولاً" in response.content.decode()
    registrar.refresh_from_db()
    assert registrar.is_active
