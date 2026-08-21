r"""
Load the branch's real inventory item register.

    .venv\Scripts\python.exe manage.py import_stock_items ^
        --organization 01 ^
        --file "<a path outside this repository>\stock-items.json" --dry-run

**Names and units only. Prices are not read, by instruction and by design.**
The source sheet carries a purchase-cost column and a selling-price column;
neither is touched. Every unit cost in this system has to trace to an invoice
line or a dated declaration, and a price picked up from a master-data export
would compete with that chain while looking just as authoritative.

Why this exists at all: the recipe book names *cooking states* — `بصل مقلي`,
`رز مندي`, `قطعة لحم مندي`. Those are not things anybody buys. The register
names *purchasable goods* — `تمن جوكر`, `خلطة مندي`, `طماطة`. Only the second
kind can carry a moving average, because only the second kind appears on an
invoice. Importing recipe wording as items produced a master that no purchase
will ever match, and this command loads the master that will.

Containers are counted, not converted. `دبة`, `كيس`, `علبة`, `باكيت`, `ربطة`
and `رول` become `PIECE`: you buy three drums and receive three drums. What a
drum *weighs* is a conversion the sheet does not state, so it is left to the
item's package conversions where a human enters it against a document.

The item type is **inferred and must be reviewed**. The sheet's own `الفئة`
column is empty on all 190 rows, so there is nothing to read; the keyword rules
below are this command's guess, printed in full so the guess is visible rather
than buried. Anything unmatched stays `RAW_MATERIAL`.
"""

from __future__ import annotations

import json
import pathlib
import re
import unicodedata
from collections import Counter
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from apps.core.console import SeedCommand
from apps.inventory.models import InventoryItem, ItemCategory, ItemType
from apps.inventory.services import create_item, create_item_category, update_item
from apps.organizations.models import Organization
from apps.units.models import UnitOfMeasure

#: The sheet's unit words, mapped onto units this system defines. A container
#: maps to PIECE because it is counted; its contents are a separate fact.
UNITS = {
    "كيلوجرام": "KG",
    "كغم": "KG",
    "كغ": "KG",
    "لتر": "L",
    "طن": "TON",
    "قطعة": "PIECE",
    "دبة": "PIECE",
    "كيس": "PIECE",
    "علبة": "PIECE",
    "باكيت": "PIECE",
    "ربطة": "PIECE",
    "رول": "PIECE",
}

#: Containers, kept apart so the report can name what still needs a conversion.
CONTAINERS = frozenset({"دبة", "كيس", "علبة", "باكيت", "ربطة", "رول"})

#: Keyword → item type. First match wins, so order matters.
CLASSIFIERS: list[tuple[str, tuple[str, ...]]] = [
    (
        ItemType.PACKAGING,
        (
            "اكياس",
            "كيس ورق",
            "علبة",
            "غطاء",
            "قاعدة",
            "صينية",
            "قدح",
            "كاسة",
            "كاسات",
            "سلفون",
            "سيلفون",
            "رول حراري",
            "رول سليفون",
            "رول طابعه",
            "رول صلصة",
            "ورق طاولة",
            "ورق زبدة",
            "ورق رايز",
            "ماعون",
            "سفرة",
            "وزرة نايلون",
            "شريط لاصق",
            "بكرة",
            "كارتون",
            "صحن سفري",
            "منيو سفري",
            "اكياس خبز",
            "وصلة",
        ),
    ),
    (
        ItemType.GOODS_FOR_RESALE,
        ("ببسي", "سفن", "ميرندا", "ماء لوغو", "قصب اسود", "زاهي"),
    ),
    (
        ItemType.CONSUMABLE,
        (
            "كلينكس",
            "سلك",
            "اسفنج",
            "كفوف",
            "منظفة",
            "معطر",
            "فرجة",
            "ماسحة",
            "دفتر",
            "اشعار",
            "بدلة",
            "دشداشة",
            "شماخ",
            "قميص",
            "زي ",
            "كتلري",
            "ملعقة",
            "استكان",
            "كرسي",
            "دبة غاز",
            "كاز",
            "خشب",
            "فلاش",
            "مانكس",
            "عود أسنان",
            "سبانغ",
            "جلافة",
            "وصل مسح",
            "زبدية",
            "ورد ماوي",
            "صبغ",
            "حراكة شاي",
            "ايزي",
            "كب راس",
            "وزرة",
            "اسفنج",
            "منظفة",
        ),
    ),
]

TATWEEL = re.compile(r"[ـً-ْ]")


def fold(name: str) -> str:
    """Normalise the orthography the two sources disagree on, nothing more."""
    text = unicodedata.normalize("NFKC", name)
    text = TATWEEL.sub("", text)
    for source, target in (
        ("أ", "ا"),
        ("إ", "ا"),
        ("آ", "ا"),
        ("ة", "ه"),
        ("ى", "ي"),
        ("ؤ", "و"),
        ("ئ", "ي"),
    ):
        text = text.replace(source, target)
    words = [w for w in re.split(r"[\s(),*/\-]+", text) if w]
    words = [w[2:] if w.startswith("ال") and len(w) > 3 else w for w in words]
    return " ".join(words)


def classify(name: str) -> str:
    """
    First matching rule wins.

    A single-word keyword is matched **whole**, never as a substring: `سفن`
    the soft drink is a substring of `اسفنج` the sponge, and substring matching
    filed the sponge under goods for resale. A multi-word keyword is already
    specific enough to match as a phrase.
    """
    folded = fold(name)
    words = set(folded.split())
    for item_type, keywords in CLASSIFIERS:
        for keyword in keywords:
            target = fold(keyword)
            hit = target in folded if " " in target else target in words
            if hit:
                return item_type
    return ItemType.RAW_MATERIAL


#: Placeholder rows in the export. They name nothing and are not items.
PLACEHOLDERS = frozenset({"عام", "عام2"})


#: Category code per item type, so the register lands in a readable tree.
CATEGORIES = {
    ItemType.RAW_MATERIAL: ("KITCHEN", "مواد المطبخ"),
    ItemType.PACKAGING: ("PACKAGING", "مواد التغليف"),
    ItemType.CONSUMABLE: ("CONSUMABLE", "المستهلكات"),
    ItemType.GOODS_FOR_RESALE: ("RESALE", "بضاعة لإعادة البيع"),
}


class Command(SeedCommand):
    help = "Import the inventory item register from a file outside this repository."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True)
        parser.add_argument("--file", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        organization = Organization.objects.filter(code=options["organization"]).first()
        if organization is None:
            raise CommandError(f"No organization {options['organization']}.")

        path = pathlib.Path(options["file"])
        if not path.is_file():
            raise CommandError(f"{path} is missing.")
        rows = json.loads(path.read_text(encoding="utf-8"))["items"]

        units = {u.code: u for u in UnitOfMeasure.objects.filter(is_active=True)}
        existing = {
            fold(i.name_ar): i for i in InventoryItem.objects.filter(organization=organization)
        }

        made = reused = 0
        skipped: list[tuple[str, str]] = []
        by_type: Counter[str] = Counter()
        needs_conversion: list[str] = []
        matched: list[str] = []
        reclassified: list[str] = []

        with transaction.atomic():
            categories: dict[str, ItemCategory] = {}
            for category_type, (code, name_ar) in CATEGORIES.items():
                category = ItemCategory.objects.filter(organization=organization, code=code).first()
                if category is None:
                    category = create_item_category(
                        organization=organization, code=code, name_ar=name_ar
                    )
                categories[category_type] = category

            for number, row in enumerate(rows, start=1):
                name = row["name"].strip()
                unit_word = row["unit_ar"].strip()
                unit_code = UNITS.get(unit_word)
                if unit_code is None:
                    skipped.append((name, f"وحدة غير معروفة «{unit_word}»"))
                    continue
                if unit_word in CONTAINERS:
                    needs_conversion.append(f"{name} — {unit_word}")

                key = fold(name)
                if key in PLACEHOLDERS:
                    skipped.append((name, "سطر حاجز في الملف، لا يسمّي صنفاً"))
                    continue
                if key in existing:
                    found = existing[key]
                    wanted = classify(name)
                    # Captured before the update: `update_item` mutates the
                    # instance in place, so reading it afterwards reports the
                    # new value on both sides of the arrow.
                    was = str(found.item_type)
                    if found.code.startswith("STK-") and was != wanted:
                        # This importer wrote the row and its own rule has since
                        # changed. Re-running is how the correction lands, so it
                        # amends rather than reporting a match and moving on.
                        update_item(
                            item=found,
                            name_ar=found.name_ar,
                            category=categories[wanted],
                            item_type=wanted,
                            notes=found.notes,
                        )
                        reclassified.append(f"{name}: {was} → {wanted}")
                    else:
                        matched.append(f"{name}  ≡  {found.name_ar}")
                    reused += 1
                    continue

                item_type = classify(name)
                by_type[item_type] += 1
                try:
                    item = create_item(
                        organization=organization,
                        code=f"STK-{number:04d}",
                        name_ar=name,
                        category=categories[item_type],
                        item_type=item_type,
                        base_unit=units[unit_code],
                        notes="سجل الأصناف المخزنية — Stock.xlsx",
                    )
                    existing[key] = item
                    made += 1
                except ValidationError as refused:
                    skipped.append((name, "; ".join(refused.messages)))

            self.write("")
            self.write(f"=== سجل الأصناف · {organization.code} ===")
            self.write(f"  في الملف   : {len(rows)}")
            self.write(f"  أُنشئت     : {made}")
            self.write(f"  موجودة سلفاً: {reused}")
            self.write(f"  لم تُنشأ    : {len(skipped)}")
            self.write("")
            self.write("  التصنيف المستنتج — يحتاج مراجعتك:")
            for kind, count in by_type.most_common():
                self.write(f"    {kind:18s} {count}")

            if reclassified:
                self.write("")
                self.write(f"  أُعيد تصنيفها ({len(reclassified)}):")
                for line in reclassified:
                    self.write(f"    · {line}")

            if matched:
                self.write("")
                self.write(f"  طابقت أصنافاً موجودة ({len(matched)}):")
                for line in matched:
                    self.write(f"    · {line}")

            if needs_conversion:
                self.write("")
                self.write(f"  عبوات تُعدّ ولا يذكر الملف وزنها ({len(needs_conversion)}):")
                for line in needs_conversion:
                    self.write(f"    · {line}")
                self.write("    تُدخل تحويلاتها من شاشة الأصناف مقابل مستند، ولا تُخمَّن هنا.")

            if skipped:
                self.write("")
                self.write("  لم تُنشأ:")
                for name, why in skipped:
                    self.write(f"    · {name} — {why}")

            if options["dry_run"]:
                self.write("")
                self.write("dry run — rolled back, nothing was kept.")
                transaction.set_rollback(True)
