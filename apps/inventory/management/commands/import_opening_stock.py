r"""
Load an opening stock document from a transcribed file.

    .venv\Scripts\python.exe manage.py import_opening_stock ^
        --organization 01 --branch 011 --warehouse MAIN ^
        --file "<a path outside this repository>\opening-stock.json" ^
        --actor <username> --cutoff 2026-08-20

Same rule as the other importers: the code ships, the figures do not. What one
kitchen pays for cardamom is a competitor's homework, and this repository has
a remote.

The document is created and its lines added; **it is not posted**. Posting an
opening balance moves stock and writes to the ledger, and a loader that did
that on its own would be deciding something the storekeeper and the accountant
are supposed to sign for. It is left where the normal submit-and-post
lifecycle can pick it up.

Quantities are entered in the item's own base unit, because an opening line
carries a base quantity and a unit cost and nothing else — there is no
conversion to get wrong here, only a mapping to get right, and that mapping is
in the file where a human can read it.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.core.console import SeedCommand
from apps.inventory import opening
from apps.inventory.commands import add_opening_line, create_opening
from apps.inventory.models import InventoryItem, Warehouse
from apps.organizations.models import Branch, Organization
from apps.users.models import User


class Command(SeedCommand):
    help = "Import an opening stock document from a file outside this repository."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True)
        parser.add_argument("--branch", required=True)
        parser.add_argument("--warehouse", required=True)
        parser.add_argument("--file", required=True)
        parser.add_argument("--actor", required=True, help="Username to record as the author.")
        parser.add_argument("--cutoff", required=True, help="YYYY-MM-DD")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        organization = Organization.objects.filter(code=options["organization"]).first()
        if organization is None:
            raise CommandError(f"No organization {options['organization']}.")
        branch = Branch.objects.filter(organization=organization, code=options["branch"]).first()
        if branch is None:
            raise CommandError(f"No branch {options['branch']}.")
        warehouse = Warehouse.objects.filter(branch=branch, code=options["warehouse"]).first()
        if warehouse is None:
            raise CommandError(f"No warehouse {options['warehouse']} at branch {branch.code}.")
        actor = User.objects.filter(username=options["actor"]).first()
        if actor is None:
            raise CommandError(f"No user {options['actor']}.")

        path = pathlib.Path(options["file"])
        if not path.is_file():
            raise CommandError(f"{path} is missing.")
        payload = json.loads(path.read_text(encoding="utf-8"))

        day = datetime.date.fromisoformat(options["cutoff"])
        cutoff = timezone.make_aware(datetime.datetime.combine(day, datetime.time(0, 0)))

        added = 0
        skipped: list[tuple[str, str]] = []

        with transaction.atomic():
            document = create_opening(
                actor=actor,
                organization=organization,
                branch=branch,
                cutoff_at=cutoff,
                evidence_reference="فواتير أهل الخير 3198 و MEN GROUP 73",
                narration="رصيد افتتاحي بكميات الفواتير",
            )

            for row in payload["lines"]:
                item = InventoryItem.objects.filter(
                    organization=organization, name_ar=row["item"]
                ).first()
                if item is None:
                    skipped.append((row["item"], "لا صنف بهذا الاسم"))
                    continue
                if item.base_unit.code != row["unit"]:
                    # Refused rather than converted. The file states the unit it
                    # counted in; if that is not the unit the item is stocked in,
                    # the mapping is wrong and a silent conversion would bury it.
                    skipped.append(
                        (
                            row["item"],
                            f"وحدة الملف {row['unit']} ≠ وحدة الصنف {item.base_unit.code}",
                        )
                    )
                    continue
                try:
                    add_opening_line(
                        actor=actor,
                        document=document,
                        line=opening.OpeningLineInput(
                            warehouse=warehouse,
                            item=item,
                            unit_cost=Decimal(row["unit_cost"]),
                            base_quantity=Decimal(row["qty"]),
                        ),
                    )
                    added += 1
                except ValidationError as refused:
                    skipped.append((row["item"], "; ".join(refused.messages)))

            total = sum(
                Decimal(r["qty"]) * Decimal(r["unit_cost"])
                for r in payload["lines"]
                if not any(r["item"] == s[0] for s in skipped)
            )

            self.write("")
            self.write(f"=== رصيد افتتاحي · {organization.code} · {warehouse.code} ===")
            self.write(f"  المستند   : {document.pk} — حالته {document.status}")
            self.write(f"  سطور      : {added} من {len(payload['lines'])}")
            self.write(f"  قيمة المخزون: {total:,.0f} دينار")
            if skipped:
                self.write("")
                self.write("  لم تُضف:")
                for name, why in skipped:
                    self.write(f"    · {name} — {why}")
            self.write("")
            self.write("  المستند لم يُرحَّل. الترحيل يحرّك المخزون ويكتب في الدفتر،")
            self.write("  وهو توقيع أمين المخزن والمحاسب لا توقيع المستورِد.")

            if options["dry_run"]:
                self.write("")
                self.write("dry run — rolled back.")
                transaction.set_rollback(True)
