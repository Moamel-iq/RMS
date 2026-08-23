from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.inventory.models import InventoryItem, ItemCategory, ItemType
from apps.inventory.services import create_item
from apps.organizations.models import Branch
from apps.procurement.invoices import add_inventory_line, create_supplier_invoice
from apps.procurement.models import Supplier, SupplierInvoice
from apps.procurement.services import create_supplier
from apps.units.models import UnitOfMeasure
from apps.users.models import User

IMPORT_TAG = "legacy-purchase-pdf-v1"

SUPPLIER_NAMES = {
    "S/000002": "خان مندي المنصور",
    "S/000004": "ابو محمد / لبن كركوك",
    "S/000005": "دجاج الريان / شركة سما كربلاء",
    "S/000006": "لحوم ابو فهد الشمري",
    "S/000007": "شركة بغداد للمشروبات الغازية",
    "S/000008": "تجهيزات القيسي / خضروات",
    "S/000011": "موظف المشتريات علي عمر",
    "S/000016": "الرياحين بلاست",
    "S/000017": "تحسين الخزرجي / للخضروات",
    "S/000018": "ابو غدير / حطب",
    "S/000019": "مصطفى / كاز",
}

ITEM_NAME_CORRECTIONS = {
    "اكياس الخان صغري سفري": "أكياس الخان صغرى سفري",
    "اكياس الخان كبري سفري": "أكياس الخان كبرى سفري",
    "اكياس حاوية": "أكياس حاوية",
    "ببيس": "بيبسي",
    "ببيس دايت": "بيبسي دايت",
    "خشب ( حطب )": "خشب (حطب)",
    "دبس الرمان (900 ملم)": "دبس الرمان (900 مل)",
    "دفرت إيصال دفع خان مندي": "دفتر إيصال دفع خان مندي",
    "دفرت تقرير تلف": "دفتر تقرير تلف",
    "دفرت طلب مواد داخلي": "دفتر طلب مواد داخلي",
    "زيت الدار 1 لرت": "زيت الدار 1 لتر",
    "زيت الدار 900 ملم": "زيت الدار 900 مل",
    "صحن سفري عني وحدة كبري سفري": "صحن سفري عين وحدة كبير سفري",
    "طحني الزورد (50 كغ)": "طحين لازورد (50 كغ)",
    "طريش مشكل": "طرشي مشكل",
    "علبة مربع 100 يس يس 1*1000": "علبة مربع 100 سي سي 1*1000",
    "لنب سطل": "لبن سطل",
    "ماعون البتوب سفري": "ماعون بتوب سفري",
}

# Link only when the purchase unit is the existing item's base unit. Package
# descriptions remain separate items because the PDFs state no conversion.
EXISTING_ITEM_CODES = {
    "باذنجان": "ING-0004",
    "بصل": "ING-0006",
    "بطاطا": "ING-0011",
    "تمن جوكر": "ING-0035",
    "جزر": "ING-0041",
    "دبس الرمان (900 مل)": "ING-0060",
    "طحين لازورد (50 كغ)": "ING-0103",
    "طماطة": "ING-0105",
    "فلفل بارد": "ING-0119",
    "فلفل حار": "ING-0120",
    "لحم لشه (طلي) بدون زوائد": "ING-0144",
    "ليمون": "ING-0150",
}

UNIT_CODES = {
    "كيلوجرام": "KG",
    "لرت": "L",
    "لتر": "L",
    "طن": "TON",
    "قطعة": "PIECE",
    "باكيت": "PIECE",
    "دبة": "PIECE",
    "ربطة": "PIECE",
    "علبة": "PIECE",
}

PACKAGING_WORDS = (
    "أكياس",
    "سلفون",
    "سيلفون",
    "كاسة",
    "كاسات",
    "علبة",
    "صحن",
    "غطاء",
    "قاعدة",
    "قدح",
    "ملعقة",
    "كتلري",
    "ماعون",
    "صينية",
    "ورق طاولة",
)
CONSUMABLE_WORDS = (
    "دفتر",
    "رول حراري",
    "رول طابعه",
    "كفوف",
    "ماسحة",
    "وصل",
    "وصلة",
    "كلينكس",
    "كاز",
    "خشب",
)
RESALE_NAMES = {
    "بيبسي",
    "بيبسي دايت",
    "سفن",
    "ماء لوغو خان مندي (500مل)",
}


def canonical_supplier_code(source_code: str) -> str:
    return source_code.replace("/", "-")


def canonical_item_name(source_name: str) -> str:
    return ITEM_NAME_CORRECTIONS.get(source_name, source_name)


def generated_item_code(name: str, unit_code: str) -> str:
    digest = hashlib.sha256(f"{name}|{unit_code}".encode()).hexdigest()[:8].upper()
    return f"PUR-{digest}"


def item_type_for(name: str) -> str:
    if name in RESALE_NAMES:
        return ItemType.GOODS_FOR_RESALE
    if any(word in name for word in PACKAGING_WORDS):
        return ItemType.PACKAGING
    if any(word in name for word in CONSUMABLE_WORDS):
        return ItemType.CONSUMABLE
    return ItemType.RAW_MATERIAL


class Command(BaseCommand):
    help = "Import the verified August 2026 purchase-invoice PDF extraction."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("source_json", type=Path)
        parser.add_argument("--username", default="moamel")
        parser.add_argument("--branch-code", default="011")
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Persist the import. Without this flag the full import is rolled back.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        source_path: Path = options["source_json"]
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Could not read {source_path}: {exc}") from exc
        if not isinstance(payload, list) or not payload:
            raise CommandError("The source must contain a non-empty JSON invoice list.")

        try:
            actor = User.objects.get(username=options["username"], is_active=True)
            branch = Branch.objects.select_related("organization").get(code=options["branch_code"])
            category = ItemCategory.objects.get(
                organization=branch.organization, code="KITCHEN", is_active=True
            )
        except (User.DoesNotExist, Branch.DoesNotExist, ItemCategory.DoesNotExist) as exc:
            raise CommandError(f"Required system setup is missing: {exc}") from exc

        stats = {
            "suppliers_created": 0,
            "items_created": 0,
            "invoices_created": 0,
            "invoices_skipped": 0,
            "lines_created": 0,
        }
        with transaction.atomic():
            for document in payload:
                self._import_document(
                    document=document,
                    actor=actor,
                    branch=branch,
                    category=category,
                    stats=stats,
                )
            if not options["commit"]:
                transaction.set_rollback(True)

        mode = "COMMITTED" if options["commit"] else "DRY RUN (rolled back)"
        self.stdout.write(self.style.SUCCESS(f"{mode}: {json.dumps(stats, sort_keys=True)}"))

    def _import_document(
        self,
        *,
        document: dict[str, Any],
        actor: User,
        branch: Branch,
        category: ItemCategory,
        stats: dict[str, int],
    ) -> None:
        source_code = str(document["supplier_code"])
        if source_code not in SUPPLIER_NAMES:
            raise CommandError(f"Unknown supplier code {source_code}")
        supplier_code = canonical_supplier_code(source_code)
        supplier = Supplier.objects.filter(
            organization=branch.organization, code=supplier_code
        ).first()
        if supplier is None:
            supplier = create_supplier(
                organization=branch.organization,
                code=supplier_code,
                name_ar=SUPPLIER_NAMES[source_code],
                notes=f"رمز المصدر: {source_code}; استيراد: {IMPORT_TAG}",
            )
            stats["suppliers_created"] += 1

        invoice_number = str(document["invoice_number"])
        existing = (
            SupplierInvoice.objects.filter(
                organization=branch.organization,
                supplier=supplier,
                supplier_invoice_number_key=invoice_number.strip().upper(),
            )
            .exclude(status="REVERSED")
            .first()
        )
        if existing is not None:
            expected_total = Decimal(str(document["invoice_total"]))
            if (
                existing.invoice_date != date.fromisoformat(document["invoice_date"])
                or existing.total_amount != expected_total
                or existing.lines.count() != len(document["lines"])
                or IMPORT_TAG not in existing.notes
            ):
                raise CommandError(f"Existing invoice {invoice_number} does not match this import.")
            stats["invoices_skipped"] += 1
            return

        lines_total = sum(
            (Decimal(str(line["line_total"])) for line in document["lines"]),
            start=Decimal("0"),
        )
        invoice_total = Decimal(str(document["invoice_total"]))
        discount = lines_total - invoice_total
        if discount < 0:
            raise CommandError(
                f"Invoice {invoice_number} needs a positive surcharge, not a discount."
            )
        stated_adjustment = Decimal(str(document["rounding_adjustment"]))
        if stated_adjustment != -discount:
            raise CommandError(f"Invoice {invoice_number} has an inconsistent rounding adjustment.")

        source_file = str(document["source_file"])
        paid = document.get("paid_amount")
        remaining = document.get("remaining_amount")
        invoice_day = date.fromisoformat(document["invoice_date"])
        invoice = create_supplier_invoice(
            supplier=supplier,
            branch=branch,
            created_by=actor,
            supplier_invoice_number=invoice_number,
            invoice_date=invoice_day,
            business_date=invoice_day,
            supplier_reference=Path(source_file).name,
            discount_amount=discount,
            notes=(
                f"استيراد: {IMPORT_TAG}\n"
                f"ملف المصدر: {source_file}\n"
                f"رمز المورد في المصدر: {source_code}\n"
                f"المسدد في المصدر: {paid if paid is not None else 'غير مذكور'} د.ع.\n"
                f"المتبقي في المصدر: {remaining if remaining is not None else 'غير مذكور'} د.ع.\n"
                f"فرق التقريب/الخصم في المصدر: {discount} د.ع."
            ),
        )

        for source_line in document["lines"]:
            source_unit = str(source_line["unit_name"])
            try:
                unit_code = UNIT_CODES[source_unit]
            except KeyError as exc:
                raise CommandError(
                    f"Invoice {invoice_number} has unknown unit {source_unit}"
                ) from exc
            name = canonical_item_name(str(source_line["item_name"]))
            item = self._resolve_item(
                name=name,
                unit_code=unit_code,
                category=category,
                source_name=str(source_line["item_name"]),
                stock_code=source_line.get("stock_code"),
                stats=stats,
            )
            quantity = Decimal(str(source_line["quantity"]))
            stated_line_total = Decimal(str(source_line["line_total"]))
            effective_unit_price = stated_line_total / quantity
            line = add_inventory_line(
                invoice=invoice,
                item=item,
                base_quantity=quantity,
                unit_price=effective_unit_price,
                description=name,
                note=(
                    f"وحدة PDF: {source_line.get('source_unit_name', source_unit)}; "
                    f"سعر ظاهر: {source_line['unit_price']}; "
                    f"إجمالي ظاهر: {stated_line_total}"
                    + (
                        f"; أُدخل بوحدة {source_unit} بقرار المالك"
                        if source_line.get("source_unit_name")
                        else ""
                    )
                    + (
                        f"; الصنف في المصدر: {source_line['source_item_name']}"
                        if source_line.get("source_item_name")
                        else ""
                    )
                ),
            )
            if line.line_amount != stated_line_total:
                raise CommandError(
                    f"Invoice {invoice_number} line {source_line['sequence']} did not reproduce "
                    f"the PDF total ({line.line_amount} != {stated_line_total})."
                )
            stats["lines_created"] += 1

        invoice.refresh_from_db()
        if invoice.total_amount != invoice_total:
            raise CommandError(
                f"Invoice {invoice_number} total does not reconcile "
                f"({invoice.total_amount} != {invoice_total})."
            )
        stats["invoices_created"] += 1

    def _resolve_item(
        self,
        *,
        name: str,
        unit_code: str,
        category: ItemCategory,
        source_name: str,
        stats: dict[str, int],
        stock_code: str | None = None,
    ) -> InventoryItem:
        # A line that names its stock code resolves to that row or stops the
        # import. `EXISTING_ITEM_CODES` below still holds the `ING-*` codes of
        # the item master this command was written against, which the real
        # `STK-*` register has since replaced; falling through to the generated
        # `PUR-*` branch would mint a duplicate of an item the branch already
        # stocks, and the owner's standing rule is that a missing item is
        # reported, never invented.
        if stock_code:
            item = (
                InventoryItem.objects.select_related("base_unit")
                .filter(organization=category.organization, code=stock_code, is_active=True)
                .first()
            )
            if item is None:
                raise CommandError(f"Stock item {stock_code} is missing or archived.")
            if item.base_unit.code != unit_code:
                raise CommandError(
                    f"Item {stock_code} is stocked in {item.base_unit.code}, "
                    f"but the invoice buys it in {unit_code} ({source_name})."
                )
            return item

        existing_code = EXISTING_ITEM_CODES.get(name)
        if existing_code is not None:
            try:
                item = InventoryItem.objects.select_related("base_unit").get(
                    organization=category.organization, code=existing_code, is_active=True
                )
            except InventoryItem.DoesNotExist as exc:
                raise CommandError(f"Mapped item {existing_code} is missing.") from exc
            if item.base_unit.code != unit_code:
                raise CommandError(
                    f"Mapped item {existing_code} uses {item.base_unit.code}, not {unit_code}."
                )
            return item

        code = generated_item_code(name, unit_code)
        generated_item = (
            InventoryItem.objects.select_related("base_unit")
            .filter(organization=category.organization, code=code)
            .first()
        )
        if generated_item is not None:
            if generated_item.name_ar != name or generated_item.base_unit.code != unit_code:
                raise CommandError(f"Generated item code collision for {name}.")
            return generated_item

        try:
            base_unit = UnitOfMeasure.objects.get(code=unit_code, is_active=True)
        except UnitOfMeasure.DoesNotExist as exc:
            raise CommandError(f"Required unit {unit_code} is missing.") from exc
        created_item = create_item(
            organization=category.organization,
            code=code,
            name_ar=name,
            category=category,
            item_type=item_type_for(name),
            base_unit=base_unit,
            notes=f"وصف المصدر: {source_name}; استيراد: {IMPORT_TAG}",
        )
        stats["items_created"] += 1
        return created_item
