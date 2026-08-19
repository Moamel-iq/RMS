"""
Phase 0 exit: the foundations, working together.

Every other test in this repository proves one package behaves. This one
proves they cooperate — that an organization created by one service is
reachable by another's scope resolver, that the chart seeded by a management
command posts through the accounting kernel, that the audit trail written
underneath a command names the user the permission was checked against, and
that the period lifecycle still refuses what it is supposed to refuse once
real entries exist inside it.

Nothing here reaches for the ORM to set up state the application would create
through a service. A smoke test that writes rows directly proves the rows can
exist, which was never in doubt; what needs proving is that the real path
works end to end.
"""

from __future__ import annotations

import datetime
import json
from datetime import time
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client

from apps.accounting.commands import (
    LineInput,
    close_accounting_period,
    create_draft_entry,
    post_journal_entry,
    read_journal_entry,
    reopen_accounting_period,
    reverse_journal_entry,
    soft_close_accounting_period,
)
from apps.accounting.models import (
    Account,
    AccountingPeriod,
    CostCenter,
    JournalEntryStatus,
    PeriodState,
    SourceEvent,
)
from apps.accounting.selectors import trial_balance_totals
from apps.accounting.services import configure_accounting, open_fiscal_year, resolve_period
from apps.core.models import AuditAction, AuditEvent
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    grant_organization_access,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

YEAR = 2026
POSTING_DATE = datetime.date(YEAR, 3, 15)
PASSWORD = "pw-not-real-1234"


@pytest.fixture
def foundation() -> dict[str, Any]:
    """
    A whole Phase 0 world, built the way the application builds one.

    Organization, accounting configuration, fiscal calendar, branch, chart of
    accounts, cost centres, and two users holding genuinely different
    authority. Every step is a service call.
    """
    organization = create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=YEAR)

    branch = create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9, 0),
    )

    # Reference data, seeded the way a real deployment seeds it.
    call_command("seed_units", verbosity=0)
    call_command("seed_chart_of_accounts", organization="KM", verbosity=0)

    accountant = User.objects.create_user(username="huda", password=PASSWORD)
    grant_branch_access(user=accountant, branch=branch, role=Role.ACCOUNTANT)

    manager = User.objects.create_user(username="samir", password=PASSWORD)
    grant_organization_access(user=manager, organization=organization, role=Role.ACCOUNTING_MANAGER)

    return {
        "organization": organization,
        "branch": branch,
        "accountant": User.objects.get(pk=accountant.pk),
        "manager": User.objects.get(pk=manager.pk),
        "cash": Account.objects.get(organization=organization, code="1-01-01-001"),
        "sales": Account.objects.get(organization=organization, code="4-01-01-001"),
        "hall": CostCenter.objects.get(organization=organization, code="HALL"),
    }


class TestTheFoundationsCooperate:
    def test_the_whole_path_from_an_empty_database_to_a_closed_period(
        self, foundation: dict[str, Any]
    ) -> None:
        organization: Organization = foundation["organization"]
        branch: Branch = foundation["branch"]
        accountant: User = foundation["accountant"]
        manager: User = foundation["manager"]
        cash: Account = foundation["cash"]
        sales: Account = foundation["sales"]
        hall: CostCenter = foundation["hall"]

        # --- 1. Seeded reference data -------------------------------------
        assert UnitOfMeasure.objects.count() == 10
        # 63: the Phase 0 chart of 46, plus the Task 1.3 inventory and
        # opening-equity branches (1-03…, 3-02…), plus Task 1.4's GRNI
        # liability and consumption leaves (2-01-02…, 5-01-02…), plus
        # Task 1.5's transfer-shortage loss branch (6-02…), plus Task 2.12's
        # purchase price variance clearing leaf and its group (8-01-03…),
        # plus Task 2.13's supplier return clearing (8-01-04…) and purchase
        # return variance (7-09-04…) leaves and their groups, plus Task
        # 2.15's supplier advance branch (1-04…).
        # Phase 4 added seventeen more: the returns, card-clearing,
        # delivery-receivable, commission, other-fee, settlement-bank,
        # settlement-variance and cash-over-short leaves the eleven Sales roles
        # map to, and their groups. Task 4.0 put them in seed_chart_of_accounts
        # and left this count behind, so this gate has been red since — which is
        # the only way anybody was ever going to find out.
        assert Account.objects.filter(organization=organization).count() == 94
        assert CostCenter.objects.filter(organization=organization).count() == 6
        assert AccountingPeriod.objects.filter(fiscal_year__organization=organization).count() == 12

        # --- 2. A draft, built by the accountant --------------------------
        draft = create_draft_entry(
            actor=accountant,
            organization_id=organization.pk,
            accounting_date=POSTING_DATE,
            narration="cash sale, hall",
            lines=[
                LineInput(account_id=cash.pk, branch_id=branch.pk, debit=Decimal("1250.001")),
                LineInput(
                    account_id=sales.pk,
                    branch_id=branch.pk,
                    credit=Decimal("1250.001"),
                    cost_center_id=hall.pk,
                ),
            ],
        )
        assert draft.status == JournalEntryStatus.DRAFT
        # A draft consumes no journal number: numbering is gapless.
        assert draft.entry_number == ""

        # --- 3. Posted --------------------------------------------------
        posted = post_journal_entry(actor=accountant, entry_id=draft.pk)
        assert posted.status == JournalEntryStatus.POSTED
        assert posted.entry_number == f"JE-{YEAR}-000001"
        assert posted.posted_by_id == accountant.pk

        # --- 4. Stored amounts are exact Decimals -------------------------
        lines = list(posted.lines.order_by("line_number"))
        assert lines[0].debit == Decimal("1250.001")
        assert lines[1].credit == Decimal("1250.001")
        assert [type(line.debit) for line in lines] == [Decimal, Decimal]
        debits, credits = trial_balance_totals(organization=organization)
        assert debits == credits == Decimal("1250.001")

        # --- 5. Audited, naming the authorized actor ----------------------
        event = AuditEvent.objects.filter(
            action=AuditAction.POSTED, target_id=str(posted.pk)
        ).latest("occurred_at")
        assert event.actor_id == accountant.pk
        assert event.branch_id == branch.pk
        assert event.new_state is not None
        # Decimals reach the audit trail as strings, never as floats.
        assert isinstance(event.metadata["entry_number"], str)

        # --- 6. Reversed, and the pair nets to zero -----------------------
        reversal = reverse_journal_entry(
            actor=accountant, entry_id=posted.pk, reason="customer walked out"
        )
        assert reversal.reverses_id == posted.pk
        posted.refresh_from_db()
        assert posted.status == JournalEntryStatus.REVERSED

        debits, credits = trial_balance_totals(organization=organization)
        assert debits == credits == Decimal("2500.002")
        assert sum(
            (line.debit - line.credit for line in posted.lines.all()),
            Decimal("0"),
        ) + sum(
            (line.debit - line.credit for line in reversal.lines.all()),
            Decimal("0"),
        ) == Decimal("0")

        # --- 7. The period lifecycle, by the manager ----------------------
        march = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        soft_close_accounting_period(actor=manager, period_id=march.pk, reason="operations done")
        march.refresh_from_db()
        assert march.state == PeriodState.SOFT_CLOSED

        # Closing is chronological, so January and February go first.
        for number in (1, 2):
            close_accounting_period(
                actor=manager,
                period_id=AccountingPeriod.objects.get(
                    fiscal_year=march.fiscal_year, period_number=number
                ).pk,
                reason="signed off",
            )
        close_accounting_period(actor=manager, period_id=march.pk, reason="signed off")
        march.refresh_from_db()
        assert march.state == PeriodState.CLOSED

        # --- 8. And reopening is the Accounting Manager's alone -----------
        from django.core.exceptions import PermissionDenied

        with pytest.raises(PermissionDenied):
            reopen_accounting_period(actor=accountant, period_id=march.pk, reason="let me back in")

        reopened = reopen_accounting_period(
            actor=manager, period_id=march.pk, reason="late credit note"
        )
        assert reopened.state == PeriodState.OPEN

        reopen_event = AuditEvent.objects.filter(action=AuditAction.PERIOD_REOPENED).latest(
            "occurred_at"
        )
        assert reopen_event.actor_id == manager.pk
        assert reopen_event.reason == "late credit note"

    def test_the_same_path_over_http(self, foundation: dict[str, Any]) -> None:
        """
        The API leg, because the command layer being correct says nothing
        about whether the endpoints are wired to it.
        """
        organization: Organization = foundation["organization"]
        branch: Branch = foundation["branch"]
        cash: Account = foundation["cash"]
        sales: Account = foundation["sales"]
        hall: CostCenter = foundation["hall"]

        client = Client()
        client.force_login(foundation["accountant"])

        created = client.post(
            "/api/v1/journal-entries/",
            data=json.dumps(
                {
                    "organization_id": organization.pk,
                    "accounting_date": POSTING_DATE.isoformat(),
                    "narration": "delivery sale",
                    "lines": [
                        {
                            "account_id": cash.pk,
                            "branch_id": branch.pk,
                            "debit": "98750.500",
                        },
                        {
                            "account_id": sales.pk,
                            "branch_id": branch.pk,
                            "credit": "98750.500",
                            "cost_center_id": hall.pk,
                        },
                    ],
                }
            ),
            content_type="application/json",
        )
        assert created.status_code == 201
        entry_id = created.json()["id"]

        posted = client.post(
            f"/api/v1/journal-entries/{entry_id}/post/",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert posted.status_code == 200
        body = posted.json()
        assert body["status"] == JournalEntryStatus.POSTED

        # Exact decimal transport, verified against the raw bytes.
        assert body["debit_total"] == "98750.500"
        assert '"debit": "98750.500"' in posted.content.decode("utf-8")

        # And the stored value matches what was transported.
        entry = read_journal_entry(actor=foundation["accountant"], entry_id=entry_id)
        assert entry.lines.order_by("line_number").first().debit == Decimal(  # type: ignore[union-attr]
            "98750.500"
        )

    def test_seeding_survives_a_console_that_cannot_render_arabic(self) -> None:
        """
        Regression: a fresh Windows install seeded nothing.

        Arabic is the source language, so `seed_units` prints Arabic names. A
        Windows console defaults to a legacy code page — cp1252 here — which
        cannot encode a single Arabic character, and the command is
        `@transaction.atomic`. The `UnicodeEncodeError` raised while *printing*
        the first unit rolled back every unit, so `manage.py seed_units` ended
        with a traceback and an empty table.

        No test caught it because the development database already had units
        from an earlier run, and pytest's captured stdout is UTF-8. It took a
        genuinely empty database on a real console.
        """

        class LegacyConsole:
            """Refuses Arabic, and cannot be reconfigured out of it."""

            encoding = "cp1252"

            def __init__(self) -> None:
                self.written: list[str] = []

            def write(self, text: str) -> None:
                text.encode(self.encoding)  # raises on Arabic, as cp1252 does
                self.written.append(text)

            def flush(self) -> None:
                pass

            def isatty(self) -> bool:
                return False

        console = LegacyConsole()
        call_command("seed_units", stdout=console)

        # The data is the deliverable. It landed.
        assert UnitOfMeasure.objects.count() == 10
        assert UnitOfMeasure.objects.filter(code="KG", is_base=True).exists()
        # And the summary line still got through.
        assert any("Units seeded" in line for line in console.written)

    def test_an_upstream_module_can_post_idempotently(self, foundation: dict[str, Any]) -> None:
        """
        The contract Phase 1 depends on: a module retries, and the ledger does
        not double.
        """
        from apps.accounting.services import post_entry
        from apps.accounting.validators import PostingLine

        organization: Organization = foundation["organization"]

        def attempt() -> Any:
            return post_entry(
                organization=organization,
                accounting_date=POSTING_DATE,
                lines=[
                    PostingLine(
                        account=foundation["cash"],
                        branch=foundation["branch"],
                        debit=Decimal("40000"),
                    ),
                    PostingLine(
                        account=foundation["sales"],
                        branch=foundation["branch"],
                        credit=Decimal("40000"),
                        cost_center=foundation["hall"],
                    ),
                ],
                idempotency_key="purchase-invoice-145",
                source_document_type="PURCHASE_INVOICE",
                source_document_id="145",
                source_event=SourceEvent.POSTED,
            )

        first = attempt()
        second = attempt()
        third = attempt()

        assert first.pk == second.pk == third.pk
        debits, credits = trial_balance_totals(organization=organization)
        assert debits == credits == Decimal("40000.000")
