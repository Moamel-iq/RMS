"""Import the reviewed account hierarchy without source balances or metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounting.models import ImportedChartAccount
from apps.organizations.models import Organization

DEFAULT_SOURCE = "Khan Mandi workbook 2026-08-29"
DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data" / "imported_chart_20260829.json"


class Command(BaseCommand):
    help = "Import the reviewed account hierarchy without workbook balances or metadata."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--organization", required=True, help="Organization code")
        parser.add_argument("--source", default=DEFAULT_SOURCE, help="Visible source label")
        parser.add_argument(
            "--data", default=str(DEFAULT_DATA), help="Path to normalized JSON rows"
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        try:
            organization = Organization.objects.get(code=options["organization"])
        except Organization.DoesNotExist as error:
            raise CommandError("Organization does not exist.") from error

        data_path = Path(options["data"])
        if not data_path.is_file():
            raise CommandError(f"Chart data file does not exist: {data_path}")
        rows = json.loads(data_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or not rows:
            raise CommandError("Chart data must be a non-empty JSON list.")

        source = str(options["source"]).strip()
        records: dict[str, ImportedChartAccount] = {}
        created = updated = 0
        for row in rows:
            source_code = str(row["source_code"]).strip()
            account, was_created = ImportedChartAccount.objects.update_or_create(
                organization=organization,
                source_system=source,
                source_code=source_code,
                defaults={
                    "name": str(row["name"]).strip(),
                    # The workbook is now the approved account hierarchy only.
                    # Figures and classification columns belong to its former
                    # accounting period and must never be restored by re-import.
                    "statement_name": "",
                    "category": "",
                    "currency": "",
                    "source_debit": 0,
                    "source_credit": 0,
                    "source_balance": 0,
                    "organizer": "",
                    "is_leaf": bool(row.get("is_leaf", False)),
                },
            )
            records[source_code] = account
            created += int(was_created)
            updated += int(not was_created)

        for row in rows:
            account = records[str(row["source_code"]).strip()]
            parent_code = str(row.get("parent_source_code", "")).strip()
            parent = records.get(parent_code)
            if account.parent_id != (parent.pk if parent else None):
                account.parent = parent
                account.save(update_fields=["parent", "updated_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(records)} source accounts for {organization.code} "
                f"({created} created, {updated} refreshed)."
            )
        )
