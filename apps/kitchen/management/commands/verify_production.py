"""
Verify production drafts **and** postings, in one read-only pass.

`verify_production_drafts` stays exactly as it is and still answers its own
question — what a draft still has to fix before it can post. This command
composes it with the posting verifier so an operator asking "is production
sound?" gets one answer instead of two commands and a mental join.

The distinction it preserves is the one that keeps a verifier readable:

* **DEFECT** — a real disagreement between the batch, the stock ledger and the
  general ledger. Counted against the exit status.
* **OBSERVATION** — something true and worth saying that is not wrong. A
  kitchen substituting across dimensions is the standing example: RCP-022
  approves items, never conversions, so a correct database can contain one
  forever. Exiting non-zero on it would train everybody to ignore the output,
  and the real defects would go unread with it.

Read-only. There is no repair mode and no flag that adds one.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import CommandParser

from apps.core.console import SeedCommand
from apps.kitchen.production_posting_reconciliation import (
    posted_batches_checked,
    verify_production,
)
from apps.kitchen.production_reconciliation import drafts_checked
from apps.organizations.models import Organization


class Command(SeedCommand):
    help = (
        "Report production drafts that no longer agree with their recipe, and posted "
        "batches whose stock ledger, journal or reversal disagrees with the batch. "
        "Read-only; there is no repair mode."
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
        total_batches = 0
        for organization in organizations:
            findings = verify_production(organization)
            defects = [row for row in findings if row.is_blocking]
            observations = [row for row in findings if not row.is_blocking]
            drafts = drafts_checked(organization)
            posted = posted_batches_checked(organization)
            total_batches += drafts + posted
            total_defects += len(defects)
            total_observations += len(observations)

            self.write("")
            self.write(f"{organization.code} - {organization.name}")
            self.write(f"  drafts checked:  {drafts}")
            self.write(f"  postings checked: {posted}")
            for finding in defects:
                self.write(f"  DEFECT      {finding.code}  batch {finding.batch_id}")
                self.write(f"              {finding.message}")
            for finding in observations:
                self.write(f"  OBSERVATION {finding.code}  batch {finding.batch_id}")
                self.write(f"              {finding.message}")
            if not findings:
                self.write("  clean")

        self.write("")
        self.write(
            "A posted batch with no journal is checked by recomputing its per-account "
            "nets, not by reading the column: a journal that is rightly absent and one "
            "that is wrongly missing look identical from the outside."
        )
        if total_observations:
            self.write(f"{total_observations} observation(s) - reported, not defects.")
        if total_defects:
            self.write(f"{total_defects} defect(s) across {total_batches} batch(es).")
            raise SystemExit(1)
        self.write(f"No defects. {total_batches} batch(es) checked.")
