r"""
Check stored recipe cost snapshots and report every disagreement.

    .venv\Scripts\python.exe manage.py verify_recipe_cost_snapshots
    .venv\Scripts\python.exe manage.py verify_recipe_cost_snapshots --organization KM
    .venv\Scripts\python.exe manage.py verify_recipe_cost_snapshots --recompute

Reads each snapshot and checks that it still agrees with itself: the header
total against its lines, the three cost-class totals against the lines of each
class, the line numbering, duplicate paths, every stored extension against
`quantity x unit cost`, each unit cost against the valuation evidence beside it,
the organization / branch / warehouse / version identities, the calculation
version, the valuation mode, and each serving scenario's allocation against the
recipe total.

## What it deliberately does not check

**A snapshot is never compared against today's inventory.** Stock moved; that
is what stock does, and a March snapshot whose items cost more in September is
correct in every particular. Reporting that difference would produce a red list
nobody could act on, and therefore nobody would read.

`--recompute` is the separate, explicit mode: it re-reads the ledger at each
snapshot's own recorded `ledger_cutoff_sequence` and re-derives the unit costs.
That is still not a comparison against today — the cutoff is the snapshot's own
— and it is off by default because it is a query per item and only meaningful
while the movements behind that cutoff are still present.

## Verify only, and why there is no `--repair`

Nothing here writes. The three snapshot tables refuse UPDATE and DELETE at the
database anyway, so a repair mode could not run even if somebody wrote one — and
that is the point rather than an obstacle. A costing record that disagrees with
itself is evidence that something wrote it wrongly or reached behind the
trigger, and smoothing it over would erase the evidence that the question ever
existed. The answer is a new snapshot, taken by a person, with a reason.

Exit code 1 when anything disagrees. Exit code 2 for a selector that names
nothing, because "checked an organization that does not exist" must not look
like "clean".
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import CommandParser

from apps.core.console import SeedCommand
from apps.kitchen.cost_reconciliation import (
    recompute_findings,
    snapshots_checked,
    verify_cost_snapshots,
)
from apps.kitchen.models import RecipeCostSnapshot
from apps.organizations.models import Organization


class Command(SeedCommand):
    help = (
        "Report cost snapshots that no longer agree with themselves: totals against "
        "lines, class splits, line ordering, extensions, valuation evidence, "
        "identities and serving allocations. Read-only; there is no repair mode."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--organization",
            dest="organization_code",
            default="",
            help="Organization code. Default: every organization.",
        )
        parser.add_argument(
            "--recompute",
            action="store_true",
            help=(
                "Also re-read the ledger at each snapshot's recorded cutoff and "
                "re-derive its unit costs. Slower, and only meaningful while the "
                "movements behind that cutoff still exist."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        code = str(options.get("organization_code") or "").strip().upper()
        recompute = bool(options.get("recompute"))
        organizations = Organization.objects.all().order_by("code")
        if code:
            organizations = organizations.filter(code=code)
            if not organizations.exists():
                self.write(f"No organization with code {code}.")
                raise SystemExit(2)

        total_findings = 0
        total_snapshots = 0
        for organization in organizations:
            findings = verify_cost_snapshots(organization)
            if recompute:
                for snapshot in (
                    RecipeCostSnapshot.objects.filter(organization=organization)
                    .select_related("organization", "recipe", "warehouse")
                    .order_by("pk")
                ):
                    findings.extend(recompute_findings(snapshot))
            checked = snapshots_checked(organization)
            total_snapshots += checked
            total_findings += len(findings)

            self.write("")
            self.write(f"{organization.code} - {organization.name}")
            self.write(f"  snapshots checked: {checked}")
            if findings:
                for finding in findings:
                    self.write(f"  {finding}")
            else:
                self.write("  clean")

        self.write("")
        if recompute:
            self.write("Recomputation ran at each snapshot's own recorded ledger cutoff.")
        if total_findings:
            self.write(f"{total_findings} finding(s) across {total_snapshots} snapshot(s).")
            raise SystemExit(1)
        self.write(f"No findings. {total_snapshots} snapshot(s) checked.")
