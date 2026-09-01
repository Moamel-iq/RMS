"""
Load the item master onto a fresh deployment, by code and never by id.

The dev database and the server are two different databases that happen to
describe one business. Their primary keys agree about nothing — the
organization is 3 here and 1 there, `KG` is a different row in each — so a
fixture carrying ids would either fail loudly on a collision or, far worse,
attach an item to whatever category happened to hold that number.

So the payload names things the way people do: `STK-0102`, `KG`, the
organization's own code. This command resolves each of those **on the machine
it is running on**, and writes through `create_item` / `create_item_category`
so every rule the master data has — leaf-only categories, active base units,
code format, the audit event — applies exactly as it would to somebody typing
the item into the screen.

## Idempotent, and what that costs

Re-running changes nothing. An existing code is counted as `unchanged` and
skipped rather than updated: this command exists to *populate* an empty
master, and silently rewriting a name somebody corrected on the server would
make the dev database the authority over production, which it is not. Use the
screens to edit; use this to seed.

## What it deliberately does not carry

**Archived items.** The export holds only `is_active=True` rows, because an
archived item is a decision that the business stopped using something, and
replaying that decision onto a system that never knew the item is noise in
the master and one more row in every picker.

Nor does it carry stock, costs, movements or balances. Those are ledger
facts, and the ledger only accepts what somebody posted through the services
that value it — an opening stock sheet, posted by a person.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandParser
from django.db import transaction

from apps.core.console import SeedCommand
from apps.inventory.models import InventoryItem, ItemCategory, PackageUnit
from apps.inventory.services import create_item, create_item_category, create_package_unit
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure

DATA_FILE = Path(__file__).resolve().parent / "data" / "master_items.json"


class _DryRunError(Exception):
    """Unwinds the load's own transaction without touching the caller's."""


class Command(SeedCommand):
    help = "Load item categories, package units and active items from the exported master."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--organization",
            default="",
            help="Organization code to load into. Defaults to the code in the file.",
        )
        parser.add_argument(
            "--file",
            default=str(DATA_FILE),
            help="Path to the exported master JSON.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be written and write nothing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        path = Path(options["file"])
        if not path.is_file():
            self.write(f"الملف غير موجود: {path}")
            return
        payload = json.loads(path.read_text(encoding="utf-8"))

        code = options["organization"] or payload.get("organization_code", "")
        organization = Organization.objects.filter(code=code).first()
        if organization is None:
            available = ", ".join(Organization.objects.values_list("code", flat=True)) or "لا شيء"
            self.write(f"لا توجد مؤسسة بالرمز {code!r}. المتاح: {available}")
            return

        self.write(f"المؤسسة: {organization.code} — {organization.name}")
        dry = bool(options["dry_run"])
        if dry:
            self.write("تشغيل تجريبي: لن يُكتب شيء.")

        # One transaction for the whole load: a half-loaded master is worse
        # than an empty one, because the gaps are invisible until somebody
        # cannot find an item.
        #
        # The dry run unwinds by raising rather than by `set_rollback`, which
        # marks the *outermost* block — inside a test, or inside any caller
        # that already opened a transaction, that poisons a transaction this
        # command does not own and every later query fails. Raising rolls back
        # exactly this block and leaves the caller's intact.
        try:
            with transaction.atomic():
                categories = self._categories(organization, payload["categories"], dry=dry)
                self._package_units(organization, payload.get("package_units", []), dry=dry)
                self._items(organization, payload["items"], categories, dry=dry)
                if dry:
                    raise _DryRunError
        except _DryRunError:
            self.write("انتهى التشغيل التجريبي. لم يُكتب شيء.")

    # -- categories ------------------------------------------------------
    def _categories(
        self, organization: Organization, rows: list[dict[str, Any]], *, dry: bool
    ) -> dict[str, ItemCategory]:
        created = skipped = 0
        known: dict[str, ItemCategory] = {
            c.code: c for c in ItemCategory.objects.filter(organization=organization)
        }
        # Shallowest first, so a parent always exists before its child asks
        # for it. The export is already ordered by depth; sorting again makes
        # the command independent of that.
        for row in sorted(rows, key=lambda r: r.get("depth", 0)):
            if row["code"] in known:
                skipped += 1
                continue
            if dry:
                # Registered unsaved, so the item pass that follows can still
                # resolve this category. Without it a dry run against an empty
                # server reported every item as refused for a missing category
                # — the exact opposite of the truth, and reported by the one
                # command an operator runs *to find out* whether it will work.
                known[row["code"]] = ItemCategory(
                    organization=organization, code=row["code"], name=row["name"]
                )
                created += 1
                continue
            parent = known.get(row["parent_code"]) if row.get("parent_code") else None
            category = create_item_category(
                organization=organization,
                code=row["code"],
                name=row["name"],
                parent=parent,
            )
            known[category.code] = category
            created += 1
        self.write(f"المجموعات: {created} جديدة، {skipped} موجودة.")
        return known

    # -- package units ---------------------------------------------------
    def _package_units(
        self, organization: Organization, rows: list[dict[str, Any]], *, dry: bool
    ) -> None:
        if not rows:
            return
        existing = set(
            PackageUnit.objects.filter(organization=organization).values_list("code", flat=True)
        )
        created = skipped = 0
        for row in rows:
            if row["code"] in existing:
                skipped += 1
                continue
            created += 1
            if not dry:
                create_package_unit(organization=organization, code=row["code"], name=row["name"])
        self.write(f"وحدات التعبئة: {created} جديدة، {skipped} موجودة.")

    # -- items -----------------------------------------------------------
    def _items(
        self,
        organization: Organization,
        rows: list[dict[str, Any]],
        categories: dict[str, ItemCategory],
        *,
        dry: bool,
    ) -> None:
        existing = set(
            InventoryItem.objects.filter(organization=organization).values_list("code", flat=True)
        )
        units = {u.code: u for u in UnitOfMeasure.objects.all()}
        created = skipped = 0
        refused: list[str] = []

        for row in rows:
            if row["code"] in existing:
                skipped += 1
                continue
            category = categories.get(row["category_code"])
            unit = units.get(row["base_unit_code"])
            if category is None:
                refused.append(f"{row['code']}: لا توجد مجموعة {row['category_code']}")
                continue
            if unit is None:
                refused.append(f"{row['code']}: لا توجد وحدة {row['base_unit_code']}")
                continue
            if dry:
                created += 1
                continue
            try:
                create_item(
                    organization=organization,
                    code=row["code"],
                    name=row["name"],
                    category=category,
                    item_type=row["item_type"],
                    base_unit=unit,
                    tracks_lots=row.get("tracks_lots", False),
                    tracks_expiry=row.get("tracks_expiry", False),
                    shelf_life_days=row.get("shelf_life_days"),
                    notes=row.get("notes", ""),
                )
            except ValidationError as error:
                refused.append(f"{row['code']}: {'؛ '.join(error.messages)}")
                continue
            created += 1

        self.write(f"الأصناف: {created} جديدة، {skipped} موجودة.")
        if refused:
            # Named, not counted: a refused row is fixable only if the reader
            # knows which one it was.
            self.write(f"مرفوضة ({len(refused)}):")
            for line in refused:
                self.write(f"   {line}")
            raise ValidationError("توقف التحميل: صفوف مرفوضة.")
