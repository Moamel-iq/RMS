r"""
Check production drafts and report every disagreement.

    .venv\Scripts\python.exe manage.py verify_production_drafts
    .venv\Scripts\python.exe manage.py verify_production_drafts --organization KM

Reads each draft and checks that it still says what it was drafted to say: the
organization / branch / warehouse / recipe / version identities, the recipe
shape, the requirement paths against a fresh expansion of the version, each
planned quantity against `source × cumulative × batch multiplier`, duplicate
paths, dropped or over-expanded components, every actual item against what
somebody actually approved, complete conversion snapshots, and the absence of
anything Task 3.5 owns.

## What it deliberately does not check

**Stock.** Availability, lots, expiry and locations are Task 3.5's, at posting.
A verifier reporting "not enough rice" would be reporting a fact about Tuesday
afternoon rather than a defect in a document, and a red list of those stops
being read within a week.

## Verify only, and why there is no `--repair`

Nothing here writes, and the frozen columns refuse an `UPDATE` at the database
anyway. A draft that disagrees with itself is evidence that something wrote it
wrongly or reached behind a trigger; smoothing it over would erase the evidence
that the question ever existed. The answer is to discard the draft and draft
again — one command, and it leaves an audit trail.

Exit code 1 when anything disagrees. Exit code 2 for a selector that names
nothing, because "checked an organization that does not exist" must not look
like "clean".
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import CommandParser

from apps.core.console import SeedCommand
from apps.kitchen.production_reconciliation import drafts_checked, verify_production_drafts
from apps.organizations.models import Organization


class Command(SeedCommand):
    help = (
        "Report production drafts that no longer agree with the recipe they were "
        "drafted from: paths, planned quantities, approved substitutes, conversion "
        "snapshots and identities. Read-only; there is no repair mode."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--organization",
            dest="organization_code",
            default="",
            help="Organization code. Default: every organization.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        code = str(options.get("organization_code") or "").strip().upper()
        organizations = Organization.objects.all().order_by("code")
        if code:
            organizations = organizations.filter(code=code)
            if not organizations.exists():
                self.write(f"No organization with code {code}.")
                raise SystemExit(2)

        total_defects = 0
        total_observations = 0
        total_drafts = 0
        for organization in organizations:
            findings = verify_production_drafts(organization)
            defects = [row for row in findings if row.is_blocking]
            observations = [row for row in findings if not row.is_blocking]
            checked = drafts_checked(organization)
            total_drafts += checked
            total_defects += len(defects)
            total_observations += len(observations)

            self.write("")
            self.write(f"{organization.code} - {organization.name}")
            self.write(f"  production drafts checked: {checked}")
            for finding in defects:
                self.write(f"  DEFECT      {finding.code}  batch {finding.batch_id}")
                self.write(f"              {finding.message}")
            # Reported and never counted against the exit status. A
            # cross-dimension substitution is a legitimate thing for a kitchen to
            # do, and a verifier that exited non-zero on a correct database would
            # train everybody to ignore it.
            for finding in observations:
                self.write(f"  OBSERVATION {finding.code}  batch {finding.batch_id}")
                self.write(f"              {finding.message}")
            if not findings:
                self.write("  clean")

        self.write("")
        self.write("Stock availability is not checked here; posting is Task 3.5's.")
        if total_observations:
            self.write(f"{total_observations} observation(s) — reported, not defects.")
        if total_defects:
            self.write(f"{total_defects} defect(s) across {total_drafts} draft(s).")
            raise SystemExit(1)
        self.write(f"No defects. {total_drafts} draft(s) checked.")
