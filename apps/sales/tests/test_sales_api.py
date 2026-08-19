"""
The Sales API: exact decimals, command verbs, 404 before 403, cost omitted.

Four claims, and each is one somebody would otherwise discover from a client:

* **Every decimal crosses as a quoted string, both directions.** Asserted by
  walking the whole payload for a JSON *number* under a money key rather than by
  checking one field, because the one that regresses is always the one nobody
  listed.
* **A posted document has no PATCH and no DELETE.** Asserted as a 405, because
  a route that answered would be the API contradicting a database trigger.
* **Out of scope is 404.** A 403 would confirm the document exists, and
  `public_id` is a UUID but an organization id is not.
* **Cost keys are absent, not null.** `"food_cost" not in payload`, never
  `payload["food_cost"] is None` — the two render identically to a client and
  say completely different things.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from apps.sales.models import SalesDay, SalesDayStatus
from apps.users.models import User

pytestmark = pytest.mark.django_db

BASE = "/api/v1/sales"

#: Keys whose value must never arrive as a JSON number. Deliberately broad: a
#: key added to a schema and forgotten here is caught by the walk below only if
#: its name is in this set, so the set is the part that has to stay generous.
MONEY_KEYS = frozenset(
    {
        "allocated_amount",
        "amount",
        "application_discount",
        "application_sales",
        "balance",
        "card_sales",
        "cash_sales",
        "commission",
        "commission_amount",
        "commission_gap",
        "commission_percent",
        "counted_amount",
        "counted_cash",
        "credit",
        "customer_charge",
        "debit",
        "declared",
        "declared_amount",
        "derived",
        "difference",
        "discount_amount",
        "discount_percent",
        "expected",
        "expected_amount",
        "expected_cash",
        "food_cost",
        "fixed_fee_per_order",
        "gross",
        "gross_amount",
        "gross_profit",
        "net",
        "net_amount",
        "net_application",
        "net_card",
        "net_cash",
        "net_revenue",
        "opening_float",
        "other_fee_amount",
        "other_fees",
        "quantity",
        "remitted",
        "remitted_amount",
        "restaurant_discount",
        "returns_gross",
        "share",
        "statement",
        "statement_amount",
        "unit_price",
        "variance_amount",
    }
)


def _numeric_money(node: Any, path: str = "") -> list[str]:
    """Every money key in a payload that arrived as a JSON number."""
    problems: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in MONEY_KEYS and isinstance(value, int | float) and not isinstance(value, bool):
                problems.append(f"{path}.{key} = {value!r}")
            problems += _numeric_money(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            problems += _numeric_money(value, f"{path}[{index}]")
    return problems


def _get(client: Client, path: str) -> Any:
    response = client.get(f"{BASE}{path}")
    assert response.status_code == 200, (path, response.content[:400])
    payload = json.loads(response.content)
    assert not _numeric_money(payload), (path, _numeric_money(payload))
    return payload


def test_every_read_answers_and_carries_no_json_number(
    scenario: dict[str, Any], accounting_manager: User, client_for: Callable[[User], Client]
) -> None:
    client = client_for(accounting_manager)
    day = scenario["day"]
    for path in (
        "/menu-items",
        "/menu-prices",
        "/channels",
        "/applications",
        "/agreements",
        "/discounts",
        "/days",
        "/adjustments",
        "/settlements",
        "/shifts",
        f"/days/{day.public_id}",
        f"/adjustments/{scenario['adjustment'].public_id}",
        f"/settlements/{scenario['settlement'].public_id}",
        f"/shifts/{scenario['shift'].public_id}",
        f"/applications/{scenario['application'].public_id}/receivable",
        "/reports/daily-reconciliation?date_from=2026-08-01&date_to=2026-08-31",
    ):
        _get(client, path)


def test_the_day_payload_reconstructs_its_own_totals(
    scenario: dict[str, Any], accounting_manager: User, client_for: Callable[[User], Client]
) -> None:
    """
    The strings a client receives are exact, not rounded on the way out.

    Parsed back into `Decimal` and compared with the stored figures: a payload
    that lost the third decimal place would look right and reconcile to a
    different number.
    """
    client = client_for(accounting_manager)
    payload = _get(client, f"/days/{scenario['day'].public_id}")
    assert payload["status"] == SalesDayStatus.POSTED
    assert Decimal(payload["gross"]) == Decimal("100000.000")
    lines = payload["lines"]
    assert len(lines) == 2
    for line in lines:
        assert Decimal(line["gross_amount"]) == (
            Decimal(line["quantity"]) * Decimal(line["unit_price"])
        )


def test_the_settlement_payload_keeps_the_three_figures_apart(
    scenario: dict[str, Any], accounting_manager: User, client_for: Callable[[User], Client]
) -> None:
    """
    Expected, statement and remitted are three keys, never one net variance.

    Which two of the three agree is the diagnosis (ADR-028 §7); a payload that
    reported only the gap would answer "how much" and never "who owes an
    explanation".
    """
    client = client_for(accounting_manager)
    payload = _get(client, f"/settlements/{scenario['settlement'].public_id}")
    three_way = payload["three_way"]
    assert Decimal(three_way["expected"]) == Decimal("51000.000")
    assert Decimal(three_way["statement"]) == Decimal("50000.000")
    assert Decimal(three_way["remitted"]) == Decimal("49500.000")
    assert Decimal(three_way["unexplained_statement"]) == Decimal("0")
    assert Decimal(three_way["unexplained_remittance"]) == Decimal("0")
    # Accrued 9,000 against a statement claiming 8,000 — reported, never posted.
    assert Decimal(three_way["commission_gap"]) == Decimal("1000.000")


def test_cost_is_a_separate_route_and_carries_no_key_on_the_dashboard(
    scenario: dict[str, Any],
    accounting_manager: User,
    manager: User,
    client_for: Callable[[User], Client],
) -> None:
    """
    Absent, not null — and the absence is **structural**.

    A response schema fills an unset optional field with `null`, and a null food
    cost says a number exists and that this caller is not trusted with it. So
    cost is a route of its own: `/dashboard` carries no cost key for anybody,
    and `/dashboard/cost` answers 403 without `view_sales_cost`. There is
    nowhere for a null to appear and be read as a zero.
    """
    from django.contrib.auth.models import Permission

    from apps.organizations.permissions import group_for_role

    organization = scenario["organization"]
    query = f"?organization_id={organization.pk}&date_from=2026-08-01&date_to=2026-08-31"

    dashboard = _get(client_for(accounting_manager), f"/dashboard{query}")
    for key in ("food_cost", "gross_profit", "food_cost_percent", "uncosted_lines"):
        assert key not in dashboard

    cost = _get(client_for(accounting_manager), f"/dashboard/cost{query}")
    assert Decimal(cost["food_cost"]) == Decimal("0")
    # Nothing was costed at zero: the lines have no snapshot behind them and say
    # so, which is a different claim from "the food was free".
    assert cost["costed_lines"] == 0
    assert cost["uncosted_lines"] == 2
    assert cost["is_complete"] is False

    group = group_for_role("MANAGER")
    group.permissions.remove(
        Permission.objects.get(content_type__app_label="sales", codename="view_sales_cost")
    )
    client = client_for(User.objects.get(pk=manager.pk))
    assert client.get(f"{BASE}/dashboard/cost{query}").status_code == 403
    # The rest of the dashboard is untouched: cost is a separate permission, and
    # a manager who may not know a plate's cost still reads what it sold for.
    without_cost = _get(client, f"/dashboard{query}")
    assert Decimal(without_cost["gross"]) == Decimal("100000.000")


def test_an_unknown_document_is_a_404_and_not_a_403(
    scenario: dict[str, Any], outsider: User, client_for: Callable[[User], Client]
) -> None:
    """
    A foreign document and an absent one answer identically.

    Answering 403 for the first would confirm the record is real, which turns an
    id-guessing loop into a census of another organization's documents.
    """
    client = client_for(outsider)
    for path in (
        f"/days/{scenario['day'].public_id}",
        f"/adjustments/{scenario['adjustment'].public_id}",
        f"/settlements/{scenario['settlement'].public_id}",
        f"/shifts/{scenario['shift'].public_id}",
        "/days/00000000-0000-0000-0000-000000000000",
        "/days/not-a-uuid",
    ):
        response = client.get(f"{BASE}{path}")
        assert response.status_code == 404, (path, response.status_code)
        assert json.loads(response.content)["code"] == "not_found"


def test_a_posted_day_offers_no_patch_and_no_delete(
    scenario: dict[str, Any], accounting_manager: User, client_for: Callable[[User], Client]
) -> None:
    """
    405, because the route does not exist at all.

    A verb that answered here would be the API contradicting `0006`'s freeze
    trigger, and the trigger would win — after the caller had been told it
    worked.
    """
    client = client_for(accounting_manager)
    day = scenario["day"]
    assert client.patch(f"{BASE}/days/{day.public_id}").status_code == 405
    assert client.delete(f"{BASE}/days/{day.public_id}").status_code == 405
    assert (
        client.delete(f"{BASE}/adjustments/{scenario['adjustment'].public_id}").status_code == 405
    )


def test_a_cashier_may_draft_a_day_and_may_not_post_it(
    scenario: dict[str, Any],
    cashier: User,
    branch: Any,
    organization: Any,
    client_for: Callable[[User], Client],
) -> None:
    """
    The separation checkpoint 3 built, asserted through the API rather than the
    screen: a till may type the numbers and may not commit them.
    """
    client = client_for(cashier)
    created = client.post(
        f"{BASE}/days",
        data=json.dumps(
            {
                "organization_id": organization.pk,
                "branch_id": branch.pk,
                "business_date": "2026-08-20",
                "notes": "",
            }
        ),
        content_type="application/json",
    )
    assert created.status_code == 201, created.content[:400]
    public_id = json.loads(created.content)["public_id"]

    line = client.post(
        f"{BASE}/days/{public_id}/lines",
        data=json.dumps(
            {
                "menu_item_id": scenario["menu_item"].pk,
                "channel_id": scenario["hall"].pk,
                "quantity": "2.000",
                "order_count": 2,
            }
        ),
        content_type="application/json",
    )
    assert line.status_code == 201, line.content[:400]

    submitted = client.post(f"{BASE}/days/{public_id}/submit")
    assert submitted.status_code == 200

    refused = client.post(f"{BASE}/days/{public_id}/post")
    assert refused.status_code == 403
    assert json.loads(refused.content)["code"] == "forbidden"
    assert SalesDay.objects.get(public_id=public_id).status == SalesDayStatus.SUBMITTED


def test_posting_a_posted_day_is_a_409_and_not_a_422(
    scenario: dict[str, Any], accounting_manager: User, client_for: Callable[[User], Client]
) -> None:
    """
    `already_posted` is a state conflict, not a malformed request.

    A client retrying a 422 forever would be right to be confused by "this day
    is already posted": nothing about the request is wrong, the world has moved.
    """
    client = client_for(accounting_manager)
    response = client.post(f"{BASE}/days/{scenario['day'].public_id}/post")
    assert response.status_code == 409
    assert json.loads(response.content)["code"] == "already_posted"


def test_approving_your_own_closing_is_a_409(
    scenario: dict[str, Any],
    organization: Any,
    branch: Any,
    cashier: User,
    manager: User,
    client_for: Callable[[User], Client],
) -> None:
    """
    Maker-checker over the API, refused with the code the config maps to 409.

    Nothing about the request is malformed — the fix is a second person, which
    is why it is a conflict rather than something the caller can re-send.
    """
    from decimal import Decimal as _Decimal

    from apps.sales.day_services import (
        add_sales_line,
        create_sales_day,
        submit_sales_day,
    )
    from apps.sales.posting import post_sales_day
    from apps.sales.shift_services import (
        close_cashier_shift,
        open_cashier_shift,
        set_tender_count,
    )

    day = create_sales_day(
        organization=organization,
        branch=branch,
        business_date=scenario["day"].business_date.replace(day=11),
        actor=manager,
    )
    add_sales_line(
        day=day, menu_item=scenario["menu_item"], channel=scenario["hall"], quantity=_Decimal("1")
    )
    submit_sales_day(day=day, actor=manager)
    day = post_sales_day(day=day, actor=manager)

    shift = open_cashier_shift(
        organization=organization,
        branch=branch,
        business_date=day.business_date,
        cashier=cashier,
        opening_float=_Decimal("0"),
        actor=manager,
    )
    set_tender_count(shift=shift, tender="CASH", counted_amount=_Decimal("10000"), actor=manager)
    close_cashier_shift(shift=shift, sales_day=day, actor=manager)

    response = client_for(manager).post(f"{BASE}/shifts/{shift.public_id}/approve")
    assert response.status_code == 409
    assert json.loads(response.content)["code"] == "approver_is_the_closer"


def test_a_malformed_decimal_is_a_422_naming_the_field(
    scenario: dict[str, Any],
    organization: Any,
    branch: Any,
    manager: User,
    client_for: Callable[[User], Client],
) -> None:
    """A number the caller can fix by re-sending, which is what 422 means."""
    client = client_for(manager)
    created = client.post(
        f"{BASE}/days",
        data=json.dumps(
            {
                "organization_id": organization.pk,
                "branch_id": branch.pk,
                "business_date": "2026-08-21",
            }
        ),
        content_type="application/json",
    )
    public_id = json.loads(created.content)["public_id"]

    response = client.post(
        f"{BASE}/days/{public_id}/lines",
        data=json.dumps(
            {
                "menu_item_id": scenario["menu_item"].pk,
                "channel_id": scenario["hall"].pk,
                "quantity": "not-a-number",
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 422
    body = json.loads(response.content)
    assert body["code"] == "invalid_decimal"
    assert "quantity" in body["message"]


def _draft_settlement(scenario: dict[str, Any], actor: User) -> Any:
    """A second, still-draft settlement — an adjustment needs one."""
    import datetime

    from apps.sales.settlement_services import create_settlement

    return create_settlement(
        organization=scenario["organization"],
        branch=scenario["branch"],
        delivery_application=scenario["application"],
        period_start=datetime.date(2026, 8, 15),
        period_end=datetime.date(2026, 8, 20),
        business_date=datetime.date(2026, 8, 20),
        statement_reference="SCN/STMT-02",
        statement_date=datetime.date(2026, 8, 20),
        statement_amount=Decimal("1000"),
        remitted_amount=Decimal("500"),
        statement_commission_amount=Decimal("100"),
        remittance_destination="BANK",
        evidence_reference="SCN/EVIDENCE-02",
        actor=actor,
    )


def _unexplained_claim(approver_id: int) -> str:
    return json.dumps(
        {
            "leg": "REMITTANCE",
            "reason": "UNEXPLAINED_APPROVED",
            "amount": "-500.000",
            "explanation": "لا تفسير.",
            "approver_id": approver_id,
        }
    )


def test_a_settlement_approver_must_be_able_to_approve_settlements_here(
    scenario: dict[str, Any],
    accounting_manager: User,
    outsider: User,
    cashier: User,
    client_for: Callable[[User], Client],
) -> None:
    """
    The audit finding this test exists for.

    `approver_id` was resolved with a global `User.objects.filter(pk=...)`, so
    the caller could stamp any active user in the database as having approved an
    unexplained settlement variance — the Owner of another organization, or an
    id they guessed. `UNEXPLAINED_APPROVED` is the one place ADR-028 §7 lets a
    difference nobody can explain reach the ledger, and the name on it was the
    entire control.
    """
    settlement = _draft_settlement(scenario, accounting_manager)
    client = client_for(accounting_manager)
    path = f"{BASE}/settlements/{settlement.public_id}/adjustments"

    for stranger in (outsider, cashier):
        response = client.post(
            path, data=_unexplained_claim(stranger.pk), content_type="application/json"
        )
        # 422, not 403: the *caller* is permitted here and the payload is what
        # is wrong, which they fix by naming somebody who may actually approve.
        assert response.status_code == 422, (stranger.username, response.content[:300])
        assert json.loads(response.content)["code"] == "approver_required"

    assert settlement.adjustments.count() == 0


def test_a_nonexistent_approver_is_indistinguishable_from_a_foreign_one(
    scenario: dict[str, Any],
    accounting_manager: User,
    outsider: User,
    client_for: Callable[[User], Client],
) -> None:
    """
    The endpoint was a cross-tenant user-id oracle: an existing foreign id
    returned 201 and a nonexistent one returned an error, so a caller could
    enumerate which user ids exist in other organizations.
    """
    settlement = _draft_settlement(scenario, accounting_manager)
    client = client_for(accounting_manager)
    path = f"{BASE}/settlements/{settlement.public_id}/adjustments"

    missing = client.post(
        path, data=_unexplained_claim(10_000_000), content_type="application/json"
    )
    foreign = client.post(
        path, data=_unexplained_claim(outsider.pk), content_type="application/json"
    )
    assert missing.status_code == foreign.status_code
    assert json.loads(missing.content) == json.loads(foreign.content)


def test_an_approver_who_may_settle_here_is_accepted(
    scenario: dict[str, Any], accounting_manager: User, client_for: Callable[[User], Client]
) -> None:
    """The check refuses forgery, not approval. The legitimate path still works."""
    settlement = _draft_settlement(scenario, accounting_manager)
    response = client_for(accounting_manager).post(
        f"{BASE}/settlements/{settlement.public_id}/adjustments",
        data=_unexplained_claim(accounting_manager.pk),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content[:300]
    adjustment = settlement.adjustments.get()
    assert adjustment.approved_by == accounting_manager
    assert adjustment.approved_at is not None


def test_the_router_is_registered_under_the_versioned_prefix() -> None:
    """One line in `config/api.py`, and this is the assertion behind it."""
    from config.api import api

    paths = {
        f"{prefix}{path}"
        for prefix, router in api._routers  # noqa: SLF001 - the registry has no public read
        for path in router.path_operations
    }
    assert "/sales/days" in paths
    assert "/sales/dashboard" in paths
