"""
The command API, over HTTP.

Exercised through the real URL conf and the real auth backend, because the
things most worth checking here are the ones a direct call to the command
layer cannot show: that authentication is actually required, that a submitted
identifier reaches the scoped resolver rather than a queryset, that the error
mapping is the one a client will branch on, and that money leaves the process
as an exact decimal string.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from apps.accounting.models import (
    Account,
    AccountingPeriod,
    CostCenter,
    JournalEntry,
    JournalEntryStatus,
    PeriodState,
)
from apps.accounting.services import close_period, post_entry, resolve_period
from apps.accounting.validators import PostingLine
from apps.organizations.models import Branch, Organization
from apps.users.models import User

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db

ENTRIES = "/api/v1/journal-entries/"
PERIODS = "/api/v1/periods"


def _post(client: Client, url: str, payload: dict[str, Any] | None = None) -> Any:
    return client.post(url, data=json.dumps(payload or {}), content_type="application/json")


def _draft_payload(
    organization: Organization,
    cash: Account,
    sales: Account,
    branch: Branch,
    hall: CostCenter,
    amount: str = "100000",
) -> dict[str, Any]:
    return {
        "organization_id": organization.pk,
        "accounting_date": POSTING_DATE.isoformat(),
        "narration": "cash sale",
        "lines": [
            {"account_id": cash.pk, "branch_id": branch.pk, "debit": amount},
            {
                "account_id": sales.pk,
                "branch_id": branch.pk,
                "credit": amount,
                "cost_center_id": hall.pk,
            },
        ],
    }


class TestAuthenticationIsRequired:
    def test_an_anonymous_caller_gets_no_ledger(self, client: Client) -> None:
        assert client.get(ENTRIES).status_code == 401

    def test_an_anonymous_caller_cannot_create_a_draft(self, client: Client) -> None:
        assert _post(client, ENTRIES, {}).status_code == 401

    def test_health_stays_open(self, client: Client) -> None:
        """The probe a load balancer calls, deliberately unauthenticated."""
        assert client.get("/api/v1/health").status_code == 200


class TestDraftLifecycle:
    def test_create_amend_post(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        client = client_for(accountant)

        created = _post(client, ENTRIES, _draft_payload(organization, cash, sales, branch, hall))
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == JournalEntryStatus.DRAFT
        # A draft holds no entry number: an abandoned one must not burn one.
        assert body["entry_number"] == ""
        entry_id = body["id"]

        amended = client.patch(
            f"{ENTRIES}{entry_id}/",
            data=json.dumps({"narration": "cash sale, corrected"}),
            content_type="application/json",
        )
        assert amended.status_code == 200
        assert amended.json()["narration"] == "cash sale, corrected"

        posted = _post(client, f"{ENTRIES}{entry_id}/post/")
        assert posted.status_code == 200
        assert posted.json()["status"] == JournalEntryStatus.POSTED
        assert posted.json()["entry_number"].startswith("JE-")

    def test_a_posted_entry_cannot_be_amended(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """409, not 422: the request was well formed, the world had moved on."""
        client = client_for(accountant)
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
                PostingLine(
                    account=sales, branch=branch, credit=Decimal("100000"), cost_center=hall
                ),
            ],
            idempotency_key="api-posted",
        )

        response = client.patch(
            f"{ENTRIES}{entry.pk}/",
            data=json.dumps({"narration": "rewritten"}),
            content_type="application/json",
        )

        assert response.status_code == 409
        assert response.json()["code"] == "not_a_draft"
        entry.refresh_from_db()
        assert entry.narration != "rewritten"

    def test_a_posted_entry_cannot_be_deleted(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        client = client_for(accountant)
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
                PostingLine(
                    account=sales, branch=branch, credit=Decimal("100000"), cost_center=hall
                ),
            ],
            idempotency_key="api-posted-delete",
        )

        response = client.delete(f"{ENTRIES}{entry.pk}/")

        assert response.status_code == 409
        assert JournalEntry.objects.filter(pk=entry.pk).exists()

    def test_a_draft_can_be_discarded(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        client = client_for(accountant)
        entry_id = _post(
            client, ENTRIES, _draft_payload(organization, cash, sales, branch, hall)
        ).json()["id"]

        assert client.delete(f"{ENTRIES}{entry_id}/").status_code == 204
        assert not JournalEntry.objects.filter(pk=entry_id).exists()

    def test_an_unbalanced_draft_is_refused_at_posting_not_at_creation(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        A draft is allowed to be unbalanced while it is being written; that is
        what makes it a draft. The rule applies at the moment it becomes a
        journal.
        """
        client = client_for(accountant)
        payload = _draft_payload(organization, cash, sales, branch, hall)
        payload["lines"][1]["credit"] = "90000"

        created = _post(client, ENTRIES, payload)
        assert created.status_code == 201

        posted = _post(client, f"{ENTRIES}{created.json()['id']}/post/")
        assert posted.status_code == 422
        assert posted.json()["code"] == "unbalanced"


class TestReversalOverHttp:
    def test_a_posted_entry_can_be_reversed(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        client = client_for(accountant)
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
                PostingLine(
                    account=sales, branch=branch, credit=Decimal("100000"), cost_center=hall
                ),
            ],
            idempotency_key="api-reverse",
        )

        response = _post(client, f"{ENTRIES}{entry.pk}/reverse/", {"reason": "keyed in twice"})

        assert response.status_code == 201
        body = response.json()
        assert body["reverses_id"] == entry.pk
        # The mirror: debits and credits swapped, magnitudes identical.
        assert body["debit_total"] == "100000.000"
        assert body["credit_total"] == "100000.000"

    def test_reversing_twice_is_a_conflict(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        client = client_for(accountant)
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
                PostingLine(
                    account=sales, branch=branch, credit=Decimal("100000"), cost_center=hall
                ),
            ],
            idempotency_key="api-reverse-twice",
        )

        first = _post(client, f"{ENTRIES}{entry.pk}/reverse/", {"reason": "wrong"})
        second = _post(client, f"{ENTRIES}{entry.pk}/reverse/", {"reason": "wrong"})

        assert first.status_code == 201
        assert second.status_code == 409
        assert second.json()["code"] == "already_reversed"
        assert JournalEntry.objects.filter(reverses=entry).count() == 1

    def test_a_reversal_requires_a_reason(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        client = client_for(accountant)
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
                PostingLine(
                    account=sales, branch=branch, credit=Decimal("100000"), cost_center=hall
                ),
            ],
            idempotency_key="api-reverse-noreason",
        )

        response = _post(client, f"{ENTRIES}{entry.pk}/reverse/", {"reason": "   "})

        assert response.status_code == 422
        assert response.json()["code"] == "reversal_reason_required"


class TestPeriodCommandsOverHttp:
    def test_the_accounting_manager_can_soft_close_close_and_reopen(
        self, accounting_manager: User, client_for: Any, organization: Organization
    ) -> None:
        client = client_for(accounting_manager)
        january = AccountingPeriod.objects.get(
            fiscal_year__organization=organization, period_number=1
        )

        soft = _post(client, f"{PERIODS}/{january.pk}/soft-close/", {"reason": "月末"})
        assert soft.status_code == 200
        assert soft.json()["state"] == PeriodState.SOFT_CLOSED

        closed = _post(client, f"{PERIODS}/{january.pk}/close/", {"reason": "signed off"})
        assert closed.status_code == 200
        assert closed.json()["state"] == PeriodState.CLOSED

        reopened = _post(
            client,
            f"{PERIODS}/{january.pk}/reopen/",
            {"reason": "late supplier credit note"},
        )
        assert reopened.status_code == 200
        assert reopened.json()["state"] == PeriodState.OPEN

    def test_a_branch_accountant_is_refused(
        self, accountant: User, client_for: Any, organization: Organization
    ) -> None:
        client = client_for(accountant)
        january = AccountingPeriod.objects.get(
            fiscal_year__organization=organization, period_number=1
        )

        response = _post(client, f"{PERIODS}/{january.pk}/close/", {"reason": "mine now"})

        assert response.status_code == 403
        january.refresh_from_db()
        assert january.state == PeriodState.OPEN

    def test_closing_out_of_order_is_refused(
        self, accounting_manager: User, client_for: Any, organization: Organization
    ) -> None:
        client = client_for(accounting_manager)
        february = AccountingPeriod.objects.get(
            fiscal_year__organization=organization, period_number=2
        )

        response = _post(client, f"{PERIODS}/{february.pk}/close/", {"reason": "early"})

        assert response.status_code == 422
        assert response.json()["code"] == "close_out_of_order"

    def test_reopen_without_a_reason_is_a_422_not_a_500(
        self, accounting_manager: User, client_for: Any, organization: Organization
    ) -> None:
        client = client_for(accounting_manager)
        january = AccountingPeriod.objects.get(
            fiscal_year__organization=organization, period_number=1
        )
        close_period(period=january, reason="signed off")

        response = _post(client, f"{PERIODS}/{january.pk}/reopen/", {"reason": "  "})

        assert response.status_code == 422
        assert response.json()["code"] == "reopen_reason_required"


class TestScopeOverHttp:
    def test_a_foreign_organization_id_in_the_body_is_a_403(
        self,
        rival_accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        403 and not 404. The caller's authority is what is insufficient; a 404
        would send an honest client hunting for a record that is there.
        """
        client = client_for(rival_accountant)

        response = _post(client, ENTRIES, _draft_payload(organization, cash, sales, branch, hall))

        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"

    def test_a_foreign_entry_id_in_the_path_is_a_403(
        self,
        rival_accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
                PostingLine(
                    account=sales, branch=branch, credit=Decimal("100000"), cost_center=hall
                ),
            ],
            idempotency_key="api-foreign",
        )
        client = client_for(rival_accountant)

        assert client.get(f"{ENTRIES}{entry.pk}/").status_code == 403

    def test_the_list_shows_only_what_the_caller_may_see(
        self,
        rival_accountant: User,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
                PostingLine(
                    account=sales, branch=branch, credit=Decimal("100000"), cost_center=hall
                ),
            ],
            idempotency_key="api-list",
        )

        assert len(client_for(accountant).get(ENTRIES).json()) == 1
        assert client_for(rival_accountant).get(ENTRIES).json() == []

    def test_a_cashier_cannot_list_the_ledger(self, cashier: User, client_for: Any) -> None:
        assert client_for(cashier).get(ENTRIES).status_code == 403


class TestExactDecimalTransport:
    """
    Money crosses the boundary as strings, in both directions.

    JSON has one numeric type and it is binary floating point. A bare
    `1250.001` in a request body has already been through a float before any
    Python code sees it, and 0.1 + 0.2 is the standard demonstration of what
    that costs. A string cannot be rounded by a parser.
    """

    def test_stored_precision_survives_the_round_trip(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        client = client_for(accountant)
        payload = _draft_payload(organization, cash, sales, branch, hall, amount="1250.001")

        created = _post(client, ENTRIES, payload)
        body = created.json()

        assert body["lines"][0]["debit"] == "1250.001"
        assert body["lines"][1]["credit"] == "1250.001"
        assert body["debit_total"] == "1250.001"

    def test_the_stored_value_is_the_exact_decimal(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        client = client_for(accountant)
        entry_id = _post(
            client,
            ENTRIES,
            _draft_payload(organization, cash, sales, branch, hall, amount="1250.001"),
        ).json()["id"]

        line = JournalEntry.objects.get(pk=entry_id).lines.order_by("line_number").first()
        assert line is not None
        assert line.debit == Decimal("1250.001")

    def test_amounts_are_serialized_as_strings_not_numbers(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        Checked against the raw JSON, because `json.loads` would happily turn
        an unquoted 1250.001 into a float and the assertion would still read
        as if it had passed.
        """
        client = client_for(accountant)
        response = _post(
            client,
            ENTRIES,
            _draft_payload(organization, cash, sales, branch, hall, amount="1250.001"),
        )

        raw = response.content.decode("utf-8")
        assert '"debit": "1250.001"' in raw or '"debit":"1250.001"' in raw
        assert '"debit": 1250.001' not in raw
        assert '"debit":1250.001' not in raw

    def test_amounts_are_never_grouped_or_localised(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        The API is a technical identity, not a display surface. A thousands
        separator here would be re-parsed by a client, and a decimal comma
        under Arabic would be ambiguous.
        """
        client = client_for(accountant)
        body = _post(
            client,
            ENTRIES,
            _draft_payload(organization, cash, sales, branch, hall, amount="1250000.500"),
        ).json()

        assert body["lines"][0]["debit"] == "1250000.500"
        assert "," not in body["lines"][0]["debit"]

    def test_a_malformed_amount_is_a_422(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        client = client_for(accountant)
        payload = _draft_payload(organization, cash, sales, branch, hall)
        payload["lines"][0]["debit"] = "not money"

        assert _post(client, ENTRIES, payload).status_code == 422


class TestSoftClosedPeriodOverHttp:
    def test_a_branch_accountant_cannot_post_into_a_soft_closed_period(
        self,
        accountant: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """
        They may post, and the period is soft-closed, and overriding that is
        not theirs to do — it needs `post_soft_closed_adjustment` over the
        organization.
        """
        from apps.accounting.services import soft_close_period

        client = client_for(accountant)
        entry_id = _post(
            client, ENTRIES, _draft_payload(organization, cash, sales, branch, hall)
        ).json()["id"]

        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        soft_close_period(period=period, reason="month end")

        response = _post(client, f"{ENTRIES}{entry_id}/post/", {"reason": "let me in"})

        assert response.status_code == 403
        assert JournalEntry.objects.get(pk=entry_id).status == JournalEntryStatus.DRAFT

    def test_the_accounting_manager_can_with_a_reason(
        self,
        accountant: User,
        accounting_manager: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        from apps.accounting.services import soft_close_period
        from apps.core.models import AuditAction, AuditEvent

        entry_id = _post(
            client_for(accountant),
            ENTRIES,
            _draft_payload(organization, cash, sales, branch, hall),
        ).json()["id"]

        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        soft_close_period(period=period, reason="month end")

        response = _post(
            client_for(accounting_manager),
            f"{ENTRIES}{entry_id}/post/",
            {"reason": "supplier credit note agreed after cut-off"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == JournalEntryStatus.POSTED

        # The override is recorded as its own fact, separately from the posting.
        override = AuditEvent.objects.filter(action=AuditAction.PERMISSION_OVERRIDE).latest(
            "occurred_at"
        )
        assert override.metadata["permission"] == "accounting.post_soft_closed_adjustment"
        assert override.reason == "supplier credit note agreed after cut-off"
        assert override.actor_id == accounting_manager.pk

    def test_without_a_reason_it_is_refused(
        self,
        accountant: User,
        accounting_manager: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        from apps.accounting.services import soft_close_period

        entry_id = _post(
            client_for(accountant),
            ENTRIES,
            _draft_payload(organization, cash, sales, branch, hall),
        ).json()["id"]

        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        soft_close_period(period=period, reason="month end")

        response = _post(client_for(accounting_manager), f"{ENTRIES}{entry_id}/post/")

        assert response.status_code == 422
        assert response.json()["code"] == "soft_closed_reason_required"

    def test_nothing_posts_into_a_closed_period(
        self,
        accountant: User,
        accounting_manager: User,
        client_for: Any,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        """CLOSED is absolute. Reopening is a separate, audited act."""
        entry_id = _post(
            client_for(accountant),
            ENTRIES,
            _draft_payload(organization, cash, sales, branch, hall),
        ).json()["id"]

        period = resolve_period(organization=organization, accounting_date=POSTING_DATE)
        for number in range(1, period.period_number + 1):
            close_period(
                period=AccountingPeriod.objects.get(
                    fiscal_year=period.fiscal_year, period_number=number
                ),
                reason="year end",
            )

        response = _post(
            client_for(accounting_manager),
            f"{ENTRIES}{entry_id}/post/",
            {"reason": "please"},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "period_closed"
