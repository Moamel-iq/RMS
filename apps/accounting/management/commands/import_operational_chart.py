"""Install the approved operational chart without any monetary activity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounting.models import Account, JournalLine, ManualPostingPolicy
from apps.organizations.models import Organization

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data" / "operational_chart_local.json"


class Command(BaseCommand):
    """Import the Khan Mandi operating chart, never journal balances."""

    help = "Import the approved operational chart without journals, balances, or sales data."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--organization", required=True, help="Organization code")
        parser.add_argument(
            "--data", default=str(DEFAULT_DATA), help="Path to operational chart JSON"
        )

    @staticmethod
    def _read_rows(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            raise CommandError(f"Chart data file does not exist: {path}")

        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise CommandError("Operational chart data is not valid JSON.") from error

        if not isinstance(rows, list) or not rows:
            raise CommandError("Operational chart data must be a non-empty JSON list.")

        required = {
            "code",
            "name",
            "account_class",
            "parent_code",
            "is_postable",
            "requires_cost_center",
            "is_active",
            "manual_posting_policy",
            "is_system",
            "external_accounting_system",
            "external_account_code",
        }
        codes: set[str] = set()
        children: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for position, raw in enumerate(rows, start=1):
            if not isinstance(raw, dict) or required - raw.keys():
                raise CommandError(f"Operational chart row {position} is incomplete.")

            row = {field: raw[field] for field in required}
            row["code"] = str(row["code"]).strip()
            row["name"] = str(row["name"]).strip()
            row["parent_code"] = str(row["parent_code"]).strip()
            row["account_class"] = str(row["account_class"]).strip()
            row["manual_posting_policy"] = str(row["manual_posting_policy"]).strip()
            row["external_accounting_system"] = str(row["external_accounting_system"]).strip()
            row["external_account_code"] = str(row["external_account_code"]).strip()

            if not row["code"] or not row["name"] or row["code"] in codes:
                raise CommandError(f"Operational chart row {position} has an invalid code or name.")
            if row["account_class"] != row["code"][0]:
                raise CommandError(f"Operational account {row['code']} has the wrong class.")
            if row["manual_posting_policy"] not in ManualPostingPolicy.values:
                raise CommandError(
                    f"Operational account {row['code']} has an unknown posting policy."
                )
            if (
                not isinstance(row["is_postable"], bool)
                or not isinstance(row["requires_cost_center"], bool)
                or not isinstance(row["is_active"], bool)
                or not isinstance(row["is_system"], bool)
            ):
                raise CommandError(f"Operational account {row['code']} has invalid flags.")
            if not row["is_active"]:
                raise CommandError(
                    f"Operational account {row['code']} is inactive and needs an archive date."
                )
            if row["requires_cost_center"] and not row["is_postable"]:
                raise CommandError(
                    f"Operational rollup {row['code']} cannot require a cost center."
                )
            if (
                not row["is_postable"]
                and row["manual_posting_policy"] != ManualPostingPolicy.ALLOWED
            ):
                raise CommandError(
                    f"Operational rollup {row['code']} cannot restrict manual posting."
                )
            if bool(row["external_accounting_system"]) != bool(row["external_account_code"]):
                raise CommandError(
                    f"Operational account {row['code']} has an incomplete external mapping."
                )
            if row["parent_code"] == row["code"]:
                raise CommandError(f"Operational account {row['code']} cannot parent itself.")

            codes.add(row["code"])
            if row["parent_code"]:
                children.add(row["parent_code"])
            normalized.append(row)

        unknown_parents = children - codes
        if unknown_parents:
            raise CommandError(
                "Operational chart has unknown parent codes: " + ", ".join(sorted(unknown_parents))
            )
        for row in normalized:
            if row["is_postable"] and row["code"] in children:
                raise CommandError(
                    f"Operational posting account {row['code']} cannot have children."
                )
        return normalized

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        try:
            organization = Organization.objects.get(code=options["organization"])
        except Organization.DoesNotExist as error:
            raise CommandError("Organization does not exist.") from error

        rows = self._read_rows(Path(options["data"]))
        source_codes = {row["code"] for row in rows}
        existing = {
            account.code: account
            for account in Account.objects.select_for_update().filter(organization=organization)
        }
        if JournalLine.objects.filter(account__organization=organization).exists():
            raise CommandError(
                "The operational chart cannot be replaced because this organization has journal lines."
            )
        if existing and set(existing) != source_codes:
            raise CommandError(
                "The operational chart cannot be mixed with a different existing chart."
            )

        if existing:
            for row in rows:
                account = existing[row["code"]]
                current = {
                    "name": account.name,
                    "account_class": account.account_class,
                    "parent_code": account.parent.code if account.parent_id else "",
                    "is_postable": account.is_postable,
                    "requires_cost_center": account.requires_cost_center,
                    "is_active": account.is_active,
                    "manual_posting_policy": account.manual_posting_policy,
                    "is_system": account.is_system,
                    "external_accounting_system": account.external_accounting_system,
                    "external_account_code": account.external_account_code,
                }
                expected = {key: row[key] for key in current}
                if current != expected:
                    raise CommandError(
                        f"Existing operational account {account.code} does not match the approved chart."
                    )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Operational chart already matches the approved structure for {organization.code} "
                    f"({len(existing)} accounts, no journal lines)."
                )
            )
            return

        records: dict[str, Account] = {}
        for row in rows:
            parent = records.get(row["parent_code"])
            if row["parent_code"] and parent is None:
                raise CommandError(
                    f"Operational account {row['code']} appears before its parent "
                    f"{row['parent_code']}."
                )
            records[row["code"]] = Account.objects.create(
                organization=organization,
                code=row["code"],
                name=row["name"],
                account_class=row["account_class"],
                parent=parent,
                is_postable=row["is_postable"],
                requires_cost_center=row["requires_cost_center"],
                is_active=row["is_active"],
                manual_posting_policy=row["manual_posting_policy"],
                is_system=bool(row["is_system"]),
                external_accounting_system=row["external_accounting_system"],
                external_account_code=row["external_account_code"],
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(records)} operational accounts for {organization.code}; "
                "no journals, sales, or balances were imported."
            )
        )
