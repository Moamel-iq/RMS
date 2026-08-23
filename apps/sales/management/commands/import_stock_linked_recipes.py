r"""
Load plate recipes that are wired to the real stock register, and sell them.

    .venv\Scripts\python.exe manage.py import_stock_linked_recipes ^
        --organization 01 --branch 011 ^
        --file "<a path outside this repository>\stock-linked-recipes.json" ^
        --effective-from 2026-08-20

Same rule as `import_recipe_data` and `import_menu_prices`: the code ships, the
figures do not. The owner's per-plate quantities are the business's formulas and
this repository has a remote, so the path is an argument with no default that
could accidentally point inside the tree.

Where this differs from `import_recipe_data` — and the difference is the whole
point of a second loader:

* **It creates no inventory item, ever.** `import_recipe_data` transcribes a
  recipe book and mints an item for every ingredient name it meets, which was
  right while the item master was empty and is wrong now that the real register
  is loaded. Here every line names an existing `STK-****` code, and a code that
  does not resolve stops the import with the list of what is missing. The owner
  asked to be told what the kitchen buys but stores does not carry; a loader
  that quietly created the row would answer that question with a fiction.
* **It carries the arithmetic, not just the result.** Each line states the batch
  figure it came from and the divisor applied — `750غ ÷ 750غ للعبوة ÷ 20` — so a
  reviewer can check a plate cost against the book without re-deriving it.
* **It puts the dish on the menu but not on sale.** The menu item, its price and
  its branch row are created together, with `is_available=False`. A DRAFT recipe
  that could be ordered would post a consumption no version authorises.

Everything it writes is reversible by review: versions stay `DRAFT`, branch
availability stays off, and re-running matches on `code` and reports the skips.
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

from apps.core.console import SeedCommand
from apps.inventory.models import InventoryItem, Warehouse
from apps.kitchen.models import Recipe, RecipeCategory, RecipeLine, RecipeType
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_serving,
    create_draft_recipe_version,
    create_recipe,
    remove_recipe_line,
    update_recipe,
)
from apps.organizations.models import Branch, Organization
from apps.sales.models import FulfillmentSource, MenuCategory, MenuItem
from apps.sales.services import (
    create_menu_category,
    create_menu_item,
    create_menu_price,
    set_branch_availability,
)
from apps.units.models import UnitOfMeasure

#: The recipe category plate recipes land in. Created by `import_recipe_data`
#: and reused here rather than redefined, so both loaders file a plate in the
#: same folder.
PORTION_CATEGORY = ("PORTION", "أطباق التقديم")


class Command(SeedCommand):
    help = "Import plate recipes bound to existing stock items, with their menu rows."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True, help="Organization code.")
        parser.add_argument("--branch", required=True, help="Branch code for price and offer.")
        parser.add_argument("--file", required=True, help="Path to stock-linked-recipes.json.")
        parser.add_argument(
            "--effective-from",
            required=True,
            help="Date the prices take effect (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created and roll back.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        organization = Organization.objects.filter(code=options["organization"]).first()
        if organization is None:
            raise CommandError(f"No organization with code {options['organization']}.")
        branch = Branch.objects.filter(organization=organization, code=options["branch"]).first()
        if branch is None:
            raise CommandError(f"No branch with code {options['branch']}.")

        path = pathlib.Path(options["file"])
        if not path.is_file():
            raise CommandError(f"{path} is missing.")
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

        try:
            effective_from = datetime.date.fromisoformat(options["effective_from"])
        except ValueError as error:
            raise CommandError(f"--effective-from: {error}") from error

        with transaction.atomic():
            units = self._units()
            items = self._items(organization, payload)
            recipe_category = self._recipe_category(organization)
            menu_categories = self._menu_categories(organization, payload)

            corrections = self._correct(organization, payload, units, items)
            created, skipped, lines, priced = self._load(
                organization=organization,
                branch=branch,
                payload=payload,
                units=units,
                items=items,
                recipe_category=recipe_category,
                menu_categories=menu_categories,
                effective_from=effective_from,
            )
            resale = self._resale(
                organization=organization,
                branch=branch,
                payload=payload,
                menu_categories=menu_categories,
                effective_from=effective_from,
            )

            self.write("")
            self.write(f"=== {organization.code} — {organization.name_ar} / {branch.code} ===")
            self.write(f"  وصفات جديدة   : {len(created)}")
            self.write(f"  وصفات موجودة  : {len(skipped)}")
            self.write(f"  سطور مخزنية   : {lines}")
            self.write(f"  أسعار منيو    : {priced}")
            self.write(f"  أصناف بيع مباشر: {len(resale)}")
            for code in created:
                self.write(f"    + {code}")
            for code in resale:
                self.write(f"    + {code} (بيع مباشر من المخزن)")
            for code in skipped:
                self.write(f"    = {code} (موجودة، لم تُلمس)")
            if corrections:
                self.write("  تصحيحات على وصفات قائمة:")
                for line in corrections:
                    self.write(f"    ~ {line}")

            if options["dry_run"]:
                self.write("")
                self.write("dry run — rolled back, nothing was kept.")
                transaction.set_rollback(True)

    # -- lookups -------------------------------------------------------------

    def _units(self) -> dict[str, UnitOfMeasure]:
        units = {unit.code: unit for unit in UnitOfMeasure.objects.all()}
        missing = [code for code in ("KG", "G", "L", "ML", "PIECE") if code not in units]
        if missing:
            raise CommandError(f"Run seed_units first — missing {', '.join(missing)}.")
        return units

    def _items(
        self, organization: Organization, payload: dict[str, Any]
    ) -> dict[str, InventoryItem]:
        """
        Resolve every stock code the file names, or refuse the whole file.

        Refusing all of it rather than skipping the bad lines is deliberate. A
        plate missing one ingredient still costs something, and a cost that is
        wrong by exactly one spice is far more dangerous than no cost at all —
        it looks finished.
        """
        wanted = {
            line["stock"]
            for recipe in payload["recipes"] + payload.get("batch_recipes", [])
            for line in recipe["lines"]
        }
        wanted |= {
            row["output_item"] for row in payload.get("batch_recipes", []) if row.get("output_item")
        }
        for fix in payload.get("corrections", []):
            wanted |= {row["stock"] for row in fix.get("replace_lines", [])}
            wanted |= {row["stock"] for row in fix.get("add_lines", [])}
        found = {
            item.code: item
            for item in InventoryItem.objects.filter(
                organization=organization, code__in=sorted(wanted)
            )
        }
        missing = sorted(wanted - set(found))
        if missing:
            raise CommandError(
                "These stock codes are not in the item master, and this loader "
                "creates none: " + ", ".join(missing)
            )
        archived = sorted(code for code, item in found.items() if not item.is_active)
        if archived:
            raise CommandError("These stock items are archived: " + ", ".join(archived))
        return found

    def _recipe_category(self, organization: Organization) -> RecipeCategory:
        code, name = PORTION_CATEGORY
        category = RecipeCategory.objects.filter(organization=organization, code=code).first()
        if category is None:
            raise CommandError(
                f"Recipe category {code} ({name}) is missing — run import_recipe_data first."
            )
        return category

    def _batch_category(self, organization: Organization) -> RecipeCategory:
        category = RecipeCategory.objects.filter(organization=organization, code="BATCH").first()
        if category is None:
            raise CommandError(
                "Recipe category BATCH (وصفات الإنتاج) is missing — run import_recipe_data first."
            )
        return category

    def _menu_categories(
        self, organization: Organization, payload: dict[str, Any]
    ) -> dict[str, MenuCategory]:
        categories: dict[str, MenuCategory] = {
            category.code: category
            for category in MenuCategory.objects.filter(organization=organization)
        }
        for row in payload.get("menu_categories", []):
            if row["code"] in categories:
                continue
            categories[row["code"]] = create_menu_category(
                organization=organization,
                code=row["code"],
                name_ar=row["name_ar"],
                display_order=int(row.get("display_order", 1)),
            )
        return categories

    # -- corrections to recipes already loaded -------------------------------

    def _correct(
        self,
        organization: Organization,
        payload: dict[str, Any],
        units: dict[str, UnitOfMeasure],
        items: dict[str, InventoryItem],
    ) -> list[str]:
        """
        Re-state lines on a draft the owner has since corrected.

        A replacement removes the old line and adds the new one at the same
        position, because `update_recipe_line` refuses to change the item —
        swapping white rice for mandi rice is a different ingredient, not a
        corrected quantity, and any step pointing at the old line would
        silently come to mean something else. Removing makes that break
        visible.

        Only `DRAFT` versions are touched, and the services enforce it: a
        version that has been approved is corrected by a new version, never by
        an edit in place.
        """
        report: list[str] = []
        for fix in payload.get("corrections", []):
            recipe = Recipe.objects.filter(organization=organization, code=fix["recipe"]).first()
            if recipe is None:
                raise CommandError(f"{fix['recipe']} is not in the recipe master.")
            version = recipe.versions.order_by("-version_number").first()
            if version is None:
                raise CommandError(f"{fix['recipe']} has no version to correct.")

            for row in fix.get("replace_lines", []):
                line = RecipeLine.objects.filter(
                    version=version, line_order=row["line_order"]
                ).first()
                if line is None:
                    raise CommandError(
                        f"{fix['recipe']} has no line at position {row['line_order']}."
                    )
                if line.item.code == row["stock"]:
                    report.append(f"{fix['recipe']} سطر {row['line_order']} — {row['stock']} أصلاً")
                    continue
                was = line.item.code
                remove_recipe_line(line=line, reason=row.get("note_ar", ""))
                self._line(version, row, units, items, order=row["line_order"], payload=payload)
                report.append(f"{fix['recipe']} سطر {row['line_order']} — {was} ← {row['stock']}")

            for row in fix.get("add_lines", []):
                if RecipeLine.objects.filter(version=version, item__code=row["stock"]).exists():
                    report.append(f"{fix['recipe']} — {row['stock']} موجود أصلاً")
                    continue
                self._line(version, row, units, items, order=row["line_order"], payload=payload)
                report.append(f"{fix['recipe']} + {row['stock']} {row['qty']} {row['unit']}")

            if "notes_recipe_ar" in fix or "description_ar" in fix:
                update_recipe(
                    recipe=recipe,
                    name_ar=recipe.name_ar,
                    name_en=recipe.name_en,
                    description_ar=fix.get("description_ar", recipe.description_ar),
                    category=recipe.category,
                    output_item=recipe.output_item,
                    notes=fix.get("notes_recipe_ar", recipe.notes),
                )
                report.append(f"{fix['recipe']} — حُدِّث الوصف والملاحظة")
        return report

    def _line(
        self,
        version: Any,
        row: dict[str, Any],
        units: dict[str, UnitOfMeasure],
        items: dict[str, InventoryItem],
        *,
        order: int,
        payload: dict[str, Any],
    ) -> None:
        add_recipe_line(
            version=version,
            item=items[row["stock"]],
            entered_quantity=Decimal(row["qty"]),
            entered_unit=units[row["unit"]],
            note=row.get("note_ar", ""),
            line_order=order,
            source_document=payload.get("source_document", ""),
            source_page=row.get("page"),
            source_sha256=payload.get("source_sha256", ""),
            source_reference=row.get("source_reference", ""),
        )

    # -- resale items sold straight from stock -------------------------------

    def _resale(
        self,
        *,
        organization: Organization,
        branch: Branch,
        payload: dict[str, Any],
        menu_categories: dict[str, MenuCategory],
        effective_from: datetime.date,
    ) -> list[str]:
        """
        Put a bought-and-resold good on the menu with no recipe behind it.

        The item must already be classified `GOODS_FOR_RESALE` — Sales checks
        it, and `apply_item_decisions` is where an item earns the
        classification. A direct-stock line also needs its source warehouse on
        the branch row, because the sale issues the stock and something has to
        say from where.
        """
        rows = payload.get("direct_stock", [])
        if not rows:
            return []
        created: list[str] = []
        for row in rows:
            item = InventoryItem.objects.filter(
                organization=organization, code=row["stock"]
            ).first()
            if item is None:
                raise CommandError(f"{row['stock']} is not in the item master.")
            warehouse = Warehouse.objects.filter(branch=branch, code=row["warehouse"]).first()
            if warehouse is None:
                raise CommandError(f"Warehouse {row['warehouse']} is not on branch {branch.code}.")

            menu_item = MenuItem.objects.filter(organization=organization, code=row["code"]).first()
            if menu_item is None:
                menu_item = create_menu_item(
                    organization=organization,
                    code=row["code"],
                    name_ar=row["name_ar"],
                    category=menu_categories[row["category"]],
                    fulfillment_source=FulfillmentSource.DIRECT_STOCK,
                    inventory_item=item,
                    direct_stock_base_quantity=Decimal(row["quantity"]),
                    display_order=int(row.get("display_order", 1)),
                    notes=row.get("note_ar", ""),
                )
                created.append(row["code"])
            set_branch_availability(
                item=menu_item,
                branch=branch,
                is_available=bool(row.get("available", True)),
                notes=row.get("availability_note_ar", ""),
                source_warehouse=warehouse,
            )
            if row.get("price") and not menu_item.prices.filter(branch=branch).exists():
                create_menu_price(
                    menu_item=menu_item,
                    branch=branch,
                    unit_price=Decimal(str(row["price"])),
                    effective_from=effective_from,
                    evidence_reference=payload.get("price_evidence", ""),
                    notes=row.get("price_note_ar", ""),
                )
        return created

    # -- the load ------------------------------------------------------------

    def _load(
        self,
        *,
        organization: Organization,
        branch: Branch,
        payload: dict[str, Any],
        units: dict[str, UnitOfMeasure],
        items: dict[str, InventoryItem],
        recipe_category: RecipeCategory,
        menu_categories: dict[str, MenuCategory],
        effective_from: datetime.date,
    ) -> tuple[list[str], list[str], int, int]:
        document = payload.get("source_document", "")
        sha = payload.get("source_sha256", "")
        evidence = payload.get("price_evidence", "")

        created: list[str] = []
        skipped: list[str] = []
        line_count = 0
        price_count = 0

        for row in payload["recipes"] + payload.get("batch_recipes", []):
            code = row["code"]
            if Recipe.objects.filter(organization=organization, code=code).exists():
                skipped.append(code)
                continue

            page = int(row["page"])
            reference = row.get("source_reference", "")

            # A production recipe names the stocked thing it fills; a plate
            # recipe deliberately does not, because a plated dish is assembled
            # to order and is never an inventory row (RCP-007).
            output_code = row.get("output_item", "")
            output_item = items[output_code] if output_code else None
            batch = bool(output_item)

            recipe = create_recipe(
                organization=organization,
                code=code,
                name_ar=row["name_ar"],
                recipe_type=RecipeType.BATCH if batch else RecipeType.PORTION,
                description_ar=row.get("description_ar", ""),
                category=(self._batch_category(organization) if batch else recipe_category),
                output_item=output_item,
                notes=row.get("notes_recipe_ar", ""),
                source_document=document,
                source_page=page,
                source_sha256=sha,
                source_reference=reference,
            )
            version = create_draft_recipe_version(
                recipe=recipe,
                expected_output_quantity=Decimal(str(row.get("output_quantity", "1"))),
                output_unit=units[row.get("output_unit", "PIECE")],
                batch_size=Decimal("1"),
                instructions=" ".join(
                    part
                    for part in (row.get("description_ar", ""), row.get("notes_recipe_ar", ""))
                    if part
                ),
                notes=row.get("notes_version_ar", ""),
                source_document=document,
                source_page=page,
                source_sha256=sha,
                source_reference=reference,
            )

            for order, line in enumerate(row["lines"], start=1):
                try:
                    add_recipe_line(
                        version=version,
                        item=items[line["stock"]],
                        entered_quantity=Decimal(line["qty"]),
                        entered_unit=units[line["unit"]],
                        note=line.get("note_ar", ""),
                        line_order=order,
                        source_document=document,
                        source_page=page,
                        source_sha256=sha,
                        source_reference=reference,
                    )
                except ValidationError as error:
                    raise CommandError(
                        f"{code} line {order} ({line['stock']}): {error.messages}"
                    ) from error
                line_count += 1

            serving = row["serving"]
            add_recipe_serving(
                version=version,
                code=serving["code"],
                name_ar=serving["name_ar"],
                serving_quantity=Decimal(serving["quantity"]),
                serving_unit=units[serving["unit"]],
                is_primary=True,
                source_document=document,
                source_page=page,
                source_sha256=sha,
                source_reference=reference,
                source_note=serving.get("note_ar", ""),
            )

            # A production recipe has no menu row on purpose: the owner sells
            # the plate that consumes the sauce, never the sauce.
            if "menu" in row:
                price_count += self._publish(
                    organization=organization,
                    branch=branch,
                    recipe=recipe,
                    serving_code=serving["code"],
                    menu=row["menu"],
                    menu_categories=menu_categories,
                    effective_from=effective_from,
                    evidence=evidence,
                )
            created.append(code)

        return created, skipped, line_count, price_count

    def _publish(
        self,
        *,
        organization: Organization,
        branch: Branch,
        recipe: Recipe,
        serving_code: str,
        menu: dict[str, Any],
        menu_categories: dict[str, MenuCategory],
        effective_from: datetime.date,
        evidence: str,
    ) -> int:
        """Put the dish on the menu at its card price, switched off."""
        category = menu_categories.get(menu["category"])
        if category is None:
            raise CommandError(f"Menu category {menu['category']} is not defined in the file.")

        item = MenuItem.objects.filter(organization=organization, code=menu["code"]).first()
        if item is None:
            item = create_menu_item(
                organization=organization,
                code=menu["code"],
                name_ar=menu["name_ar"],
                recipe=recipe,
                serving_code=serving_code,
                category=category,
                display_order=int(menu.get("display_order", 1)),
            )

        set_branch_availability(
            item=item,
            branch=branch,
            is_available=False,
            notes=menu.get("availability_note_ar", ""),
        )

        price = menu.get("price")
        if not price:
            return 0
        create_menu_price(
            menu_item=item,
            branch=branch,
            unit_price=Decimal(str(price)),
            effective_from=effective_from,
            evidence_reference=evidence,
            notes="السعر من ملف المنيو؛ الصنف غير متاح للبيع حتى تفعيل الوصفة.",
        )
        return 1
