r"""
Populate a development database with the Sales demo dataset.

Not reference data and not a fixture: a menu, four channels, three fictional
delivery applications with three different commission bases, three discount
programmes, three trading days, three adjustments, two settlements and one
counted drawer — every one of them built through the real domain services, so
each Phase 4 screen can be looked at with something on it. See
`docs/development/demo-data-policy.md`.

    .venv\Scripts\python.exe manage.py seed_sales_demo --user moamel --confirm-demo

`settings.DEBUG` is checked **first**, before any argument is read, and no flag
turns it off: demo sales in production would be indistinguishable from the
branch's real takings on every report the business opens.

`--confirm-demo` gates the irreversible half and only that half. Without it the
command builds the menu, the channels, the applications, the agreements and the
discounts and stops — all of which can be recreated. With it, it posts sales
days, adjustments, a settlement and a cashier closing, and those write journal
entries and receivable movements that no reseed may ever delete.

## Idempotency

Every step looks before it creates, keyed on something the database already
makes unique: a code for master data, `(branch, business_date)` for a sales
day and a shift, `(application, statement_reference)` for a settlement, and the
namespaced `evidence_reference` for an adjustment. A second run reports
`0 created, N reused` and adds no second document, no second journal and no
second receivable entry.

## There is no --reset-demo

The inventory seed has one and this deliberately does not. Everything this
command posts is ledger history — a journal, an application receivable movement,
an approved cash difference — and none of it may be removed to make a reseed
convenient. To start from nothing, use a fresh development database or a fresh
namespace version. Both leave the ledger's guarantee intact, and a `--reset`
that could only ever delete the master data would be a flag whose name promised
more than it does.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import CommandError, CommandParser
from django.db import transaction
from django.urls import reverse

from apps.core.console import SeedCommand
from apps.core.context import audit_context
from apps.inventory.demo import DEMO_ORGANIZATION_CODE, DemoSelectionError
from apps.inventory.management.commands.seed_inventory_demo import resolve_user
from apps.organizations.models import Branch, Organization
from apps.sales.demo import (
    ANCHOR,
    DEMO_BANNER,
    DEMO_NAMESPACE,
    DemoPreconditionError,
    SalesDemo,
    seed_sales_demo,
)
from apps.sales.demo import SETTLEMENT_DATE as DEMO_SETTLEMENT_DATE

#: The screens this dataset makes reviewable, in navigation order. Kept in step
#: with `apps/core/navigation.py`: an entry here that renders empty is the demo
#: telling an operator to look at nothing, which is how Task 3.8 discovered a
#: whole module had been rendering correctly against no rows at all.
INSPECTION_ROUTES: tuple[tuple[str, str], ...] = (
    ("sales:dashboard", "لوحة المبيعات"),
    ("sales:day_list", "المبيعات اليومية"),
    ("sales:menu_item_list", "أصناف المنيو"),
    ("sales:channel_list", "قنوات البيع"),
    ("sales:application_list", "تطبيقات التوصيل"),
    ("sales:agreement_list", "العمولات والاتفاقيات"),
    ("sales:discount_list", "الخصومات"),
    ("sales:adjustment_list", "المرتجعات والإلغاءات"),
    ("sales:receivable_list", "ذمم التطبيقات"),
    ("sales:settlement_list", "تسويات التطبيقات"),
    ("sales:shift_list", "إقفال الكاشير"),
    ("sales:report_daily_reconciliation", "المطابقة اليومية"),
)


class Command(SeedCommand):
    help = (
        "Seed the Sales demo dataset (DEBUG only): a menu, four channels, three "
        "fictional delivery applications with three commission bases, three discount "
        "programmes, a posted day, a reversed day, a draft day, one adjustment of each "
        "reason kind, two settlements and one counted drawer with a small shortage. "
        "Everything posts through the real domain services; nothing is inserted "
        "directly. Requires --confirm-demo before anything reaches the ledger."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--user",
            required=True,
            help="Username, email, or id of the person the audit trail will name.",
        )
        parser.add_argument(
            "--organization",
            default=DEMO_ORGANIZATION_CODE,
            help=f"Organization code. Default {DEMO_ORGANIZATION_CODE}.",
        )
        parser.add_argument(
            "--branch",
            default="",
            help="Branch code for the posted day and the drawer. Default: the first.",
        )
        parser.add_argument(
            "--second-branch",
            default="",
            help="Branch code for the reversed day. Default: the second, or the first.",
        )
        parser.add_argument(
            "--confirm-demo",
            action="store_true",
            dest="confirm_demo",
            help=(
                "Required before anything posts. Without it the command builds master "
                "data only — a posted journal and an application receivable cannot be "
                "removed afterwards."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG:
            raise CommandError(
                "seed_sales_demo runs only with DEBUG=True. Demo sales in production "
                "would be indistinguishable from the branch's real takings on every "
                "report the business opens."
            )

        try:
            user = resolve_user(options["user"])
        except DemoSelectionError as problem:
            raise CommandError(str(problem)) from problem

        organization = self._organization(options["organization"])
        branch = self._branch(organization, options["branch"], "--branch", index=0)
        second = self._branch(organization, options["second_branch"], "--second-branch", index=1)
        if second.pk == branch.pk and organization.branches.count() > 1:
            raise CommandError(
                "--branch and --second-branch name the same branch. The reversed day "
                "belongs to a different one so both screens have rows."
            )

        post_documents = bool(options["confirm_demo"])
        try:
            # One transaction for the whole scenario. A domain refusal anywhere
            # leaves nothing behind, because the half that posted would be real.
            with transaction.atomic(), audit_context(actor=user):
                result = seed_sales_demo(
                    organization=organization,
                    branch=branch,
                    second_branch=second,
                    post_documents=post_documents,
                )
        except DemoPreconditionError as problem:
            raise CommandError(str(problem)) from problem

        self._report(result, user=user, post_documents=post_documents)

    # -- selection ---------------------------------------------------------

    def _organization(self, code: str) -> Organization:
        wanted = code.strip().upper()
        organization = Organization.objects.filter(code=wanted).first()
        if organization is None:
            known = ", ".join(Organization.objects.order_by("code").values_list("code", flat=True))
            raise CommandError(
                f"No organization {wanted!r}. Known: {known or 'none'}. "
                "Run seed_inventory_demo and seed_kitchen_demo first — the Sales menu "
                "is built on the kitchen's recipes."
            )
        return organization

    def _branch(self, organization: Organization, code: str, flag: str, *, index: int) -> Branch:
        """
        One branch, never guessed among several without saying which.

        An explicit code that does not exist is an error listing the valid ones;
        an omitted code takes a *stated* default and the output says which was
        taken, because a demo that silently picked a branch would be a demo
        whose figures nobody can locate.
        """
        branches = list(organization.branches.order_by("code"))
        if not branches:
            raise CommandError(f"{organization.code} has no branches.")
        wanted = code.strip().upper()
        if wanted:
            match = next((row for row in branches if row.code == wanted), None)
            if match is None:
                known = ", ".join(row.code for row in branches)
                raise CommandError(
                    f"{flag} {wanted!r} is not in {organization.code}. Known: {known}."
                )
            return match
        return branches[min(index, len(branches) - 1)]

    # -- output ------------------------------------------------------------

    def _report(self, result: SalesDemo, *, user: Any, post_documents: bool) -> None:
        self.write("")
        self.write(f"Organization  {result.organization.code} - {result.organization.name_ar}")
        self.write(
            f"Branches      {result.branch.code} (posted, drawer) / "
            f"{result.second_branch.code} (reversed)"
        )
        self.write(f"Namespace     {DEMO_NAMESPACE}")
        self.write(f"Audit actor   {user}")
        self.write(f"Every record below is {DEMO_BANNER}")
        self.write("")
        self.write(f"{result.created} created, {result.reused} reused.")

        self.write("")
        self.write("master data:")
        for item in result.menu_items:
            self.write(f"  menu    {item.code:<24} {item.serving_code:<8} {item.name_ar}")
        for code, channel in result.channels.items():
            self.write(f"  channel {code:<24} {channel.category:<22} {channel.default_tender}")
        for code, application in result.applications.items():
            self.write(
                f"  app     {code:<24} cycle {application.settlement_cycle_days:>3}d  "
                f"{application.name_ar}"
            )
        for code, program in result.programs.items():
            self.write(
                f"  discount {code:<23} restaurant {program.restaurant_funded_share}% / "
                f"application {program.application_funded_share}%"
            )

        if not post_documents:
            self.write("")
            self.write(
                "Master data only. Pass --confirm-demo to post the days, the "
                "adjustments, the settlement and the drawer — those reach the ledger "
                "and cannot be removed afterwards."
            )
            return

        self.write("")
        self.write("documents:")
        for label, day in (
            ("posted", result.posted_day),
            ("reversed", result.reversed_day),
            ("draft", result.draft_day),
        ):
            if day is None:
                continue
            self.write(
                f"  day     {label:<9} {day.business_date} {day.branch.code:<14} "
                f"{day.status:<9} {day.number or '(no number)'}"
            )
        for adjustment in result.adjustments:
            self.write(
                f"  return  {adjustment.reason_kind:<30} {adjustment.status:<9} "
                f"{adjustment.number or '(no number)'}"
            )
        for settlement in result.settlements:
            self.write(
                f"  settle  {settlement.delivery_application.code:<18} "
                f"{settlement.status:<11} expected {settlement.expected_amount} "
                f"statement {settlement.statement_amount} remitted {settlement.remitted_amount}"
            )
        if result.shift is not None:
            self.write(
                f"  drawer  {result.shift.business_date} {result.shift.branch.code:<14} "
                f"{result.shift.status:<9} expected {result.shift.expected_cash} "
                f"counted {result.shift.counted_cash} variance {result.shift.variance_amount}"
            )

        self.write("")
        self.write("Only the cancellation reduces theoretical consumption. A return was")
        self.write("cooked and its ingredients left; subtracting it would invent an")
        self.write("unexplained usage variance of exactly that quantity, every time.")

        window = f"?from={ANCHOR.isoformat()}&to={DEMO_SETTLEMENT_DATE.isoformat()}"
        self.write("")
        self.write("worth opening:")
        for name, label in INSPECTION_ROUTES:
            suffix = window if name == "sales:dashboard" else ""
            self.write(f"  {reverse(name)}{suffix:<34} {label}")
        self.write("")
        self.write(
            "The dashboard link carries the scenario's dates because the demo uses "
            "fixed business dates: a relative anchor would create a second set of "
            "sales days every calendar day."
        )
        self.write("")
        self.write("then verify it:")
        self.write(f"  manage.py verify_sales --organization {result.organization.code}")
