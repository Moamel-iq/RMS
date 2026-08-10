"""
Replay the stock ledger and report where the projection disagrees.

Read-only. There is no repair mode and no `--fix`, because a divergence
between the ledger and its projection is a **defect**, and a command that
silently rewrote the projection would destroy the evidence of whatever caused
it — usually the most useful thing in the incident.

    manage.py verify_stock_ledger
    manage.py verify_stock_ledger --organization KM

Exit code 1 when anything disagrees, so CI and cron notice without anybody
reading the output.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import CommandParser

from apps.core.console import SeedCommand
from apps.inventory.reconciliation import verify_organization
from apps.organizations.models import Organization


class Command(SeedCommand):
    help = "Replay the stock ledger and report any divergence from StockBalance."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--organization",
            dest="organization_code",
            default="",
            help="Limit the check to one organization code. Default: every organization.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        code = str(options.get("organization_code") or "").strip().upper()
        organizations = Organization.objects.all().order_by("code")
        if code:
            organizations = organizations.filter(code=code)
            if not organizations.exists():
                self.write(f"No organization with code {code}.")
                raise SystemExit(2)

        total = 0
        for organization in organizations:
            mismatches = verify_organization(organization)
            total += len(mismatches)
            if mismatches:
                self.write(f"{organization.code}: {len(mismatches)} mismatch(es)")
                for mismatch in mismatches:
                    self.write(f"  ! {mismatch}")
            else:
                self.write(f"{organization.code}: ledger and balances agree.")

        if total:
            self.write("")
            self.write(
                f"{total} mismatch(es). This is a defect, not drift — the balances are a "
                "projection of the movements and cannot legitimately differ from them. "
                "Investigate before posting anything further; do not repair the "
                "projection by hand."
            )
            raise SystemExit(1)
