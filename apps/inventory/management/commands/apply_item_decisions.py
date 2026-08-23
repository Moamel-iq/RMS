r"""
Apply the owner's decisions to the item register: units, retirements, types.

    .venv\Scripts\python.exe manage.py apply_item_decisions ^
        --organization 01 ^
        --file "<a path outside this repository>\owner-item-decisions.json"

`import_stock_items` loaded the register as the owner's sheet had it. Reading
the recipe book against that register then surfaced four kinds of question only
the owner could answer — which unit a spice is really bought in, which of nine
rice rows the kitchen actually uses, which drinks are resold rather than cooked,
and which production outputs deserve a row of their own. This command carries
those answers back, one file, each entry with the sentence it came from.

Four operations, and each refuses rather than improvises:

* **`base_unit_corrections`** go through `correct_unused_item_base_unit`, which
  refuses an item that anything already references. A base unit changes what
  every stored quantity *means*; on a used item the answer is a replacement row,
  not an edit.
* **`archive`** sets `is_active=False`. It deletes nothing: the rows keep their
  history and stop being offerable. Nine rice rows retiring behind two is a
  master-data decision, not a correction, so the reason travels with each one.
* **`reclassify`** changes `item_type` only. Direct-stock selling requires
  `GOODS_FOR_RESALE`, and the check that enforces it lives in Sales; this is
  where the item earns the classification.
* **`create`** adds production outputs — `SEMI_FINISHED` rows a batch recipe can
  name as its output. It will not create a `RAW_MATERIAL`: the owner's standing
  instruction is that missing ingredients get reported, never invented, and the
  type check here is what keeps this command from becoming the back door.

Re-running is safe. Every operation compares against the current row first and
reports "unchanged" rather than writing again.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from apps.core.console import SeedCommand
from apps.inventory.models import InventoryItem, ItemCategory, ItemType
from apps.inventory.services import correct_unused_item_base_unit, create_item, update_item
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure

#: Types this command may bring into existence. A production output is a row the
#: kitchen fills by cooking; a raw material is a row a supplier fills by
#: delivering, and only a purchase document may introduce one of those.
CREATABLE = frozenset({ItemType.SEMI_FINISHED.value, ItemType.FINISHED_GOOD.value})


class Command(SeedCommand):
    help = "Apply owner decisions to the inventory item register."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True, help="Organization code.")
        parser.add_argument("--file", required=True, help="Path to owner-item-decisions.json.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and roll back.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        organization = Organization.objects.filter(code=options["organization"]).first()
        if organization is None:
            raise CommandError(f"No organization with code {options['organization']}.")

        path = pathlib.Path(options["file"])
        if not path.is_file():
            raise CommandError(f"{path} is missing.")
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

        with transaction.atomic():
            report: list[str] = []
            report += self._correct_units(organization, payload)
            report += self._archive(organization, payload)
            report += self._reclassify(organization, payload)
            report += self._create(organization, payload)

            self.write("")
            self.write(f"=== {organization.code} — {organization.name_ar} ===")
            for line in report:
                self.write(f"  {line}")

            if options["dry_run"]:
                self.write("")
                self.write("dry run — rolled back, nothing was kept.")
                transaction.set_rollback(True)

    # -- helpers -------------------------------------------------------------

    def _item(self, organization: Organization, code: str) -> InventoryItem:
        item = InventoryItem.objects.filter(organization=organization, code=code).first()
        if item is None:
            raise CommandError(f"{code} is not in the item master.")
        return item

    def _unit(self, code: str) -> UnitOfMeasure:
        unit = UnitOfMeasure.objects.filter(code=code).first()
        if unit is None:
            raise CommandError(f"Unit {code} does not exist — run seed_units first.")
        return unit

    # -- operations ----------------------------------------------------------

    def _correct_units(self, organization: Organization, payload: dict[str, Any]) -> list[str]:
        lines = []
        for row in payload.get("base_unit_corrections", []):
            item = self._item(organization, row["code"])
            unit = self._unit(row["base_unit"])
            if item.base_unit_id == unit.pk:
                lines.append(f"= {item.code} {item.name_ar} — الوحدة {unit.code} أصلاً")
                continue
            before = item.base_unit.code
            try:
                correct_unused_item_base_unit(item=item, base_unit=unit, reason=row["reason"])
            except ValidationError as error:
                raise CommandError(f"{item.code}: {error.messages}") from error
            lines.append(f"~ {item.code} {item.name_ar} — الوحدة {before} ← {unit.code}")
        return lines

    def _archive(self, organization: Organization, payload: dict[str, Any]) -> list[str]:
        lines = []
        for row in payload.get("archive", []):
            item = self._item(organization, row["code"])
            if not item.is_active:
                lines.append(f"= {item.code} {item.name_ar} — مؤرشف أصلاً")
                continue
            note = "\n".join(part for part in (item.notes, row["reason"]) if part)
            update_item(
                item=item,
                name_ar=item.name_ar,
                name_en=item.name_en,
                category=item.category,
                item_type=item.item_type,
                notes=note,
                is_active=False,
            )
            lines.append(f"- {item.code} {item.name_ar} — أُرشف")
        return lines

    def _reclassify(self, organization: Organization, payload: dict[str, Any]) -> list[str]:
        lines = []
        for row in payload.get("reclassify", []):
            item = self._item(organization, row["code"])
            wanted = row["item_type"]
            if item.item_type == wanted:
                lines.append(f"= {item.code} {item.name_ar} — نوعه {wanted} أصلاً")
                continue
            before = item.item_type
            note = "\n".join(part for part in (item.notes, row.get("note_ar", "")) if part)
            update_item(
                item=item,
                name_ar=item.name_ar,
                name_en=item.name_en,
                category=item.category,
                item_type=wanted,
                notes=note,
                is_active=item.is_active,
            )
            lines.append(f"~ {item.code} {item.name_ar} — النوع {before} ← {wanted}")
        return lines

    def _create(self, organization: Organization, payload: dict[str, Any]) -> list[str]:
        lines = []
        for row in payload.get("create", []):
            if row["item_type"] not in CREATABLE:
                raise CommandError(
                    f"{row['code']}: this command creates production outputs only, "
                    f"not {row['item_type']}."
                )
            existing = InventoryItem.objects.filter(
                organization=organization, code=row["code"]
            ).first()
            if existing is not None:
                lines.append(f"= {existing.code} {existing.name_ar} — موجود أصلاً")
                continue
            category = ItemCategory.objects.filter(
                organization=organization, code=row["category"]
            ).first()
            if category is None:
                raise CommandError(f"Item category {row['category']} does not exist.")
            item = create_item(
                organization=organization,
                code=row["code"],
                name_ar=row["name_ar"],
                category=category,
                item_type=row["item_type"],
                base_unit=self._unit(row["base_unit"]),
                notes=row.get("notes_ar", ""),
            )
            lines.append(f"+ {item.code} {item.name_ar} — {item.item_type} / {row['base_unit']}")
        return lines
