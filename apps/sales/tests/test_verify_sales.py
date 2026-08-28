"""
`verify_sales`, and the disagreements it is supposed to find.

A verifier that only ever runs against correct data proves nothing, so every
check below is asserted twice: clean on the scenario, and **failing** on a
database made to disagree.

## How the disagreements are manufactured, and what that revealed

The obvious technique — `queryset.update()` past the services — does not work
here, and finding that out was worth more than the tests it cost. Every write
this module makes is protected at the database: `sales_day_line_follows_its_day`,
`sales_adjustment_is_frozen`, `sales_shift_is_frozen`,
`sales_receivable_is_append_only`, `accounting_posted_line_is_immutable` and the
source-identity trigger between them refuse a raw `UPDATE` on every posted row.
So the tests that would have used one now assert the **refusal** instead, by
name, which is the stronger claim.

What is still reachable is what a verifier actually exists for:

* a **mapping repointed after the fact** — the journal was right when it posted
  and the accounts it names have since been given different meanings;
* an **append** to an append-only ledger, which is permitted by construction;
* a **draft edited around the service** before it posted, so the freeze arrived
  after the damage.

Each of those is a real way a production database diverges, and each is how a
finding below is produced.

## The two severities that must never become errors

A commission gap with a delivery company, and a drawer counted but not yet
approved. Both are asserted as advisories and limitations by name. A verifier
red every month is a verifier ignored every month.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction

from apps.accounting.models import (
    DELIVERY_COMMISSION_EXPENSE,
    SALES_CASH_OVER_SHORT,
    SALES_REVENUE,
    Account,
    JournalLine,
    OrganizationAccountMapping,
)
from apps.organizations.models import Organization
from apps.sales.models import (
    ApplicationReceivableEntry,
    CashierShift,
    MenuItem,
    ReceivableSource,
    SalesAdjustmentLine,
    SalesDayLine,
)
from apps.sales.reconciliation import (
    ADVISORY,
    COVERAGE_LIMITATION,
    ERROR,
    EXPECTED_PERMISSION_COUNT,
    counts_for,
    verify_adjustment_journals,
    verify_adjustments_are_within_their_originals,
    verify_application_discount_never_posts,
    verify_coverage,
    verify_daily_reconciliation,
    verify_day_journals,
    verify_discount_funding,
    verify_line_arithmetic,
    verify_menu,
    verify_permission_scope,
    verify_prices,
    verify_receivable_ledger,
    verify_revenue_is_gross,
    verify_settlement_allocations,
    verify_settlement_commission,
    verify_settlement_journals,
    verify_shift_counts,
    verify_shift_journals,
    verify_source_identity,
    verify_theoretical_quantities,
)

pytestmark = pytest.mark.django_db

WINDOW_FROM = datetime.date(2026, 8, 1)
WINDOW_TO = datetime.date(2026, 8, 31)


def _codes(findings: list[Any], severity: str | None = None) -> set[str]:
    return {row.code for row in findings if severity is None or row.severity == severity}


def _repoint(organization: Organization, role_code: str, account_code: str) -> None:
    """
    Give a role a different account **after** the journals that used it posted.

    A raw `update()` rather than `create_account_mapping`, which refuses an
    overlapping range. This is the one divergence the triggers cannot prevent
    and the one a verifier is genuinely for: every journal was right on the day
    it posted, and the accounts it names have since been given other meanings.
    """
    OrganizationAccountMapping.objects.filter(
        organization=organization, account_role__code=role_code
    ).update(account=Account.objects.get(organization=organization, code=account_code))


def _all(organization: Organization) -> list[Any]:
    """Every organization-scoped check, exactly as the command composes them."""
    return (
        verify_menu(organization)
        + verify_prices(organization)
        + verify_discount_funding(organization)
        + verify_line_arithmetic(organization)
        + verify_day_journals(organization)
        + verify_revenue_is_gross(organization)
        + verify_application_discount_never_posts(organization)
        + verify_adjustment_journals(organization)
        + verify_adjustments_are_within_their_originals(organization)
        + verify_theoretical_quantities(organization)
        + verify_receivable_ledger(organization)
        + verify_settlement_journals(organization)
        + verify_settlement_allocations(organization)
        + verify_settlement_commission(organization)
        + verify_shift_journals(organization)
        + verify_shift_counts(organization)
        + verify_source_identity(organization)
        + verify_daily_reconciliation(organization, date_from=WINDOW_FROM, date_to=WINDOW_TO)
    )


def test_a_correctly_posted_module_raises_no_error(scenario: dict[str, Any]) -> None:
    """
    Clean means **no ERROR**. Advisories are expected and are not failures.

    The drawer came up short and the counterparty's statement disagrees about
    commission. Both are real, both are for a person to decide about, and
    neither is a defect in this software.
    """
    findings = _all(scenario["organization"])
    assert _codes(findings, ERROR) == set()
    assert "settlement_commission_gap" in _codes(findings, ADVISORY)


def test_counts_report_what_was_actually_looked_at(scenario: dict[str, Any]) -> None:
    counts = counts_for(scenario["organization"])
    assert counts.posted_days == 1
    assert counts.posted_lines == 2
    assert counts.adjustments == 1
    assert counts.settlements == 1
    assert counts.shifts == 1
    assert counts.receivable_entries >= 2


# ---------------------------------------------------------------------------
# Divergences a verifier can actually meet
# ---------------------------------------------------------------------------


def test_revenue_that_stops_being_gross_is_an_error(scenario: dict[str, Any]) -> None:
    """
    The failure ADR-027 §2 exists to prevent, produced the way it really happens.

    `SALES_REVENUE` is repointed at the cash account, so the account the verifier
    calls revenue now holds a debit rather than the day's gross credit. That is
    the shape of every "somebody netted the discount into revenue" incident: the
    account is no longer holding what its name claims.
    """
    organization = scenario["organization"]
    assert verify_revenue_is_gross(organization) == []
    _repoint(organization, SALES_REVENUE, "1-01-01-001")
    assert "sales_revenue_is_not_gross" in _codes(verify_revenue_is_gross(organization), ERROR)


def test_an_adjustment_that_reaches_revenue_is_an_error(scenario: dict[str, Any]) -> None:
    """
    A return posts to `SALES_RETURNS`; revenue stays gross, always.

    Repointing `SALES_REVENUE` at the returns account is exactly the
    misconfiguration the check guards: two roles sharing one account makes every
    adjustment restate a posted gross revenue figure, and both sides of the
    ledger stay balanced while it happens.
    """
    organization = scenario["organization"]
    assert verify_adjustment_journals(organization) == []
    _repoint(organization, SALES_REVENUE, "4-03-01-001")
    findings = verify_adjustment_journals(organization)
    assert "sales_adjustment_touches_revenue" in _codes(findings, ERROR)


def test_a_settlement_that_debits_commission_twice_is_an_error(
    scenario: dict[str, Any],
) -> None:
    """
    The double-recognition check ADR-028 §6 asks for by name.

    Commission was accrued at the sale. A second debit at settlement overstates
    selling expense and understates gross margin by the same amount, and both
    figures are individually defensible afterwards — which is why nobody finds
    it by reading. Produced by pointing the commission role at the settlement
    variance account, which is a mapping mistake one edit away at any time.
    """
    organization = scenario["organization"]
    assert verify_settlement_journals(organization) == []
    _repoint(organization, DELIVERY_COMMISSION_EXPENSE, "7-09-05-001")
    findings = verify_settlement_journals(organization)
    assert "settlement_recognises_commission_twice" in _codes(findings, ERROR)


def test_a_shift_journal_touching_the_wrong_accounts_is_an_error(
    scenario: dict[str, Any],
) -> None:
    """
    Exactly two accounts, and nothing else. Not revenue, not card clearing.

    The intuitive design — the closing records the day's takings — would double
    every cash sales figure in the system, invisibly, because both entries would
    name a real document (ADR-027 §8).
    """
    organization = scenario["organization"]
    assert verify_shift_journals(organization) == []
    _repoint(organization, SALES_CASH_OVER_SHORT, "1-01-03-001")
    findings = verify_shift_journals(organization)
    assert "cashier_shift_journal_touches_the_wrong_accounts" in _codes(findings, ERROR)


def test_a_day_journal_that_stops_agreeing_with_its_plan_is_an_error(
    scenario: dict[str, Any],
) -> None:
    """The plan is rebuilt, never the stored journal trusted (RCP-112 proof 5)."""
    organization = scenario["organization"]
    assert verify_day_journals(organization) == []
    _repoint(organization, SALES_REVENUE, "1-01-02-001")
    findings = verify_day_journals(organization)
    assert "sales_day_journal_disagrees_with_its_plan" in _codes(findings, ERROR)


def test_an_appended_receivable_entry_breaks_the_subledger(
    scenario: dict[str, Any],
) -> None:
    """
    The ledger is append-only, so an *append* is the divergence it can meet.

    An entry with no journal behind it moves the subledger and leaves the
    control account where it was, which is the disagreement that surfaces
    mid-argument with the counterparty (ADR-027 §5).
    """
    organization = scenario["organization"]
    assert verify_receivable_ledger(organization) == []
    ApplicationReceivableEntry.objects.create(
        organization=organization,
        branch=scenario["branch"],
        delivery_application=scenario["application"],
        business_date=scenario["day"].business_date,
        source=ReceivableSource.SALE_POSTED,
        source_document_type="SALES.SALESDAY",
        source_document_id=str(uuid.uuid4()),
        debit=Decimal("1000.000"),
        narration="أُضيف خارج الخدمة",
    )
    findings = verify_receivable_ledger(organization)
    assert "receivable_subledger_disagrees_with_the_general_ledger" in _codes(findings, ERROR)


def test_a_journal_with_no_document_behind_it_is_an_error(
    scenario: dict[str, Any],
) -> None:
    """
    A stray journal at a Sales source identity, posted through the kernel.

    Built with `post_entry` rather than by hand, because the kernel is the only
    thing that can write one — which is also why this is the honest shape of the
    failure: some caller passed a document id that names nothing.
    """
    from apps.accounting.models import SourceEvent
    from apps.accounting.services import post_entry
    from apps.accounting.validators import PostingLine

    organization = scenario["organization"]
    assert verify_source_identity(organization) == []

    cash = Account.objects.get(organization=organization, code="1-01-01-001")
    bank = Account.objects.get(organization=organization, code="1-01-02-001")
    post_entry(
        organization=organization,
        accounting_date=scenario["day"].business_date,
        document_date=scenario["day"].business_date,
        lines=[
            PostingLine(
                account=cash,
                branch=scenario["branch"],
                cost_center=None,
                debit=Decimal("1"),
                credit=Decimal("0"),
            ),
            PostingLine(
                account=bank,
                branch=scenario["branch"],
                cost_center=None,
                debit=Decimal("0"),
                credit=Decimal("1"),
            ),
        ],
        idempotency_key=f"orphan:{uuid.uuid4()}",
        source_document_type="SALES.SALESDAY",
        source_document_id=str(uuid.uuid4()),
        source_event=SourceEvent.POSTED,
    )
    findings = verify_source_identity(organization)
    assert "sales_journal_names_no_document" in _codes(findings, ERROR)


def test_a_menu_item_whose_serving_lapsed_is_an_error(scenario: dict[str, Any]) -> None:
    """
    Master data is not frozen, so this one is reachable directly.

    An item whose serving code exists on no version cannot be sold, and the
    refusal arrives at the till at nine in the evening. Finding it here is the
    point.
    """
    assert verify_menu(scenario["organization"]) == []
    # The scenario's sales lines were captured against this item, and `0015`
    # checks each captured snapshot against the menu at the end of the
    # transaction. In production those lines committed before the serving
    # lapsed; inside one test transaction they would be checked *after* it,
    # against a menu they no longer match. Settle the queued checks now, the
    # way the commit did, so the lapse that follows is the later event it is.
    connection.check_constraints()
    MenuItem.objects.filter(pk=scenario["menu_item"].pk).update(serving_code="GONE")
    findings = verify_menu(scenario["organization"])
    assert "menu_item_serving_is_not_offered" in _codes(findings, ERROR)


def test_a_shift_closed_but_not_yet_approved_is_a_coverage_limitation(
    scenario: dict[str, Any],
    organization: Organization,
    branch: Any,
    cashier: Any,
    manager: Any,
) -> None:
    """
    A drawer waiting for a second person is the normal state of a drawer.

    Reported as a limitation rather than a finding, because a branch that has
    not had its closing approved yet has done nothing wrong — and colouring it
    red would make the report red every evening.
    """
    from apps.sales.day_services import add_sales_line, create_sales_day, submit_sales_day
    from apps.sales.posting import post_sales_day
    from apps.sales.shift_services import (
        close_cashier_shift,
        open_cashier_shift,
        set_tender_count,
    )

    second_date = scenario["day"].business_date + datetime.timedelta(days=1)
    day = create_sales_day(
        organization=organization, branch=branch, business_date=second_date, actor=manager
    )
    add_sales_line(
        day=day,
        menu_item=scenario["menu_item"],
        channel=scenario["hall"],
        quantity=Decimal("1.000"),
    )
    submit_sales_day(day=day, actor=manager)
    day = post_sales_day(day=day, actor=manager)

    shift = open_cashier_shift(
        organization=organization,
        branch=branch,
        business_date=second_date,
        cashier=cashier,
        opening_float=Decimal("0"),
        actor=manager,
    )
    set_tender_count(shift=shift, tender="CASH", counted_amount=Decimal("10000"), actor=cashier)
    close_cashier_shift(shift=shift, sales_day=day, actor=cashier)

    findings = verify_shift_counts(organization)
    assert "cashier_shift_is_not_approved_yet" in _codes(findings, COVERAGE_LIMITATION)
    assert _codes(findings, ERROR) == set()


# ---------------------------------------------------------------------------
# The divergences the database simply refuses to produce
# ---------------------------------------------------------------------------
#
# Each of these would have been a negative test for a check above. The refusal
# is the better assertion: the invariant does not depend on anybody remembering
# to call a service, and the verifier's own check remains as the thing that
# would notice a restore replaying around the trigger.


def test_a_posted_sales_line_refuses_a_raw_update(scenario: dict[str, Any]) -> None:
    with (
        pytest.raises(IntegrityError, match="only be changed while the day is a draft"),
        transaction.atomic(),
    ):
        SalesDayLine.objects.filter(sales_day=scenario["day"], sequence=1).update(
            gross_amount=Decimal("99999.000")
        )


def test_a_posted_adjustment_line_refuses_a_raw_update(scenario: dict[str, Any]) -> None:
    """The over-adjustment the trigger will not let anybody write."""
    with (
        pytest.raises(IntegrityError, match="while the adjustment is a draft"),
        transaction.atomic(),
    ):
        SalesAdjustmentLine.objects.filter(adjustment=scenario["adjustment"]).update(
            adjusted_quantity=Decimal("99.000")
        )


def test_a_posted_journal_line_refuses_a_raw_update(scenario: dict[str, Any]) -> None:
    line = JournalLine.objects.filter(entry__source_document_type="SALES.SALESDAY").first()
    assert line is not None
    with pytest.raises(IntegrityError, match="immutable"), transaction.atomic():
        JournalLine.objects.filter(pk=line.pk).update(debit=line.debit + Decimal("1"))


def test_the_receivable_ledger_refuses_a_raw_update(scenario: dict[str, Any]) -> None:
    entry = ApplicationReceivableEntry.objects.filter(organization=scenario["organization"]).first()
    assert entry is not None
    with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
        ApplicationReceivableEntry.objects.filter(pk=entry.pk).update(debit=Decimal("1"))


def test_an_approved_shift_refuses_a_raw_update(scenario: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError, match="frozen"), transaction.atomic():
        CashierShift.objects.filter(pk=scenario["shift"].pk).update(counted_cash=Decimal("1.000"))


def test_a_discount_whose_funding_does_not_close_is_refused(
    organization: Organization,
) -> None:
    """
    A funding split that leaves ten percent belonging to nobody, refused at the
    database rather than only by the service.

    `verify_discount_funding` stays as the check that would notice a row written
    before the constraint existed.
    """
    from apps.sales.models import DiscountProgram

    with pytest.raises(IntegrityError), transaction.atomic():
        DiscountProgram.objects.create(
            organization=organization,
            code="BROKEN",
            name="خصم مكسور",
            discount_percent=Decimal("10"),
            restaurant_funded_share=Decimal("60"),
            application_funded_share=Decimal("30"),
            effective_from=datetime.date(2026, 1, 1),
        )
    assert verify_discount_funding(organization) == []


# ---------------------------------------------------------------------------
# Theoretical consumption — the asymmetry, asserted directly
# ---------------------------------------------------------------------------


def test_only_a_cancellation_reduces_the_theoretical_quantity(
    scenario: dict[str, Any], accounting_manager: Any
) -> None:
    """
    A return is posted beside the cancellation and the map does not move.

    The intuitive implementation subtracts every posted adjustment. It reads
    perfectly well, is one filter shorter, and manufactures an unexplained usage
    variance of exactly the returned quantity in every branch that ever takes a
    plate back (ADR-028 §8). This is the cheapest possible guard against it.
    """
    from apps.sales.adjustment_posting import post_sales_adjustment
    from apps.sales.adjustment_services import add_adjustment_line, create_sales_adjustment
    from apps.sales.consumption_source import cancelled_quantities
    from apps.sales.models import SalesAdjustmentReasonKind

    day = scenario["day"]
    line_ids = list(day.lines.values_list("pk", flat=True))
    before = cancelled_quantities(line_ids)
    assert sum(before.values()) == Decimal("1.000")

    application_line = day.lines.get(sequence=2)
    returned = create_sales_adjustment(
        sales_day=day,
        reason_kind=SalesAdjustmentReasonKind.RETURNED_AFTER_FULFILLMENT,
        business_date=day.business_date + datetime.timedelta(days=5),
        reason="طلب أُعيد بعد التسليم.",
        evidence_reference="SCN/ADJ-RETURN",
        actor=accounting_manager,
    )
    add_adjustment_line(
        adjustment=returned,
        original_line=application_line,
        adjusted_quantity=Decimal("2.000"),
        actor=accounting_manager,
    )
    post_sales_adjustment(adjustment=returned, actor=accounting_manager)

    after = cancelled_quantities(line_ids)
    assert after == before
    assert verify_theoretical_quantities(scenario["organization"]) == []


# ---------------------------------------------------------------------------
# The two that must never become errors
# ---------------------------------------------------------------------------


def test_the_commission_gap_is_an_advisory_and_never_an_error(
    scenario: dict[str, Any],
) -> None:
    """
    A rate dispute is a commercial fact, not a software defect.

    A verifier that exited non-zero on it would be red every month and would
    therefore be ignored every month, which is worse than not checking.
    """
    findings = verify_settlement_commission(scenario["organization"])
    assert _codes(findings, ADVISORY) == {"settlement_commission_gap"}
    assert _codes(findings, ERROR) == set()


def test_the_settlement_legs_are_both_claimed(scenario: dict[str, Any]) -> None:
    """Every dinar of each gap carries an adjustment, so nothing is unexplained."""
    assert verify_settlement_allocations(scenario["organization"]) == []


# ---------------------------------------------------------------------------
# Module-wide
# ---------------------------------------------------------------------------


def test_the_permission_table_is_complete_at_seventeen() -> None:
    """
    The promise `apps/sales/permissions.py` makes in its own docstring, kept.

    A name is not a grant, and a name whose checkpoint has not landed appears in
    neither `ALL_PERMISSIONS` nor any role. Every checkpoint has landed, so the
    two sets must finally coincide.
    """
    from apps.sales.permissions import ALL_PERMISSIONS

    assert len(ALL_PERMISSIONS) == EXPECTED_PERMISSION_COUNT
    assert len(set(ALL_PERMISSIONS)) == EXPECTED_PERMISSION_COUNT


def test_every_declared_permission_name_is_granted_and_migrated() -> None:
    assert verify_permission_scope() == []


def test_the_sales_quantity_source_is_registered() -> None:
    """
    Phase 3 shipped `TheoreticalSourceType.SALES` with no adapter on purpose.

    Checkpoint 3 registered one; this asserts the coverage code actually
    changed, because a registration that silently failed at app-ready would
    leave every kitchen report honestly saying the wrong thing.
    """
    from apps.kitchen.consumption_sources import (
        SALES_NOT_INCLUDED,
        coverage_code,
        sales_source_is_registered,
    )

    assert sales_source_is_registered()
    assert coverage_code() != SALES_NOT_INCLUDED
    assert verify_coverage() == []


def test_prices_have_one_answer_per_scope(scenario: dict[str, Any]) -> None:
    assert _codes(verify_prices(scenario["organization"]), ERROR) == set()


def test_the_command_runs_and_exits_zero_on_a_clean_module(
    scenario: dict[str, Any], capsys: Any
) -> None:
    """
    Exit code zero with advisories present, which is the whole design.

    `SystemExit` is raised only for `ERROR`; a coverage limitation and a
    commercial disagreement leave the code alone.
    """
    from django.core.management import call_command

    call_command("verify_sales", organization=scenario["organization"].code)
    output = capsys.readouterr().out
    assert "No ERROR findings." in output
    assert "settlement_commission_gap" in output
    assert "There is no --fix." in output


def test_the_command_exits_non_zero_on_an_error(scenario: dict[str, Any]) -> None:
    from django.core.management import call_command

    _repoint(scenario["organization"], SALES_REVENUE, "1-01-01-001")
    with pytest.raises(SystemExit) as raised:
        call_command("verify_sales", organization=scenario["organization"].code)
    assert raised.value.code == 1


def test_the_command_offers_no_repair_flag() -> None:
    """
    RCP-050 as a test rather than as a promise.

    A verifier that could change what it verifies is a verifier nobody can
    trust, and the one moment a repair is tempting is the moment a human needs
    to see the numbers disagree.
    """
    from apps.sales.management.commands.verify_sales import Command

    parser = Command().create_parser("manage.py", "verify_sales")
    flags = {option for action in parser._actions for option in action.option_strings}  # noqa: SLF001
    assert "--fix" not in flags
    assert "--repair" not in flags
    assert "--rebuild" not in flags
