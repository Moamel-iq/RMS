"""
The Phase 4 composite verifier. Read-only, and it composes rather than repeats.

`python manage.py verify_sales` answers one question: **does everything the
Sales module claims still agree with the ledgers underneath it?**

## Composition, not reimplementation

| Owner | Owns |
|---|---|
| `apps.sales.reconciliation` | the Sales equations: menu and serving validity, price resolution, discount funding, line arithmetic, the four journals, the receivable subledger, settlement allocations and legs, the till, source identity, idempotency, and the permission table |
| `apps.sales.daily_reconciliation` | the per-day three-way comparison, already written for المطابقة اليومية and forwarded here rather than re-derived |
| `apps.kitchen.consumption_sources` | whether the `SALES` theoretical adapter is registered at all |

Re-deriving any of them here would produce a second opinion that agrees until
the day it does not — the same discipline `verify_kitchen` follows.

## Three severities, and only one of them is a failure

```
ERROR                — a real disagreement. Exit code 1.
ADVISORY             — worth a human's attention. Exit code unchanged.
COVERAGE_LIMITATION  — something is knowably absent. Exit code unchanged.
```

The middle class is what makes this command usable as a Phase 4 gate. A
settlement whose commission gap is 4,300 dinars is **not** a defect in this
software: the counterparty computed a rate differently, that disagreement is a
commercial fact, and a verifier that exited non-zero on it would be red every
month and therefore ignored every month. What *is* an error is the module
recognising that commission twice — and that is checked, as an error, by name.

The third class covers a drawer that has been counted and not yet approved, and
a menu item whose recipe has no cost snapshot. Neither is anybody's mistake.

## No repair mode

There is no `--fix`, no `--repair`, no `--rebuild` (RCP-050). A verifier that
could change the thing it verifies is a verifier nobody can trust, and the one
situation where a repair is tempting — the numbers disagree — is exactly the
situation where a human needs to see them disagree first.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from django.core.management.base import CommandParser
from django.utils import timezone

from apps.core.console import SeedCommand
from apps.organizations.models import Organization
from apps.sales.reconciliation import (
    ADVISORY,
    COVERAGE_LIMITATION,
    ERROR,
    Finding,
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

#: How far back the per-day reconciliation walks by default. A quarter, because
#: that is roughly the horizon over which a settlement argument is still live,
#: and rebuilding every day since the branch opened to answer a question about
#: last week is how a verifier becomes something nobody runs.
DEFAULT_RECONCILIATION_DAYS = 90

#: The policies these checks exist to keep visible, printed in the epilogue so a
#: reader of the output does not have to find the ADRs to know what was at
#: stake.
SALES_POLICIES: tuple[str, ...] = (
    "Revenue is credited GROSS. Every deduction sits beside it as its own account.",
    "The application-funded discount reaches no account at all: the application "
    "reimburses it, so it reduces neither revenue nor the receivable.",
    "An adjustment posts to SALES_RETURNS and never touches SALES_REVENUE.",
    "Commission is recognised once, at the sale. A settlement journal that "
    "debits DELIVERY_COMMISSION_EXPENSE is wrong.",
    "A cashier closing posts the approved cash over/short variance and nothing "
    "else. A zero variance posts no journal and is not a failure.",
    "Only CANCELLED_BEFORE_FULFILLMENT reduces theoretical consumption. A return "
    "was cooked, and subtracting it invents a usage variance of exactly that "
    "quantity.",
    "Every unexplained settlement gap blocks reconciliation. Nothing is absorbed.",
    "No aging bucket is ever written off automatically.",
)


@dataclass(frozen=True)
class Section:
    """One verifier's contribution, with what it looked at."""

    title: str
    checked: str
    findings: list[Finding]


class Command(SeedCommand):
    help = (
        "Verify the whole Sales module against the ledgers underneath it: the menu and "
        "its servings, price resolution, discount funding, posted line arithmetic, the "
        "sales-day journal, gross revenue, adjustments, the application receivable "
        "subledger against its control account, settlement allocations and both variance "
        "legs, commission recognised once, the cashier over/short journal and its "
        "maker-checker, theoretical-consumption quantities, source identity, idempotency, "
        "the permission table and the daily reconciliation. Read-only; there is no repair "
        "mode. Exits non-zero only for ERROR — a coverage limitation is not a defect and "
        "neither is a commission gap with a counterparty."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--organization",
            dest="organization_code",
            default="",
            help="Organization code. Default: every organization.",
        )
        parser.add_argument(
            "--from",
            dest="date_from",
            default="",
            help=(
                "First business date the per-day reconciliation walks. "
                f"Default: {DEFAULT_RECONCILIATION_DAYS} days back."
            ),
        )
        parser.add_argument(
            "--to",
            dest="date_to",
            default="",
            help="Last business date the per-day reconciliation walks. Default: today.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        code = str(options.get("organization_code") or "").strip().upper()
        organizations = Organization.objects.all().order_by("code")
        if code:
            organizations = organizations.filter(code=code)
            if not organizations.exists():
                self.write(f"No organization with code {code}.")
                raise SystemExit(2)

        date_to = self._date(options.get("date_to"), timezone.localdate())
        date_from = self._date(
            options.get("date_from"),
            date_to - datetime.timedelta(days=DEFAULT_RECONCILIATION_DAYS),
        )
        if date_from > date_to:
            date_from = date_to

        errors = 0
        advisories = 0
        limitations = 0

        for organization in organizations:
            self.write("")
            self.write("=" * 72)
            self.write(f"{organization.code} — {organization.name_ar}")
            self.write("=" * 72)
            for section in self._sections(organization, date_from, date_to):
                counted = self._render(section)
                errors += counted[0]
                advisories += counted[1]
                limitations += counted[2]

        # The permission table belongs to the *module*, not to one organization.
        # Running it inside the loop above would report the same seventeen rows
        # once per organization, which is how a summary count stops meaning
        # anything.
        self.write("")
        self.write("=" * 72)
        self.write("Module-wide checks (not per organization)")
        self.write("=" * 72)
        for section in self._module_sections():
            counted = self._render(section)
            errors += counted[0]
            advisories += counted[1]
            limitations += counted[2]

        self._epilogue(errors, advisories, limitations, date_from, date_to)

    # -- plumbing ----------------------------------------------------------

    def _date(self, raw: Any, fallback: datetime.date) -> datetime.date:
        text = str(raw or "").strip()
        if not text:
            return fallback
        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            self.write(f"{text!r} is not an ISO date (YYYY-MM-DD).")
            raise SystemExit(2) from None

    def _render(self, section: Section) -> tuple[int, int, int]:
        """Print one section and return its (error, advisory, limitation) counts."""
        self.write("")
        self.write(f"{section.title}")
        self.write(f"  checked: {section.checked}")
        if not section.findings:
            self.write("  clean")
        for finding in section.findings:
            self.write(f"  [{finding.severity:<19}] {finding.code}")
            self.write(f"                        {finding.message}")
        return (
            len([row for row in section.findings if row.severity == ERROR]),
            len([row for row in section.findings if row.severity == ADVISORY]),
            len([row for row in section.findings if row.severity == COVERAGE_LIMITATION]),
        )

    # -- the sections ------------------------------------------------------

    def _sections(
        self, organization: Organization, date_from: datetime.date, date_to: datetime.date
    ) -> list[Section]:
        counts = counts_for(organization)
        return [
            Section(
                title="1. Menu, servings and fulfillment source",
                checked=f"{counts.menu_items} menu item(s)",
                findings=verify_menu(organization),
            ),
            Section(
                title="2. Price resolution: one answer per scope, and one in force",
                checked=f"{counts.prices} price row(s)",
                findings=verify_prices(organization),
            ),
            Section(
                title="3. Discount funding: the two shares add to one hundred",
                checked="every discount programme",
                findings=verify_discount_funding(organization),
            ),
            Section(
                title="4. Posted line arithmetic: gross, charge, net and the agreement",
                checked=f"{counts.posted_lines} posted line(s)",
                findings=verify_line_arithmetic(organization),
            ),
            Section(
                title="5. The sales-day journal, rebuilt from its plan",
                checked=f"{counts.posted_days} posted day(s)",
                findings=verify_day_journals(organization),
            ),
            Section(
                title="6. Revenue is gross, and the application-funded share posts nowhere",
                checked=f"{counts.posted_lines} posted line(s)",
                findings=(
                    verify_revenue_is_gross(organization)
                    + verify_application_discount_never_posts(organization)
                ),
            ),
            Section(
                title=(
                    "7. Adjustments: a journal that never touches revenue, and nothing "
                    "taken back beyond what was sold"
                ),
                checked=f"{counts.adjustments} posted adjustment(s)",
                findings=(
                    verify_adjustment_journals(organization)
                    + verify_adjustments_are_within_their_originals(organization)
                ),
            ),
            Section(
                title="8. Theoretical consumption: cancellations reduce it, returns do not",
                checked="every posted sales line",
                findings=verify_theoretical_quantities(organization),
            ),
            Section(
                title="9. The application receivable subledger against its control account",
                checked=f"{counts.receivable_entries} receivable entry/entries",
                findings=verify_receivable_ledger(organization),
            ),
            Section(
                title=(
                    "10. Settlements: allocations equal expected, both legs claimed, "
                    "no entry paid twice, commission recognised once"
                ),
                checked=f"{counts.settlements} posted settlement(s)",
                findings=(
                    verify_settlement_journals(organization)
                    + verify_settlement_allocations(organization)
                    + verify_settlement_commission(organization)
                ),
            ),
            Section(
                title=(
                    "11. The till: exactly two accounts or none at all, the stamped "
                    "variance, and maker-checker"
                ),
                checked=f"{counts.shifts} approved shift(s)",
                findings=(verify_shift_journals(organization) + verify_shift_counts(organization)),
            ),
            Section(
                title="12. Source identity, idempotency and no orphan journal",
                checked="every journal at a SALES.* source identity",
                findings=verify_source_identity(organization),
            ),
            Section(
                title="13. Daily reconciliation, forwarded from المطابقة اليومية",
                checked=f"posted days from {date_from.isoformat()} to {date_to.isoformat()}",
                findings=verify_daily_reconciliation(
                    organization, date_from=date_from, date_to=date_to
                ),
            ),
        ]

    def _module_sections(self) -> list[Section]:
        return [
            Section(
                title="14. Permissions: seventeen, all named, all scoped, all migrated",
                checked="apps/sales/permissions.py against the migrated codenames",
                findings=verify_permission_scope(),
            ),
            Section(
                title="15. Theoretical coverage: the SALES adapter is registered",
                checked="apps.kitchen.consumption_sources.coverage_code()",
                findings=verify_coverage(),
            ),
        ]

    def _epilogue(
        self,
        errors: int,
        advisories: int,
        limitations: int,
        date_from: datetime.date,
        date_to: datetime.date,
    ) -> None:
        self.write("")
        self.write("=" * 72)
        self.write(f"ERROR:               {errors}")
        self.write(f"ADVISORY:            {advisories}")
        self.write(f"COVERAGE_LIMITATION: {limitations}")
        self.write(f"reconciled window:   {date_from.isoformat()} .. {date_to.isoformat()}")
        self.write("=" * 72)
        self.write("")
        self.write("The policies these checks exist to keep visible:")
        for policy in SALES_POLICIES:
            self.write(f"  - {policy}")
        self.write("")
        self.write(
            "A COVERAGE_LIMITATION is not a defect: a drawer counted but not yet "
            "approved, or a menu item with no cost snapshot, is an ordinary state. "
            "Neither is an ADVISORY: a commission gap with a delivery company is a "
            "commercial disagreement, not a software one."
        )
        self.write("This command reports and refuses to repair. There is no --fix.")
        if errors:
            self.write("")
            self.write(f"{errors} ERROR finding(s). Exiting non-zero.")
            raise SystemExit(1)
        self.write("")
        self.write("No ERROR findings.")
