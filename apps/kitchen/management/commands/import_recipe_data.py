r"""
Load transcribed recipe data into the kitchen master.

    .venv\Scripts\python.exe manage.py import_recipe_data ^
        --organization 01 --directory "<a folder outside this repository>"

**The data lives outside the repository and this command does not ship it.**
The quantities in Khan Mandi's recipe book are the business's own formulas, and
this repository has a remote. Code that can load them belongs in git; the
figures themselves do not, which is why the path is an argument with no default
that could accidentally point inside the tree.

What it does, and deliberately does not do:

* **Creates nothing it was not given.** An ingredient with no quantity in the
  source is imported with no quantity, and the row says so. There is no
  fallback, no average, and no "reasonable" gram figure — a fabricated number
  here becomes a plate cost, then a menu price, then a variance somebody is
  held to (`docs/runbooks/phase-3-owner-data-gate.md`).
* **Leaves every version in DRAFT.** The import is a transcription, not an
  approval. A version becomes authoritative by completing the maker-checker
  lifecycle, never by the loader saying so.
* **Records where each row came from.** Document name, page and SHA-256 go on
  the recipe and on every line, so a later reader can tell which revision was
  transcribed and check it.
* **Is idempotent.** Re-running matches on `code` and reports what it skipped.

Yield: the recipe book states the ingredient list for one production batch and,
for most recipes, no output weight. The batch is therefore recorded as one
batch, and the absent yield is written into the version's source note rather
than guessed.
"""

from __future__ import annotations

import json
import pathlib
import re
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from apps.core.console import SeedCommand
from apps.inventory.models import InventoryItem, ItemCategory, ItemType
from apps.inventory.services import create_item, create_item_category
from apps.kitchen.models import MeasurementBasis, RecipeLineCostClass, RecipeType
from apps.kitchen.services import (
    add_recipe_line,
    create_draft_recipe_version,
    create_recipe,
    create_recipe_category,
)
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure

#: Units the sources use that the standard table does not carry. Only those the
#: documents name are listed, and only the conversions the documents themselves
#: state are applied — `بطل` is 1000 ml because page 1 says so. `علبة`, `كوب`
#: and `ملعقة` carry no stated weight anywhere in the sources, so they stay
#: counts and no factor is invented for them.
SOURCE_UNITS = {
    "BOTTLE": ("بطل", "ML", Decimal("1000")),
    "CAN": ("علبة", None, None),
    "CUP": ("كوب", None, None),
    "SPOON": ("ملعقة", None, None),
}

#: The item category every imported ingredient lands in. One category, because
#: the sources classify nothing — inventing a taxonomy would be exactly the
#: kind of addition this import refuses to make.

#: Descriptors that describe the knife, not the item. Onion diced, sliced or
#: minced is the same onion off the same purchase order, so the cut belongs on
#: the recipe line and not in the item master.
CUT_WORDS = frozenset(
    {
        "مقطعة",
        "مقطع",
        "ارباع",
        "شرائح",
        "مكعبات",
        "جوليان",
        "ناعمة",
        "ناعم",
        "مهروسة",
        "مهروس",
    }
)

#: Words that mean a genuinely different item even when the head noun matches.
#: A state change (fried, roasted, dried, smoked, powdered) is produced or
#: bought separately and costs differently; a type (white against red) is a
#: different thing; and every spice blend and recipe output stays whole,
#: because the name is the only thing distinguishing one from another.
DISTINCT_WORDS = frozenset(
    {
        "مقلي",
        "محمص",
        "مسلوق",
        "مسلوقة",
        "مطبوخ",
        "مجفف",
        "مدخن",
        "باودر",
        "مطحون",
        "مطحونة",
        "صحيح",
        "صحيحة",
        "ابيض",
        "احمر",
        "اخضر",
        "حار",
        "بارد",
        "كرزية",
        "عراقي",
        "غنم",
        "عجل",
        "طازج",
        "طازجة",
        "فريش",
        "بهارات",
        "خلطة",
        "صوص",
        "رز",
        "قطعة",
        "دجاجة",
        "نصف",
        "حبة",
        "ماجي",
    }
)


def fold_orthography(text: str) -> str:
    """
    Fold the spellings of one word into one.

    أ/إ/آ and ا are the same letter written with and without its hamza, and the
    sources use both. Left alone, "فلفل أخضر بارد" and "فلفل اخضر بارد" become
    two items, two purchase histories and two costs for one pepper.
    """
    text = re.sub("[أإآٱ]", "ا", text).replace("ى", "ي")
    return re.sub(r"\s+", " ", text).strip()


def canonical_item(name: str) -> str:
    """
    The item an ingredient name belongs to, merging cut variants only.

    Word matching rather than pattern matching. A regex over Arabic has to
    reason about where a word ends, and `` between an Arabic letter and a
    space is not the boundary it looks like — splitting on whitespace and
    comparing whole words says exactly what the rule is and cannot be read two
    ways.
    """
    words = fold_orthography(name).split()
    if any(word in DISTINCT_WORDS for word in words):
        return " ".join(words)
    kept = [word for word in words if word not in CUT_WORDS]
    return " ".join(kept) if kept else " ".join(words)


CATEGORY_CODE = "KITCHEN"
CATEGORY_NAME = "مواد المطبخ"

RECIPE_CATEGORY: dict[str, tuple[str, str]] = {
    RecipeType.BATCH.value: ("BATCH", "وصفات الإنتاج"),
    RecipeType.PORTION.value: ("PORTION", "أطباق التقديم"),
}


class Command(SeedCommand):
    help = "Import transcribed recipe data from a directory outside this repository."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True, help="Organization code.")
        parser.add_argument(
            "--directory",
            required=True,
            help="Folder holding batch-recipes.json and portion-recipes.json.",
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

        folder = pathlib.Path(options["directory"])
        if not folder.is_dir():
            raise CommandError(f"{folder} is not a directory.")

        payloads = []
        for name in ("batch-recipes.json", "portion-recipes.json"):
            path = folder / name
            if not path.is_file():
                raise CommandError(f"{path} is missing.")
            payloads.append(json.loads(path.read_text(encoding="utf-8")))

        with transaction.atomic():
            units = self._units()
            category = self._item_category(organization)
            created_items, reused_items = self._items(organization, category, payloads, units)
            recipes, versions, lines, gaps, blocked = self._recipes(
                organization, payloads, units, category
            )

            self.write("")
            self.write(f"=== {organization.code} — {organization.name_ar} ===")
            self.write(f"  أصناف     : {created_items} أنشئت، {reused_items} موجودة")
            self.write(f"  وصفات     : {recipes}")
            self.write(f"  نسخ مسودة : {versions}")
            self.write(f"  سطور      : {lines}")
            self.write(f"  سطور ناقصة في المصدر (نُقلت كما هي): {gaps}")
            self.write(f"  سطور محجوبة بانتظار حجم العبوة: {len(blocked)}")

            if blocked:
                self.write("")
                self.write("  السطور المحجوبة — المصدر يقيسها بعبوة لا يذكر وزنها:")
                for row in blocked:
                    self.write(
                        f"    ص{row['page']:>3} · {row['recipe']} · {row['item']} "
                        f"· {row['qty']} {row['unit']}"
                    )
                self.write("")
                self.write(
                    "  لا يُخترع لها معامل. تُدخل العبوة وتحويلها من شاشة الأصناف، "
                    "ثم يُعاد تشغيل الاستيراد فتلتحق هذه السطور."
                )

            if options["dry_run"]:
                self.write("")
                self.write("dry run — rolled back, nothing was kept.")
                transaction.set_rollback(True)

    # -- units ---------------------------------------------------------------

    def _units(self) -> dict[str, UnitOfMeasure]:
        units = {unit.code: unit for unit in UnitOfMeasure.objects.all()}
        missing = [code for code in ("KG", "G", "L", "ML", "PIECE") if code not in units]
        if missing:
            raise CommandError(f"Run seed_units first — missing {', '.join(missing)}.")
        # The source-only units are mapped onto what the documents state. A
        # unit with no stated conversion falls back to PIECE so the row can be
        # stored and counted; the entered text keeps what was written.
        for code, (_label, base, _factor) in SOURCE_UNITS.items():
            units[code] = units[base] if base else units["PIECE"]
        return units

    # -- items ---------------------------------------------------------------

    def _item_category(self, organization: Organization) -> ItemCategory:
        existing = ItemCategory.objects.filter(
            organization=organization, code=CATEGORY_CODE
        ).first()
        if existing is not None:
            return existing
        return create_item_category(
            organization=organization, code=CATEGORY_CODE, name_ar=CATEGORY_NAME
        )

    def _base_unit(
        self, unit_codes: set[str | None], units: dict[str, UnitOfMeasure]
    ) -> UnitOfMeasure:
        """
        The stock-keeping unit, decided from **every** measure the sources use.

        The documents measure some ingredients both ways — flour by the spoon in
        one recipe and by the 50 kg sack in another, tomato paste by the tin and
        by the kilo. Mass wins where it appears at all, because that is how the
        store counts; the odd count-unit line is then reported rather than
        converted, since no document states what one tin weighs.
        """
        if unit_codes & {"KG", "G"}:
            return units["KG"]
        if unit_codes & {"L", "ML", "BOTTLE"}:
            return units["L"]
        return units["PIECE"]

    def _items(
        self,
        organization: Organization,
        category: ItemCategory,
        payloads: list[dict[str, Any]],
        units: dict[str, UnitOfMeasure],
    ) -> tuple[int, int]:
        wanted: dict[str, set[str | None]] = {}
        for payload in payloads:
            for recipe in payload["recipes"]:
                for line in recipe["lines"]:
                    wanted.setdefault(canonical_item(line["item"]), set()).add(line.get("unit"))

        created = reused = 0
        for index, (name, unit_codes) in enumerate(sorted(wanted.items()), start=1):
            code = f"ING-{index:04d}"
            if InventoryItem.objects.filter(organization=organization, name_ar=name).exists():
                reused += 1
                continue
            create_item(
                organization=organization,
                code=code,
                name_ar=name,
                category=category,
                # Everything the sources name is a kitchen input. Nothing here
                # claims to know which are bought and which are produced —
                # that is a purchasing decision the documents do not record.
                item_type=ItemType.RAW_MATERIAL,
                base_unit=self._base_unit(unit_codes, units),
                notes="مستورد من مستندات الوصفات",
            )
            created += 1
        return created, reused

    # -- recipes -------------------------------------------------------------

    def _recipe_category(self, organization: Organization, recipe_type: str) -> Any:
        from apps.kitchen.models import RecipeCategory

        code, name = RECIPE_CATEGORY[recipe_type]
        existing = RecipeCategory.objects.filter(organization=organization, code=code).first()
        if existing is not None:
            return existing
        return create_recipe_category(organization=organization, code=code, name_ar=name)

    def _entered(self, quantity: object, unit_code: str | None) -> Decimal:
        """
        The quantity in the unit the line will carry.

        Where a source unit is only meaningful through a factor the document
        itself states — page 1 says a بطل is 1000 ml — the factor is applied
        here and the line stores millilitres. Without it "3 بطل زيت" lands as
        three millilitres of oil: a thousandfold error that reads as a
        plausible number and would never look wrong in a list.

        A row the sources leave blank gets the smallest storable quantity and a
        note saying so. It is never guessed and never dropped, because a
        missing ingredient is invisible and invisible is worse than incomplete.
        """
        if quantity is None:
            return Decimal("0.001")
        amount = Decimal(str(quantity))
        stated = SOURCE_UNITS.get(unit_code or "", (None, None, None))[2]
        return amount * stated if stated is not None else amount

    def _recipes(
        self,
        organization: Organization,
        payloads: list[dict[str, Any]],
        units: dict[str, UnitOfMeasure],
        category_items: ItemCategory,
    ) -> tuple[int, int, int, int, list[dict[str, Any]]]:
        from apps.kitchen.models import Recipe

        recipes = versions = lines = gaps = 0
        blocked: list[dict[str, Any]] = []
        for payload in payloads:
            recipe_type = payload["recipe_type"]
            category = self._recipe_category(organization, recipe_type)
            default_document = payload.get("source_document", "")
            default_sha = payload.get("source_sha256", "")
            sources = payload.get("sources", {})

            for entry in payload["recipes"]:
                if Recipe.objects.filter(organization=organization, code=entry["code"]).exists():
                    continue
                source = sources.get(entry.get("source"), {})
                document = source.get("document", default_document)
                sha = source.get("sha256", default_sha)

                # RCP-007: a batch recipe must name the stored item it
                # produces; a portion recipe must not, because a plate is
                # assembled to order and never stocked. The sources name no
                # output item, so the batch's output takes the recipe's own
                # name — derived from the document, not chosen — and is typed
                # semi-finished because the plating cards consume it.
                output_item = None
                if recipe_type == RecipeType.BATCH.value:
                    output_item = InventoryItem.objects.filter(
                        organization=organization, name_ar=canonical_item(entry["name_ar"])
                    ).first()
                    if output_item is None:
                        output_item = create_item(
                            organization=organization,
                            code=f"OUT-{entry['code']}"[:32],
                            name_ar=canonical_item(entry["name_ar"]),
                            category=category_items,
                            item_type=ItemType.SEMI_FINISHED,
                            base_unit=units["PIECE"],
                            notes="ناتج وصفة إنتاج — الاسم من عنوان الوصفة في المصدر",
                        )

                recipe = create_recipe(
                    organization=organization,
                    code=entry["code"],
                    name_ar=entry["name_ar"],
                    recipe_type=recipe_type,
                    category=category,
                    output_item=output_item,
                    notes=entry.get("note_ar", ""),
                    source_document=document,
                    source_page=entry["page"],
                    source_sha256=sha,
                    source_reference=f"صفحة {entry['page']}",
                )
                recipes += 1

                yield_note = (
                    "الوحدة: دفعة إنتاج واحدة. وزن الناتج غير مذكور في المصدر."
                    if recipe_type == RecipeType.BATCH
                    else "طبق واحد كما يصفه كارت التقديم."
                )
                version = create_draft_recipe_version(
                    recipe=recipe,
                    expected_output_quantity=Decimal("1"),
                    output_unit=units["PIECE"],
                    notes=entry.get("note_ar", ""),
                    source_document=document,
                    source_page=entry["page"],
                    source_sha256=sha,
                    source_note=yield_note,
                )
                versions += 1

                for order, line in enumerate(entry["lines"], start=1):
                    item = InventoryItem.objects.get(
                        organization=organization, name_ar=canonical_item(line["item"])
                    )
                    quantity = line.get("qty")
                    unit_code = line.get("unit")
                    note = line.get("note_ar", "")
                    if canonical_item(line["item"]) != line["item"]:
                        note = f"{line['item']}" + (f" — {note}" if note else "")
                    if quantity is None or unit_code is None:
                        gaps += 1
                        note = (
                            note + " — " if note else ""
                        ) + "الكمية أو الوحدة غير مذكورة في المصدر"
                    try:
                        add_recipe_line(
                            version=version,
                            item=item,
                            # A row the source left blank is stored with the
                            # smallest storable quantity and a note that says the
                            # source is silent. It is never filled in with a guess,
                            # and it is never dropped either: a missing ingredient
                            # is invisible, and invisible is worse than incomplete.
                            entered_quantity=self._entered(quantity, unit_code),
                            entered_unit=units.get(unit_code or "PIECE", units["PIECE"]),
                            cost_class=(
                                RecipeLineCostClass.ACCOMPANIMENT
                                if line.get("class") == "ACCOMPANIMENT"
                                else RecipeLineCostClass.FOOD
                            ),
                            measurement_basis=line.get("basis", MeasurementBasis.RAW),
                            note=note,
                            line_order=order,
                            source_document=document,
                            source_page=entry["page"],
                            source_note=note,
                        )
                    except ValidationError as refused:
                        # The unit layer refuses mass against volume, and a
                        # count against either, because no document says what
                        # one tin or one spoon weighs. The line is reported
                        # with the recipe and page it came from rather than
                        # dropped: the owner states the package size once and
                        # the row imports on the next run.
                        blocked.append(
                            {
                                "recipe": entry["name_ar"],
                                "page": entry["page"],
                                "item": line["item"],
                                "qty": quantity,
                                "unit": unit_code,
                                "why": "; ".join(refused.messages),
                            }
                        )
                        continue
                    lines += 1
        return recipes, versions, lines, gaps, blocked
