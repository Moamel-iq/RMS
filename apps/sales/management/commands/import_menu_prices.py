r"""
Load the menu and its selling prices from a transcribed card.

    .venv\Scripts\python.exe manage.py import_menu_prices ^
        --organization 01 --branch 011 ^
        --file "<a path outside this repository>\menu-prices.json" ^
        --effective-from 2026-08-20

Same rule as `import_recipe_data`: the code ships, the figures do not. A price
list is what a competitor would most like to read, and this repository has a
remote, so the path is an argument with no default that could point inside the
tree.

Two things it will not do:

* **Sell what has no recipe.** A menu line with no plating card is reported and
  skipped, not created against a guessed recipe. Eleven lines on the current
  card are in that state — the combination plate, the whole-lamb bookings, the
  side dishes and the drinks — and each is a real decision about how the dish
  is costed, not a gap to paper over.
* **Invent a price.** A line whose price is set at booking time is skipped with
  its reason. `الطلب الخاص` has no number on the card and gets none here.
"""

from __future__ import annotations

import csv
import datetime
import json
import pathlib
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from apps.core.console import SeedCommand
from apps.kitchen.models import Recipe
from apps.organizations.models import Branch, Organization
from apps.sales.models import MenuCategory, MenuItem
from apps.sales.services import create_menu_category, create_menu_item, create_menu_price


def _load(path: pathlib.Path) -> dict[str, Any]:
    """
    Read the menu from CSV or JSON.

    CSV is the format a restaurant owner can actually maintain: it opens in
    Excel, a new dish is a new row, and a price change is one cell. JSON stays
    supported because the earlier transcription used it, and re-transcribing a
    working file to change its punctuation would risk the data for nothing.
    """
    if path.suffix.lower() == ".json":
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    sections: dict[str, list[dict[str, Any]]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("اسم الطبق") or "").strip()
            if not name:
                continue
            sections.setdefault((row.get("القسم") or "المنيو").strip(), []).append(
                {
                    "name_ar": name,
                    "variant": (row.get("الحجم") or "").strip(),
                    "price": (row.get("السعر") or "").strip(),
                    "recipe": (row.get("رمز الوصفة") or "").strip(),
                    "note_ar": (row.get("ملاحظات") or "").strip(),
                }
            )
    return {"sections": [{"name_ar": k, "items": v} for k, v in sections.items()]}


class Command(SeedCommand):
    help = "Import menu items and selling prices from a file outside this repository."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True)
        parser.add_argument("--branch", required=True)
        parser.add_argument("--file", required=True)
        parser.add_argument("--effective-from", required=True, help="YYYY-MM-DD")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        organization = Organization.objects.filter(code=options["organization"]).first()
        if organization is None:
            raise CommandError(f"No organization {options['organization']}.")
        branch = Branch.objects.filter(organization=organization, code=options["branch"]).first()
        if branch is None:
            raise CommandError(f"No branch {options['branch']}.")

        path = pathlib.Path(options["file"])
        if not path.is_file():
            raise CommandError(f"{path} is missing.")
        payload = _load(path)
        effective = datetime.date.fromisoformat(options["effective_from"])

        made_items = made_prices = reused = 0
        skipped: list[tuple[str, str]] = []

        with transaction.atomic():
            for order, section in enumerate(payload["sections"], start=1):
                category = self._category(organization, section["name_ar"], order)
                for index, row in enumerate(section["items"], start=1):
                    label = row["name_ar"] + (f" ({row['variant']})" if row.get("variant") else "")

                    if not row.get("recipe"):
                        skipped.append((label, row.get("note_ar") or "لا وصفة مرتبطة"))
                        continue
                    if not row.get("price"):
                        skipped.append((label, row.get("note_ar") or "لا سعر في المنيو"))
                        continue

                    recipe = Recipe.objects.filter(
                        organization=organization, code=row["recipe"]
                    ).first()
                    if recipe is None:
                        skipped.append((label, f"الوصفة {row['recipe']} غير موجودة"))
                        continue

                    code = row["recipe"]
                    item = MenuItem.objects.filter(organization=organization, code=code).first()
                    if item is None:
                        item = create_menu_item(
                            organization=organization,
                            code=code,
                            name_ar=label,
                            recipe=recipe,
                            serving_code="PLATE",
                            category=category,
                            display_order=index,
                        )
                        made_items += 1
                    else:
                        reused += 1

                    try:
                        create_menu_price(
                            menu_item=item,
                            branch=branch,
                            unit_price=Decimal(row["price"]),
                            effective_from=effective,
                            evidence_reference="منيو خان مندي",
                        )
                        made_prices += 1
                    except ValidationError as refused:
                        skipped.append((label, "; ".join(refused.messages)))

            self.write("")
            self.write(f"=== {organization.code} · {branch.code} ===")
            self.write(f"  أصناف منيو : {made_items} أنشئت، {reused} موجودة")
            self.write(f"  أسعار      : {made_prices}")
            self.write(f"  متخطّاة    : {len(skipped)}")
            if skipped:
                self.write("")
                self.write("  لم تُنشأ، وسبب كل واحدة:")
                for label, why in skipped:
                    self.write(f"    · {label} — {why}")

            if options["dry_run"]:
                self.write("")
                self.write("dry run — rolled back.")
                transaction.set_rollback(True)

    def _category(self, organization: Organization, name: str, order: int) -> MenuCategory:
        code = f"CAT-{order:02d}"
        existing = MenuCategory.objects.filter(organization=organization, code=code).first()
        if existing is not None:
            return existing
        return create_menu_category(organization=organization, code=code, name_ar=name)
