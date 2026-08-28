"""
Populate a development database with the inventory demo dataset.

Not reference data, and not a fixture: this posts real business documents
through the real domain services so that every inventory screen can be looked
at with something in it. See `docs/development/demo-data-policy.md`.

    .venv\\Scripts\\python.exe manage.py seed_inventory_demo --user moamel --confirm-demo

## The guards, and why each one is there

`--confirm-demo` is required before anything *posts*. Master data can be
recreated; a posted stock movement and its journal cannot be deleted, because
the ledger is append-only by design. So the flag does not guard the command —
it guards the irreversible half of it.

`settings.DEBUG` is checked first, before any argument is read, and there is no
flag that turns it off. A demo dataset in production would be indistinguishable
from real stock in every report the business runs.

Selectors never guess. Two users matching `--user`, or an organization code
that does not exist, ends the command with the valid choices listed. A seed
that quietly picks the first of two candidates writes into the wrong one
exactly once, and that once is unrecoverable.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import CommandError, CommandParser
from django.db.models import Q
from django.urls import reverse

from apps.core.console import SeedCommand
from apps.core.context import audit_context
from apps.inventory.demo import (
    DEMO_ORGANIZATION_CODE,
    DESTINATION_BRANCH_CODE,
    NAMESPACE,
    SOURCE_BRANCH_CODE,
    DemoResult,
    DemoSelectionError,
    reset_demo,
    seed_inventory_demo,
)
from apps.users.models import User

#: The screens this dataset makes reviewable, in navigation order. Route names
#: rather than literal paths: a hard-coded path is a second copy of the URL
#: configuration, and the copy is the one that goes stale.
INSPECTION_ROUTES: list[tuple[str, str]] = [
    ("inventory:category_list", "مجموعات الأصناف"),
    ("inventory:package_unit_list", "وحدات التعبئة"),
    ("inventory:item_list", "الأصناف"),
    ("inventory:conversion_list", "تحويلات وحدات الصنف"),
    ("inventory:warehouse_list", "المخازن"),
    ("inventory:stock_list", "المخزون المتوفر"),
    ("inventory:movement_list", "حركة المخزون"),
    ("inventory:opening_list", "الأرصدة الافتتاحية"),
    ("inventory:mapping_list", "ربط حسابات المخزون"),
    ("inventory:inventory_issue_list", "صرف مخزني للاستهلاك"),
    ("inventory:transfer_list", "التحويلات المخزنية"),
    ("inventory:inventory_waste_list", "إتلاف مخزني"),
    ("inventory:count_list", "الجرد الفعلي"),
    ("inventory:adjustment_list", "التسويات المخزنية"),
]


def resolve_user(selector: str) -> User:
    """
    The one user this selector names — by username, email, or id.

    Ambiguity is an error, never a choice. `--user 3` could mean the user whose
    id is 3 or the one whose username is "3"; if both exist the command stops
    and says so rather than deciding.
    """
    selector = selector.strip()
    if not selector:
        raise DemoSelectionError("--user is empty.")

    matches = Q(username__iexact=selector) | Q(email__iexact=selector)
    if selector.isdigit():
        matches |= Q(pk=int(selector))
    found = list(User.objects.filter(matches).order_by("pk")[:5])

    if not found:
        known = ", ".join(User.objects.order_by("username").values_list("username", flat=True))
        raise DemoSelectionError(f"No user matches {selector!r}. Known users: {known or 'none'}.")
    if len(found) > 1:
        candidates = ", ".join(f"{user.pk}:{user.username}" for user in found)
        raise DemoSelectionError(
            f"{selector!r} matches more than one user ({candidates}). Use the id."
        )
    return found[0]


class Command(SeedCommand):
    help = (
        "Seed the inventory demo dataset (DEBUG only). Master data, posted opening "
        "stock, receipts, issues, returns, a reversal, transfers, waste, counts and "
        "adjustments — all through the real domain services."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--user",
            required=True,
            help="Username, email, or id of the person who will sign in to review this.",
        )
        parser.add_argument(
            "--organization",
            default=DEMO_ORGANIZATION_CODE,
            help=f"Organization code. Default {DEMO_ORGANIZATION_CODE}, created on demand.",
        )
        parser.add_argument(
            "--source-branch",
            default=SOURCE_BRANCH_CODE,
            help=f"Branch the stock starts in. Default {SOURCE_BRANCH_CODE}.",
        )
        parser.add_argument(
            "--destination-branch",
            default=DESTINATION_BRANCH_CODE,
            help=f"Branch the cross-branch transfers go to. Default {DESTINATION_BRANCH_CODE}.",
        )
        parser.add_argument(
            "--confirm-demo",
            action="store_true",
            help="Required before anything posts. Posted movements and journals cannot be deleted.",
        )
        parser.add_argument(
            "--reset-demo",
            action="store_true",
            help=f"Remove what can legitimately be removed from the {NAMESPACE} namespace first.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        # First, before an argument is read: a demo dataset in production would
        # be indistinguishable from real stock in every report the business runs.
        if not settings.DEBUG:
            raise CommandError(
                "seed_inventory_demo runs only with DEBUG=True. This posts business "
                "documents, and posted stock and accounting effects are append-only."
            )

        try:
            user = resolve_user(options["user"])
        except DemoSelectionError as problem:
            raise CommandError(str(problem)) from problem

        organization_code = options["organization"].strip().upper()

        if options["reset_demo"]:
            self._reset(organization_code)

        confirmed = options["confirm_demo"]
        if not confirmed:
            self.write(
                "--confirm-demo not given: seeding master data only, posting nothing.\n"
                "  Master data can be recreated. A posted movement and its journal cannot."
            )

        try:
            with audit_context(actor=user):
                result = seed_inventory_demo(
                    user=user,
                    organization_code=organization_code,
                    source_branch_code=options["source_branch"].strip().upper(),
                    destination_branch_code=options["destination_branch"].strip().upper(),
                    with_operations=confirmed,
                )
        except DemoSelectionError as problem:
            raise CommandError(str(problem)) from problem

        self._report(result, posted=confirmed)

    # -- output ------------------------------------------------------------

    def _reset(self, organization_code: str) -> None:
        report = reset_demo(organization_code=organization_code)
        self.write(f"Reset {NAMESPACE}:")
        if report.refused:
            self.write(f"  refused: {report.refused}")
        for line in report.removed:
            self.write(f"  removed {line}")
        for line in report.kept:
            self.write(f"  kept    {line}")
        if not (report.removed or report.kept or report.refused):
            self.write("  nothing to remove.")
        self.write("")

    def _report(self, result: DemoResult, *, posted: bool) -> None:
        log = result.log
        self.write("")
        self.write(f"Namespace     {NAMESPACE}")
        self.write(f"Organization  {result.organization.code} — {result.organization.name}")
        self.write(f"Branches      {result.source_branch.code} -> {result.destination_branch.code}")
        self.write(f"Business date {result.business_date.isoformat()}")
        self.write("")

        for kind in (
            "organization",
            "branch",
            "accounting",
            "account mapping",
            "inventory mapping",
            "category",
            "package unit",
            "item",
            "conversion",
            "warehouse",
            "location",
            "branch item",
            "reason code",
            "user",
            "access",
            "opening",
            "receipt",
            "issue",
            "return",
            "reversal",
            "transfer",
            "waste",
            "stock count",
            "adjustment",
            "draft",
            "location stock",
        ):
            rows = log.of_kind(kind)
            if not rows:
                continue
            self.write(f"{kind}:")
            for _, label, state in rows:
                self.write(f"  {state:<8} {label}")

        self.write("")
        self.write(f"{log.created} created, {log.reused} reused.")

        self.write("")
        self.write(f"Sign in as   {result.user.username}")
        self.write(f"Count sheets were entered by {result.conductor.username} (no usable password;")
        self.write("  run `manage.py changepassword demo-storekeeper` to sign in as them).")

        if not posted:
            self.write("")
            self.write(
                "No documents were posted. Re-run with --confirm-demo for the full scenario."
            )
            return

        self.write("")
        self.write("Screens to inspect (python manage.py runserver, then):")
        for route, label in INSPECTION_ROUTES:
            self.write(f"  http://127.0.0.1:8000{reverse(route):<34} {label}")
