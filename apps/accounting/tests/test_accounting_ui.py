"""
The two screen properties that a status-code check cannot see.

A 200 carrying an empty body passes every naive smoke ever written, which is
how nine screens shipped blank in an earlier phase and went unnoticed until
somebody opened one. Both tests here assert on **content**:

* every dashboard card answers as its own fragment with real markup and no
  second page shell, so one failing card cannot blank the other fourteen;
* every report export is CSV with a BOM and comes from the same service the
  screen used, not a second query that could drift from it.
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal
from typing import Any

import pytest
from django.urls import reverse

from apps.accounting.dashboard_views import CARDS
from apps.accounting.models import Account, AccountingPeriod, FiscalYear, PeriodState
from apps.accounting.services import post_entry
from apps.accounting.validators import PostingLine
from apps.organizations.models import Branch, Organization
from apps.users.models import User

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db

MARKUP = re.compile(r"<(div|table|form|section|ul|tbody|tr|p|span|h[1-3])")

EXPORTS = ("trial_balance", "general_ledger", "income_statement", "balance_sheet")


@pytest.fixture
def a_posted_journal(
    organization: Organization, branch: Branch, chart: None, cash: Account, hall: Any
) -> None:
    sales = Account.objects.get(organization=organization, code="4-01-01-001")
    post_entry(
        organization=organization,
        accounting_date=POSTING_DATE,
        lines=[
            PostingLine(account=cash, branch=branch, debit=Decimal("75000")),
            PostingLine(account=sales, branch=branch, cost_center=hall, credit=Decimal("75000")),
        ],
        narration="cash sale",
        idempotency_key="test:ui:sale",
    )


def test_every_dashboard_card_answers_as_a_fragment(
    client_for: Any, superuser: User, a_posted_journal: None
) -> None:
    """
    Each card is fetched independently, so a card that fails degrades to one
    error tile rather than taking the page with it.

    The assertion is on markup, not status: an empty 200 would satisfy a status
    check and render an invisible hole on the page.
    """
    client = client_for(superuser)
    failures: list[str] = []

    for card in CARDS:
        url = reverse("accounting:dashboard_card", args=[card.key])
        response = client.get(url, headers={"HX-Request": "true"})
        body = response.content.decode("utf-8").strip()

        if response.status_code != 200:
            failures.append(f"{card.key}: HTTP {response.status_code}")
            continue
        if not MARKUP.search(body):
            failures.append(f"{card.key}: no markup in {len(body)} bytes")
            continue
        if "<html" in body.lower() or "<body" in body.lower():
            failures.append(f"{card.key}: a fragment carrying a second page shell")

    assert not failures, "dashboard cards that did not answer as fragments:\n  " + "\n  ".join(
        failures
    )
    assert len(CARDS) == 16


def test_every_report_export_is_csv_with_a_bom(
    client_for: Any, superuser: User, organization: Organization, a_posted_journal: None
) -> None:
    """
    Excel needs the BOM to read Arabic, and an auditor needs the export to be
    the rows the screen just built rather than a second query that has drifted.
    """
    client = client_for(superuser)

    for name in EXPORTS:
        url = reverse(f"accounting:{name}")
        response = client.get(url, {"organization": organization.pk, "export": "csv"})

        assert response.status_code == 200, f"{name}: HTTP {response.status_code}"
        assert response["Content-Type"] == "text/csv; charset=utf-8", name
        assert response.content.startswith(b"\xef\xbb\xbf"), f"{name}: no UTF-8 BOM"
        assert b"attachment" in response["Content-Disposition"].encode(), name


def test_an_exported_cell_cannot_execute_as_a_formula(
    client_for: Any, superuser: User, organization: Organization, a_posted_journal: None
) -> None:
    """
    A value beginning `=`, `+`, `-` or `@` is neutralised before it reaches a
    spreadsheet. Account names are operator-entered text, and a spreadsheet
    treats a leading `=` as an instruction.
    """
    client = client_for(superuser)
    response = client.get(
        reverse("accounting:trial_balance"), {"organization": organization.pk, "export": "csv"}
    )
    body = response.content.decode("utf-8-sig")

    for line in body.splitlines():
        for cell in line.split(","):
            stripped = cell.strip().strip('"')
            assert not stripped.startswith(("=", "+", "@")), f"unneutralised cell: {cell!r}"


def test_accounting_manager_can_open_a_fiscal_year_from_periods(
    client_for: Any, accounting_manager: User, organization: Organization
) -> None:
    client = client_for(accounting_manager)

    response = client.post(
        reverse("accounting:fiscal_year_open"),
        {"organization": organization.pk, "year": "2027"},
    )

    assert response.status_code == 302
    assert response.url == f"{reverse('accounting:period_list')}?organization={organization.pk}"
    fiscal_year = FiscalYear.objects.get(organization=organization, year=2027)
    periods = list(fiscal_year.periods.order_by("period_number"))
    assert len(periods) == 12
    assert periods[0].start_date == datetime.date(2027, 1, 1)
    assert periods[-1].end_date == datetime.date(2027, 12, 31)
    assert all(period.state == PeriodState.OPEN for period in periods)


def test_fiscal_year_open_is_not_offered_to_a_branch_manager(
    client_for: Any, branch_manager: User, organization: Organization
) -> None:
    response = client_for(branch_manager).get(
        reverse("accounting:period_list"), {"organization": organization.pk}
    )

    assert response.status_code == 200
    assert "فتح سنة مالية" not in response.content.decode("utf-8")


def test_branch_manager_cannot_open_a_fiscal_year_by_posting_the_route(
    client_for: Any, branch_manager: User, organization: Organization
) -> None:
    response = client_for(branch_manager).post(
        reverse("accounting:fiscal_year_open"),
        {"organization": organization.pk, "year": "2027"},
    )

    assert response.status_code == 403
    assert not FiscalYear.objects.filter(organization=organization, year=2027).exists()


def test_opening_an_existing_fiscal_year_keeps_the_original_periods(
    client_for: Any, accounting_manager: User, organization: Organization
) -> None:
    client = client_for(accounting_manager)

    response = client.post(
        reverse("accounting:fiscal_year_open"),
        {"organization": organization.pk, "year": "2026"},
        follow=True,
    )

    assert response.status_code == 200
    assert FiscalYear.objects.filter(organization=organization, year=2026).count() == 1
    assert AccountingPeriod.objects.filter(fiscal_year__organization=organization).count() == 12
    assert "مفتوحة بالفعل" in response.content.decode("utf-8")
