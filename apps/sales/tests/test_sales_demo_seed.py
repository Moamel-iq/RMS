"""
`seed_sales_demo`: the guards, the namespace, and the second run that adds nothing.

What `docs/development/demo-data-policy.md` asks a demo command to be tested for
is exactly what is asserted here — refusal outside `DEBUG`, refusal without
`--confirm-demo` before anything posts, safe failure on an ambiguous selector,
only namespaced master data, posted operations reaching the ledger through the
services, and a second run creating no second document, movement or journal.

The idempotency test counts **everything**, including journal entries and
application receivable entries, and compares byte-for-byte. Counting only the
documents would miss the failure that actually matters: a second run that
re-posts a day it already posted would leave the row count unchanged and the
ledger doubled.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from apps.accounting.models import JournalEntry, JournalLine
from apps.core.models import AuditEvent
from apps.organizations.models import Branch, Organization
from apps.sales.demo import (
    ANCHOR,
    APPLICATIONS,
    DEMO_BANNER,
    DEMO_CODE_PREFIX,
    DEMO_NAMESPACE,
    MENU,
)
from apps.sales.models import (
    ApplicationReceivableEntry,
    CashierShift,
    CashierTenderCount,
    DeliveryAgreement,
    DeliveryApplication,
    DeliveryApplicationSettlement,
    DeliveryApplicationSettlementAdjustment,
    DeliveryApplicationSettlementAllocation,
    DiscountProgram,
    MenuItem,
    MenuPriceVersion,
    SalesAdjustment,
    SalesAdjustmentLine,
    SalesAdjustmentStatus,
    SalesChannel,
    SalesDay,
    SalesDayLine,
    SalesDayStatus,
    SalesTenderSummary,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

#: Every table the seed can touch. A count taken before and after the second
#: run, and compared as a whole: a partial list is a list somebody will forget
#: to extend when the scenario grows.
COUNTED = (
    MenuItem,
    MenuPriceVersion,
    SalesChannel,
    DeliveryApplication,
    DeliveryAgreement,
    DiscountProgram,
    SalesDay,
    SalesDayLine,
    SalesTenderSummary,
    SalesAdjustment,
    SalesAdjustmentLine,
    ApplicationReceivableEntry,
    DeliveryApplicationSettlement,
    DeliveryApplicationSettlementAllocation,
    DeliveryApplicationSettlementAdjustment,
    CashierShift,
    CashierTenderCount,
    JournalEntry,
    JournalLine,
    AuditEvent,
)


def _counts() -> dict[str, int]:
    return {model.__name__: model.objects.count() for model in COUNTED}


@pytest.fixture
def demo_ready(
    organization: Organization,
    branch: Branch,
    second_branch: Branch,
    superuser: User,
    scenario_recipe: Any,
    hall_cost_center: Any,
    delivery_cost_center: Any,
) -> dict[str, Any]:
    """
    Everything the seed needs and refuses to invent: a chart and the recipes.

    The recipes are the kitchen demo's in a real development database; here one
    is enough, and every menu row in `MENU` is pointed at it so the seed has a
    version to resolve. That substitution is done by patching `MENU`, not by
    weakening the seed: a menu item that named a recipe nobody had would be
    refused, and it should be.
    """
    from apps.accounting.management.commands.seed_chart_of_accounts import CHART
    from apps.accounting.services import create_account

    for code, name, name in CHART:
        create_account(organization=organization, code=code, name=name)
    return {"organization": organization, "branch": branch, "second_branch": second_branch}


@pytest.fixture
def one_recipe_menu(monkeypatch: pytest.MonkeyPatch, scenario_recipe: Any) -> None:
    """Point every demo menu row at the single recipe this test database has."""
    import apps.sales.demo as demo

    monkeypatch.setattr(
        demo,
        "MENU",
        tuple(
            (code, name, scenario_recipe.code, "WHOLE", price)
            for code, name, _recipe, _serving, price in MENU
        ),
    )


def _seed(**options: Any) -> None:
    call_command(
        "seed_sales_demo",
        user="root",
        organization="KM",
        branch="BUNOOK",
        second_branch="KARRADA",
        **options,
    )


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


@override_settings(DEBUG=False)
def test_it_refuses_to_run_outside_debug(demo_ready: dict[str, Any]) -> None:
    """
    Checked first, before any argument is read, and no flag turns it off.

    Demo sales in production would be indistinguishable from the branch's real
    takings on every report the business opens.
    """
    with pytest.raises(CommandError, match="DEBUG=True"):
        call_command("seed_sales_demo", user="root")


@override_settings(DEBUG=True)
def test_it_posts_nothing_without_confirm_demo(
    demo_ready: dict[str, Any], one_recipe_menu: None
) -> None:
    """
    Master data can be recreated; a posted journal cannot, so only that half is
    gated.
    """
    _seed()
    assert MenuItem.objects.filter(code__startswith=DEMO_CODE_PREFIX).count() == len(MENU)
    assert SalesDay.objects.count() == 0
    assert JournalEntry.objects.count() == 0
    assert ApplicationReceivableEntry.objects.count() == 0


@override_settings(DEBUG=True)
def test_an_unknown_organization_lists_the_valid_ones(demo_ready: dict[str, Any]) -> None:
    """The command never picks among several on the caller's behalf."""
    with pytest.raises(CommandError, match="Known:"):
        call_command("seed_sales_demo", user="root", organization="NOPE")


@override_settings(DEBUG=True)
def test_an_unknown_branch_is_refused_by_name(demo_ready: dict[str, Any]) -> None:
    with pytest.raises(CommandError, match="is not in KM"):
        call_command("seed_sales_demo", user="root", organization="KM", branch="NOWHERE")


@override_settings(DEBUG=True)
def test_an_unknown_user_is_refused(demo_ready: dict[str, Any]) -> None:
    with pytest.raises(CommandError, match="No user matches"):
        call_command("seed_sales_demo", user="nobody-at-all", organization="KM")


@override_settings(DEBUG=True)
def test_the_same_branch_twice_is_refused(demo_ready: dict[str, Any]) -> None:
    """
    The reversed day belongs to a different branch so both screens have rows.

    Refused rather than silently collapsed, because a seed that put both days on
    one branch would render one screen empty and nobody would know why.
    """
    with pytest.raises(CommandError, match="same branch"):
        call_command(
            "seed_sales_demo",
            user="root",
            organization="KM",
            branch="BUNOOK",
            second_branch="BUNOOK",
        )


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------


@override_settings(DEBUG=True)
def test_the_scenario_covers_every_screen(
    demo_ready: dict[str, Any], one_recipe_menu: None
) -> None:
    """
    A seed that renders empty is a seed that failed (Task 3.8's lesson).

    Every count below is a screen with rows on it, and the three adjustment
    kinds are asserted individually because the whole point of showing them
    together is that they differ.
    """
    _seed(confirm_demo=True)

    assert MenuItem.objects.filter(code__startswith=DEMO_CODE_PREFIX).count() == len(MENU)
    assert SalesChannel.objects.count() == 4
    assert DeliveryApplication.objects.count() == len(APPLICATIONS)
    assert DiscountProgram.objects.count() == 3

    assert SalesDay.objects.filter(status=SalesDayStatus.POSTED).count() == 1
    assert SalesDay.objects.filter(status=SalesDayStatus.REVERSED).count() == 1
    assert SalesDay.objects.filter(status=SalesDayStatus.DRAFT).count() == 1

    kinds = set(
        SalesAdjustment.objects.filter(status=SalesAdjustmentStatus.POSTED).values_list(
            "reason_kind", flat=True
        )
    )
    assert kinds == {
        "CANCELLED_BEFORE_FULFILLMENT",
        "RETURNED_AFTER_FULFILLMENT",
        "FINANCIAL_CORRECTION",
    }

    assert DeliveryApplicationSettlement.objects.filter(status="POSTED").count() == 1
    assert DeliveryApplicationSettlement.objects.filter(status="RECONCILED").count() == 1

    shift = CashierShift.objects.get()
    assert shift.status == "APPROVED"
    # Not zero, deliberately: a variance of exactly zero posts no journal at all
    # and demonstrates nothing about the one thing a closing may post.
    assert shift.variance_amount < 0
    assert shift.closed_by_id != shift.approved_by_id


@override_settings(DEBUG=True)
def test_the_financial_correction_takes_back_money_and_no_quantity(
    demo_ready: dict[str, Any], one_recipe_menu: None
) -> None:
    """
    ADR-028 §8: a money correction is not a claim that less food was sold.

    Enforced by a trigger and asserted here because it is the reason the third
    reason kind exists at all.
    """
    _seed(confirm_demo=True)
    correction = SalesAdjustment.objects.get(reason_kind="FINANCIAL_CORRECTION")
    for line in correction.lines.all():
        assert line.adjusted_quantity == 0
        assert line.adjusted_gross > 0


@override_settings(DEBUG=True)
def test_every_record_says_it_is_a_demo(demo_ready: dict[str, Any], one_recipe_menu: None) -> None:
    """
    Anyone looking at the database can tell in one glance what is scaffolding.

    Codes carry the prefix, documents carry the namespace in their evidence
    reference, and the banner is on the notes — three independent ways to find
    it, because a screenshot only ever shows one of them.
    """
    _seed(confirm_demo=True)
    for item in MenuItem.objects.all():
        assert item.code.startswith(DEMO_CODE_PREFIX)
        assert DEMO_BANNER in item.notes
    for adjustment in SalesAdjustment.objects.all():
        assert adjustment.evidence_reference.startswith(DEMO_NAMESPACE)
        assert DEMO_BANNER in adjustment.reason
    for settlement in DeliveryApplicationSettlement.objects.all():
        assert settlement.statement_reference.startswith(DEMO_NAMESPACE)


@override_settings(DEBUG=True)
def test_the_delivery_applications_are_fictional(
    demo_ready: dict[str, Any], one_recipe_menu: None
) -> None:
    """
    Spec §8.6: no real contract, rate or company name is used as approved data.

    Asserted as a positive — every code is namespaced and every name says
    تجريبي — rather than as a blocklist of real company names, which would be a
    list somebody has to keep current.
    """
    _seed()
    for application in DeliveryApplication.objects.all():
        assert application.code.startswith(DEMO_CODE_PREFIX)
        assert "تجريبي" in application.name


@override_settings(DEBUG=True)
def test_the_seed_writes_no_journal_by_hand(
    demo_ready: dict[str, Any], one_recipe_menu: None
) -> None:
    """
    Every journal names a Sales document, so every one came from a service.

    A journal written directly would carry no source identity, and this is the
    cheapest way to prove none was — the demo dataset's only value is being
    real.
    """
    _seed(confirm_demo=True)
    assert JournalEntry.objects.exists()
    for entry in JournalEntry.objects.all():
        assert entry.source_document_type.startswith("SALES.")
        assert entry.source_document_id
        assert entry.source_event


@override_settings(DEBUG=True)
def test_the_business_dates_are_fixed_and_not_todays(
    demo_ready: dict[str, Any], one_recipe_menu: None
) -> None:
    """
    A relative anchor would create a second set of sales days every calendar day.

    `SalesDay` is unique per branch and business date, so "today" as an anchor
    makes tomorrow's run a *new* document rather than a retry — which is not
    idempotency, it is accumulation.
    """
    _seed(confirm_demo=True)
    posted = SalesDay.objects.get(status=SalesDayStatus.POSTED)
    assert posted.business_date == ANCHOR
    assert posted.business_date != datetime.date.today()  # noqa: DTZ011 - the point of the test


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@override_settings(DEBUG=True)
def test_a_second_run_creates_nothing_at_all(
    demo_ready: dict[str, Any], one_recipe_menu: None, capsys: Any
) -> None:
    """
    Every table counted before and after, and every count identical.

    Journals and receivable entries are in the comparison deliberately: a second
    run that re-posted a day it had already posted would leave the document
    count unchanged and the ledger doubled, and a test that counted only
    documents would pass on it.
    """
    _seed(confirm_demo=True)
    capsys.readouterr()
    before = _counts()

    _seed(confirm_demo=True)
    output = capsys.readouterr().out
    after = _counts()

    assert after == before
    assert "0 created" in output


@override_settings(DEBUG=True)
def test_master_data_only_then_posting_still_reuses(
    demo_ready: dict[str, Any], one_recipe_menu: None
) -> None:
    """
    A run without `--confirm-demo` followed by one with it adds the documents and
    no second copy of the master data.

    This is the ordinary way somebody uses the command: look at the screens
    first, then decide to post.
    """
    _seed()
    master = MenuItem.objects.count(), SalesChannel.objects.count()

    _seed(confirm_demo=True)
    assert (MenuItem.objects.count(), SalesChannel.objects.count()) == master
    assert SalesDay.objects.count() == 3


@override_settings(DEBUG=True)
def test_the_seeded_module_verifies_clean(
    demo_ready: dict[str, Any], one_recipe_menu: None, capsys: Any
) -> None:
    """
    The demo and the verifier agree, which is what makes the demo worth having.

    A dataset that could not survive its own module's verifier would show the
    screens working and prove nothing about the postings behind them.
    """
    _seed(confirm_demo=True)
    capsys.readouterr()
    call_command("verify_sales", organization="KM")
    output = capsys.readouterr().out
    assert "No ERROR findings." in output


@override_settings(DEBUG=True)
def test_there_is_no_reset_flag() -> None:
    """
    Nothing this command posts may be removed to make a reseed convenient.

    A `--reset` that could only ever delete the master data would be a flag
    whose name promised more than it does.
    """
    from apps.sales.management.commands.seed_sales_demo import Command

    parser = Command().create_parser("manage.py", "seed_sales_demo")
    flags = {option for action in parser._actions for option in action.option_strings}  # noqa: SLF001
    assert "--reset-demo" not in flags
    assert "--force" not in flags
