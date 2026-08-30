"""Regression coverage for the redesigned accounting workspaces."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.accounting.models import Account, ImportedChartAccount

pytestmark = pytest.mark.django_db


def test_imported_chart_is_idempotent_and_does_not_replace_live_chart(
    organization: Any, chart: Any
) -> None:
    live_count = Account.objects.filter(organization=organization).count()

    call_command("import_accounting_chart", organization=organization.code, verbosity=0)
    call_command("import_accounting_chart", organization=organization.code, verbosity=0)

    imported = ImportedChartAccount.objects.filter(organization=organization)
    assert imported.count() == 113
    assert imported.filter(parent__isnull=True).count() == 5
    account_12121 = imported.get(source_code="12121")
    account_2213000 = imported.get(source_code="2213000")
    assert account_12121.parent is not None
    assert account_2213000.parent is not None
    assert account_12121.parent.source_code == "1212"
    assert account_2213000.parent.source_code == "22"
    assert not imported.exclude(source_debit=0).exists()
    assert not imported.exclude(source_credit=0).exists()
    assert not imported.exclude(source_balance=0).exists()
    assert not imported.exclude(statement_name="").exists()
    assert not imported.exclude(category="").exists()
    assert not imported.exclude(currency="").exists()
    assert Account.objects.filter(organization=organization).count() == live_count


def test_accounting_workspaces_render_for_authorized_user(
    organization: Any, chart: Any, superuser: Any, client_for: Any
) -> None:
    call_command("import_accounting_chart", organization=organization.code, verbosity=0)
    client = client_for(superuser)

    for route in (
        "accounting:imported_chart_tree",
        "accounting:asset_overview",
        "accounting:cost_center_list",
    ):
        response = client.get(reverse(route), {"organization": organization.pk})
        assert response.status_code == 200
        assert 'dir="rtl"' in response.content.decode()

    tree = client.get(
        reverse("accounting:chart_tree"),
        {"organization": organization.pk},
    )
    assert "113" in tree.content.decode()
    assert "الرمز واسم الحساب فقط" in tree.content.decode()
    assert "مدين" not in tree.content.decode()


def test_imported_chart_children_returns_small_htmx_fragment(
    organization: Any, superuser: Any, client_for: Any
) -> None:
    call_command("import_accounting_chart", organization=organization.code, verbosity=0)
    root = ImportedChartAccount.objects.get(organization=organization, source_code="1")

    response = client_for(superuser).get(
        reverse("accounting:imported_chart_children", args=[root.pk]),
        HTTP_HX_REQUEST="true",
    )

    body = response.content.decode()
    assert response.status_code == 200
    assert "<html" not in body
    assert "11" in body
    assert "الأصول المتداولة" in body
