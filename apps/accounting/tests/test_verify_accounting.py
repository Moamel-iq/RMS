"""
`verify_accounting` — the composite verifier, and the properties it must keep.

The tests worth having here are structural rather than numeric. Whether one
organization's books tie on one day is what the command *reports*; whether the
command can be trusted to report it honestly is what these check:

* it offers no repair, in the argument parser or anywhere in `reconciliation`;
* it forwards Procurement's and Sales' verifiers rather than re-deriving them;
* a check that raises becomes a finding instead of aborting the run.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounting import reconciliation
from apps.accounting.management.commands import verify_accounting as command_module
from apps.accounting.models import Account
from apps.accounting.reconciliation import (
    ERROR,
    verify_application_subledger,
    verify_journals_balance,
    verify_no_stored_balance,
    verify_supplier_subledger,
)
from apps.accounting.services import post_entry
from apps.accounting.validators import PostingLine
from apps.organizations.models import Branch, Organization

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db


def _run(*args: str) -> str:
    out = StringIO()
    call_command("verify_accounting", *args, stdout=out)
    return out.getvalue()


def test_the_command_offers_no_repair_flag() -> None:
    """
    No `--fix`, no `--repair`, no `--rebuild`.

    A verifier that could change what it verifies is one nobody can trust, and
    the single situation where a repair is tempting — the numbers disagree — is
    exactly the one where a human has to see them disagree first.
    """
    parser = command_module.Command().create_parser("manage.py", "verify_accounting")
    flags = {option for action in parser._actions for option in action.option_strings}

    assert not {"--fix", "--repair", "--rebuild", "--force"} & flags
    assert "--organization" in flags


def test_nothing_in_reconciliation_writes() -> None:
    """
    The report-only rule, checked against the source rather than trusted.

    A `.save()` that appeared in a verifier would make the run change what the
    next run measures, and the drift would be invisible because the verifier
    would keep reporting clean.
    """
    source = inspect.getsource(reconciliation)
    for forbidden in (".save(", ".delete(", ".update(", ".create(", ".bulk_"):
        assert forbidden not in source, f"{forbidden} appears in a report-only module"


def test_it_forwards_the_other_modules_verifiers() -> None:
    """
    Composition, not repetition.

    A second derivation of a supplier position agrees with Procurement's right
    up until the day it does not, and then there are two answers and no way to
    tell which is wrong.
    """
    supplier_source = inspect.getsource(verify_supplier_subledger)
    application_source = inspect.getsource(verify_application_subledger)

    assert "verify_supplier_payables" in supplier_source
    assert "verify_receivable_ledger" in application_source


def test_a_check_that_raises_becomes_a_finding(
    monkeypatch: pytest.MonkeyPatch, organization: Organization, chart: None
) -> None:
    """
    One broken check does not cost the other fifteen answers.

    The run reports the failure as an ERROR of its own — visible, attributed to
    the check that raised, and non-zero on exit — rather than swallowing it or
    dying halfway through.
    """

    def explode(_organization: Organization) -> list[Any]:
        raise RuntimeError("deliberately broken check")

    monkeypatch.setattr(
        command_module,
        "CHECKS",
        (("journals balance", explode), ("statement mapping", lambda org: [])),
    )

    with pytest.raises(CommandError):
        _run("--organization", organization.code)


def test_a_clean_organization_reports_no_blocking_findings(
    organization: Organization, branch: Branch, chart: None, cash: Account, hall: Any
) -> None:
    """The happy path, so the command's own plumbing is exercised end to end."""
    sales = Account.objects.get(organization=organization, code="4-01-01-001")
    post_entry(
        organization=organization,
        accounting_date=POSTING_DATE,
        lines=[
            PostingLine(account=cash, branch=branch, debit=Decimal("1000")),
            PostingLine(account=sales, branch=branch, cost_center=hall, credit=Decimal("1000")),
        ],
        narration="cash sale",
        idempotency_key="test:verify:sale",
    )

    assert verify_journals_balance(organization) == []
    assert [row for row in verify_supplier_subledger(organization) if row.severity == ERROR] == []
    assert [
        row for row in verify_application_subledger(organization) if row.severity == ERROR
    ] == []


def test_no_accounting_model_carries_a_stored_balance() -> None:
    """
    The tripwire for the rule the whole module rests on.

    This one is about the code rather than one organization's data, which is
    why it takes no organization: a `current_balance` column added to `Cashbox`
    next year would be caught here on the first run.
    """
    assert verify_no_stored_balance() == []


def test_an_unknown_organization_is_refused_rather_than_silently_skipped(
    organization: Organization,
) -> None:
    """A typo in the code must not look like a clean run."""
    with pytest.raises(CommandError):
        _run("--organization", "NO-SUCH-ORG")
