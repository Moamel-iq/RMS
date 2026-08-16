r"""
Check the recipe version lifecycle and report every disagreement.

    .venv\Scripts\python.exe manage.py verify_recipe_versions
    .venv\Scripts\python.exe manage.py verify_recipe_versions --organization KM

Reads the versions, their reviews, their effective scope rows and the
supersession chain, and reports overlapping ranges, ambiguous resolution,
approvals with no evidence behind them, scope rows that have drifted from the
version they belong to, broken supersession links and half-filled provenance.

## Verify only, and why there is no `--repair`

Nothing here writes. An ambiguous effective range is not wear that a
maintenance job should smooth out; it is a claim that two different recipes
governed the same Tuesday at the same branch, and only a person who knows what
the kitchen actually cooked can say which one to close. A repair mode would
replace that question with a plausible answer and erase the evidence that the
question ever existed.

Exit code 1 when anything disagrees, so CI and cron notice without a person
reading the output. Exit code 2 for a selector that names nothing, because
"checked an organization that does not exist" must not look like "clean".
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import CommandParser

from apps.core.console import SeedCommand
from apps.kitchen.reconciliation import recipes_checked, verify_organization
from apps.organizations.models import Organization


class Command(SeedCommand):
    help = (
        "Report overlapping or ambiguous recipe version effective ranges, approvals "
        "without evidence, and broken supersession chains. Read-only; there is no "
        "repair mode."
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

        total_findings = 0
        total_recipes = 0
        for organization in organizations:
            findings = verify_organization(organization)
            checked = recipes_checked(organization)
            total_recipes += checked
            total_findings += len(findings)

            self.write("")
            self.write(f"{organization.code} - {organization.name_ar}")
            self.write(f"  recipes checked: {checked}")
            if not findings:
                self.write("  clean")
                continue
            for finding in findings:
                self.write(f"  {finding}")

        self.write("")
        if total_findings:
            self.write(f"{total_findings} finding(s) across {total_recipes} recipe(s).")
            raise SystemExit(1)
        self.write(f"No findings. {total_recipes} recipe(s) checked.")
