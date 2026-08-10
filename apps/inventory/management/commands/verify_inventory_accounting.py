"""
Verify inventory against itself and against the general ledger. Read-only.

Three comparisons per organization: each posted opening against its own
movements and journal, the balance projection against the ledger replay, and
current inventory book value against the GL control accounts the effects
actually posted to.

**There is no repair mode, deliberately.** A mismatch is a defect: the honest
response is to report it and investigate, and a command that could overwrite
the projection or post a balancing journal would erase the evidence it exists
to find.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.inventory.reconciliation import verify_inventory_accounting
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = "Verify openings, stock balances, and the GL against each other. Read-only."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--organization",
            help="Organization code to verify. Omitted, every active organization is verified.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        code = options.get("organization")
        organizations = Organization.objects.filter(is_active=True).order_by("code")
        if code:
            organizations = organizations.filter(code=code)
            if not organizations.exists():
                raise SystemExit(f"organization {code!r} does not exist")

        failed = False
        for organization in organizations:
            problems = verify_inventory_accounting(organization)
            if not problems:
                self.stdout.write(self.style.SUCCESS(f"{organization.code}: OK"))
                continue
            failed = True
            self.stdout.write(self.style.ERROR(f"{organization.code}: {len(problems)} mismatches"))
            for line in problems:
                self.stdout.write(f"  {line}")

        if failed:
            # A non-zero exit so a scheduled run fails loudly. Nothing was
            # modified; a mismatch is investigated, never auto-repaired.
            raise SystemExit(1)
