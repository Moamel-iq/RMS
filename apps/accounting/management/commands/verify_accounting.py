r"""
The Phase 5 composite verifier.

    .venv\Scripts\python.exe manage.py verify_accounting --organization DEMO-KHAN-MANDI

Answers one question: **does everything the Accounting module shows still agree
with the ledger underneath it?**

It composes rather than repeats. `verify_supplier_payables` belongs to
Procurement and `verify_receivable_ledger` to Sales; both are forwarded, never
re-derived, because a second derivation agrees with the first until the day it
does not — and then there are two answers and no way to tell which is wrong.

Three severities, one of which is a failure:

    ERROR                — a real disagreement. Exit code 1.
    ADVISORY             — worth a human's attention. Exit code unchanged.
    COVERAGE_LIMITATION  — something is knowably absent. Exit code unchanged.

**Report-only.** No `--fix`, no `--repair`, no `--rebuild`. A verifier that
could change what it verifies is one nobody can trust, and the single situation
where a repair is tempting — the numbers disagree — is exactly the one where a
human has to see them disagree first.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import CommandError, CommandParser

from apps.accounting.reconciliation import (
    ADVISORY,
    COVERAGE_LIMITATION,
    ERROR,
    Finding,
    counts_for,
    verify_account_hierarchy,
    verify_accruals,
    verify_application_subledger,
    verify_balance_sheet,
    verify_cash_account_consistency,
    verify_expense_vouchers,
    verify_income_statement,
    verify_journals_balance,
    verify_manual_maker_checker,
    verify_mapping_continuity,
    verify_no_stored_balance,
    verify_periods,
    verify_prepayment_schedules,
    verify_source_identity,
    verify_statement_mapping,
    verify_supplier_subledger,
    verify_trial_balance,
)
from apps.core.console import SeedCommand
from apps.organizations.models import Organization

#: Every check, named so the output says which one produced a finding. A check
#: that raises is reported as a finding of its own rather than aborting the run:
#: the other twenty answers are still worth having.
CHECKS = (
    ("journals balance", verify_journals_balance),
    ("source identity", verify_source_identity),
    ("manual maker-checker", verify_manual_maker_checker),
    ("account hierarchy", verify_account_hierarchy),
    ("mapping continuity", verify_mapping_continuity),
    ("cash account consistency", verify_cash_account_consistency),
    ("supplier subledger", verify_supplier_subledger),
    ("application subledger", verify_application_subledger),
    ("expense vouchers", verify_expense_vouchers),
    ("accruals", verify_accruals),
    ("prepayment schedules", verify_prepayment_schedules),
    ("trial balance", verify_trial_balance),
    ("statement mapping", verify_statement_mapping),
    ("income statement", verify_income_statement),
    ("balance sheet", verify_balance_sheet),
    ("periods", verify_periods),
)


class Command(SeedCommand):
    help = "Report-only accounting verification. Exits 1 on ERROR findings."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--organization",
            help="Organization code. Omit to check every organization.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        code = (options.get("organization") or "").strip().upper()
        if code:
            organizations = list(Organization.objects.filter(code=code))
            if not organizations:
                raise CommandError(f"No organization with code {code}.")
        else:
            organizations = list(Organization.objects.order_by("code"))

        total_errors = 0
        for organization in organizations:
            self.write("")
            self.write(f"=== {organization.code} ===")
            counts = counts_for(organization)
            self.write("  " + "  ".join(f"{key}={value}" for key, value in counts.items()))

            findings: list[tuple[str, Finding]] = []
            for label, check in CHECKS:
                try:
                    for finding in check(organization):
                        findings.append((label, finding))
                except Exception as failure:  # noqa: BLE001 - a broken check is a finding
                    findings.append(
                        (
                            label,
                            Finding(
                                severity=ERROR,
                                code="check_raised",
                                message=f"{type(failure).__name__}: {failure}",
                            ),
                        )
                    )

            # The no-stored-balance check is about the code rather than one
            # organization's data, so it runs once and is reported here.
            for finding in verify_no_stored_balance():
                findings.append(("stored balance", finding))

            errors = [row for row in findings if row[1].severity == ERROR]
            advisories = [row for row in findings if row[1].severity == ADVISORY]
            limitations = [row for row in findings if row[1].severity == COVERAGE_LIMITATION]
            total_errors += len(errors)

            for label, finding in errors:
                self.write(f"  ERROR      [{label}] {finding.code}: {finding.message}")
            for label, finding in advisories:
                self.write(f"  ADVISORY   [{label}] {finding.code}: {finding.message}")
            for label, finding in limitations:
                self.write(f"  LIMITATION [{label}] {finding.code}: {finding.message}")

            self.write(
                f"  {len(errors)} ERROR, {len(advisories)} ADVISORY, "
                f"{len(limitations)} COVERAGE_LIMITATION"
            )

        self.write("")
        if total_errors:
            raise CommandError(f"{total_errors} accounting ERROR findings.")
        self.write("verify_accounting: no blocking findings.")
