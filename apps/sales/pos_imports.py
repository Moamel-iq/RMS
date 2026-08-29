"""Import and reconcile the six daily XLSX reports exported by the external POS."""

from __future__ import annotations

import datetime
import hashlib
import io
import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction

from apps.core.models import AuditAction
from apps.core.services import record_audit_event
from apps.sales.models import (
    DeliveryApplication,
    MenuItem,
    PosMenuItemMapping,
    PosSalesImportBatch,
    PosSalesImportFile,
    PosSalesImportStatus,
)

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile

    from apps.organizations.models import Branch
    from apps.users.models import User


MAX_FILE_SIZE = 8 * 1024 * 1024
MAX_UNCOMPRESSED_SIZE = 30 * 1024 * 1024
EXPECTED_REPORTS = {
    "sales_items": "مبيعات الأصناف",
    "sales_final": "التقرير الشامل",
    "item_sales_by_type": "مبيعات الأصناف حسب نوع الطلب",
    "sales_by_type": "المبيعات حسب نوع الطلب",
    "sales_by_category": "المبيعات حسب المجموعة",
    "expenses": "المصاريف وحركة التطبيقات",
}
REPORT_TITLES = {
    "تقرير مبيعات المواد": "sales_items",
    "تقرير شامل": "sales_final",
    "تقرير انواع مبيعات المواد": "item_sales_by_type",
    "تقرير انواع المبيعات": "sales_by_type",
    "تقرير مبيعات المجموعات": "sales_by_category",
    "تقرير مصاريف": "expenses",
}
APPLICATION_ALIASES = {
    "طلبات": "طلبات",
    "تطبيق طلبات": "طلبات",
    "بلي": "بلي",
    "بالي": "بلي",
    "توترز": "توترز",
    "طلباتي": "طلباتي",
    "عالسريع": "على السريع",
    "على السريع": "على السريع",
}


def _xml_root(content: bytes) -> ElementTree.Element:
    """Parse OOXML only after refusing DTD/entity declarations."""
    upper = content[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValidationError("ملف XLSX يحتوي على تعريف XML غير مسموح.")
    # ElementTree does not load external resources; the explicit DTD refusal
    # above and the archive expansion cap protect this narrow OOXML reader.
    return ElementTree.fromstring(content)  # noqa: S314


def normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = text.replace("ـ", "")
    text = re.sub(r"[.،,:؛/\\()\[\]{}_-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.001"))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValidationError("وجدت قيمة رقمية غير صالحة في أحد تقارير POS.") from error


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if not letters:
        return 0
    number = 0
    for char in letters.group(0):
        number = number * 26 + ord(char) - 64
    return number


class XlsxSheet:
    """Small, read-only XLSX reader for values; formulas and formatting are ignored."""

    def __init__(self, content: bytes) -> None:
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as error:
            raise ValidationError("الملف ليس مصنف XLSX صالحاً.") from error
        with archive:
            members = archive.infolist()
            if (
                len(members) > 2000
                or sum(item.file_size for item in members) > MAX_UNCOMPRESSED_SIZE
            ):
                raise ValidationError("ملف XLSX أكبر من الحدود الآمنة للاستيراد.")
            if any(item.flag_bits & 1 for item in members):
                raise ValidationError("ملفات XLSX المشفرة غير مدعومة.")
            shared = self._shared_strings(archive)
            worksheet_path = self._first_worksheet_path(archive)
            root = _xml_root(archive.read(worksheet_path))
            namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            self.rows: list[dict[int, Any]] = []
            for row_element in root.findall(".//x:sheetData/x:row", namespace):
                row: dict[int, Any] = {}
                for cell in row_element.findall("x:c", namespace):
                    reference = cell.attrib.get("r", "")
                    column = _column_number(reference)
                    kind = cell.attrib.get("t", "n")
                    value_element = cell.find("x:v", namespace)
                    if kind == "inlineStr":
                        texts = [node.text or "" for node in cell.findall(".//x:t", namespace)]
                        value: Any = "".join(texts)
                    elif value_element is None:
                        continue
                    elif kind == "s":
                        value = shared[int(value_element.text or "0")]
                    elif kind in {"str", "e"}:
                        value = value_element.text or ""
                    elif kind == "b":
                        value = value_element.text == "1"
                    else:
                        raw = value_element.text or ""
                        try:
                            value = Decimal(raw)
                        except InvalidOperation:
                            value = raw
                    if isinstance(value, str):
                        value = value.strip()
                    if value not in (None, ""):
                        row[column] = value
                if row:
                    self.rows.append(row)

    @staticmethod
    def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
        try:
            root = _xml_root(archive.read("xl/sharedStrings.xml"))
        except KeyError:
            return []
        namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        return [
            "".join(node.text or "" for node in item.findall(".//x:t", namespace))
            for item in root.findall("x:si", namespace)
        ]

    @staticmethod
    def _first_worksheet_path(archive: zipfile.ZipFile) -> str:
        main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        office = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        package = "http://schemas.openxmlformats.org/package/2006/relationships"
        workbook = _xml_root(archive.read("xl/workbook.xml"))
        first_sheet = workbook.find(f".//{{{main}}}sheet")
        if first_sheet is None:
            raise ValidationError("ملف XLSX لا يحتوي على ورقة عمل.")
        relation_id = first_sheet.attrib[f"{{{office}}}id"]
        relationships = _xml_root(archive.read("xl/_rels/workbook.xml.rels"))
        for relation in relationships.findall(f"{{{package}}}Relationship"):
            if relation.attrib.get("Id") == relation_id:
                target = relation.attrib["Target"].lstrip("/")
                return target if target.startswith("xl/") else posixpath.normpath(f"xl/{target}")
        raise ValidationError("تعذر تحديد ورقة العمل داخل ملف XLSX.")


@dataclass(frozen=True)
class ParsedReport:
    report_type: str
    business_date: datetime.date
    data: dict[str, Any]


@dataclass(frozen=True)
class UploadedReport:
    original_name: str
    content: bytes
    checksum: str
    parsed: ParsedReport


def _row_texts(row: dict[int, Any]) -> list[str]:
    return [normalize_name(value) for value in row.values() if isinstance(value, str)]


def _detect_report(rows: list[dict[int, Any]]) -> str:
    candidates = set()
    for row in rows[:12]:
        for text in _row_texts(row):
            for title, report_type in REPORT_TITLES.items():
                if normalize_name(title) == text:
                    candidates.add(report_type)
    if len(candidates) != 1:
        raise ValidationError("تعذر تحديد نوع تقرير POS من عنوانه الداخلي.")
    return candidates.pop()


def _business_date(rows: list[dict[int, Any]]) -> datetime.date:
    serials: set[int] = set()
    for row in rows[:15]:
        texts = set(_row_texts(row))
        if "من" not in texts and "الى" not in texts:
            continue
        for value in row.values():
            if isinstance(value, Decimal) and Decimal("40000") <= value <= Decimal("70000"):
                serials.add(int(value))
    if len(serials) != 1:
        raise ValidationError("يجب أن يغطي كل تقرير يوم عمل واحداً فقط.")
    return datetime.date(1899, 12, 30) + datetime.timedelta(days=serials.pop())


def _header(row: dict[int, Any], labels: dict[str, str]) -> dict[str, int] | None:
    normalized = {column: normalize_name(value) for column, value in row.items()}
    found: dict[str, int] = {}
    for key, label in labels.items():
        column = next((column for column, value in normalized.items() if value == label), None)
        if column is None:
            return None
        found[key] = column
    return found


def _number_on_label_row(rows: list[dict[int, Any]], label: str, *, last: bool = True) -> Decimal:
    found: list[Decimal] = []
    wanted = normalize_name(label)
    for row in rows:
        if wanted not in _row_texts(row):
            continue
        numbers = [value for value in row.values() if isinstance(value, Decimal)]
        if numbers:
            found.append(_decimal(numbers[0]))
    if not found:
        raise ValidationError(f"التقرير لا يحتوي على القيمة المطلوبة: {label}.")
    return found[-1] if last else found[0]


def _detail_rows(
    rows: list[dict[int, Any]], labels: dict[str, str], *, repeated_headers: bool = False
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    columns: dict[str, int] | None = None
    for row_number, row in enumerate(rows, start=1):
        candidate = _header(row, labels)
        if candidate is not None:
            columns = candidate
            continue
        if columns is None:
            continue
        name = row.get(columns["name"])
        amount = row.get(columns["amount"])
        quantity = row.get(columns["quantity"]) if "quantity" in columns else None
        serial = row.get(columns["serial"]) if "serial" in columns else Decimal("1")
        if (
            not isinstance(name, str)
            or not isinstance(amount, Decimal)
            or not isinstance(serial, Decimal)
        ):
            continue
        line: dict[str, Any] = {
            "row": row_number,
            "name": normalize_name(name),
            "amount": _decimal(amount),
        }
        if quantity is not None:
            if not isinstance(quantity, Decimal):
                continue
            line["quantity"] = _decimal(quantity)
        result.append(line)
        if not repeated_headers and len(result) > 5000:
            raise ValidationError("تقرير POS يحتوي على عدد سطور غير متوقع.")
    return result


def _parse_sales_items(rows: list[dict[int, Any]]) -> dict[str, Any]:
    items = _detail_rows(
        rows, {"name": "اسم المادة", "quantity": "الكمية", "amount": "اجمالي البيع", "serial": "ت"}
    )
    return {
        "items": items,
        # This export prints the grand total as an unlabeled merged cell.  The
        # detail rows are the authoritative source, so derive both controls
        # from them instead of relying on presentation coordinates.
        "total_sales": sum((line["amount"] for line in items), Decimal("0")),
        "total_quantity": sum((line["quantity"] for line in items), Decimal("0")),
    }


def _parse_item_sales_by_type(rows: list[dict[int, Any]]) -> dict[str, Any]:
    result: list[dict[str, Any]] = []
    current_section = ""
    pending_heading = ""
    columns: dict[str, int] | None = None
    for row_number, row in enumerate(rows, start=1):
        texts = _row_texts(row)
        numbers = [value for value in row.values() if isinstance(value, Decimal)]
        if len(texts) == 1 and not numbers and "تقرير" not in texts[0]:
            pending_heading = texts[0]
        candidate = _header(
            row,
            {"name": "اسم المادة", "quantity": "الكمية", "amount": "اجمالي البيع", "serial": "ت"},
        )
        if candidate is not None:
            columns = candidate
            current_section = pending_heading
            continue
        if columns is None:
            continue
        name = row.get(columns["name"])
        quantity = row.get(columns["quantity"])
        amount = row.get(columns["amount"])
        serial = row.get(columns["serial"])
        if (
            isinstance(name, str)
            and isinstance(quantity, Decimal)
            and isinstance(amount, Decimal)
            and isinstance(serial, Decimal)
        ):
            result.append(
                {
                    "row": row_number,
                    "channel": current_section,
                    "name": normalize_name(name),
                    "quantity": _decimal(quantity),
                    "amount": _decimal(amount),
                }
            )
    totals: dict[str, Decimal] = {}
    quantities: dict[str, Decimal] = {}
    for line in result:
        totals[line["channel"]] = totals.get(line["channel"], Decimal("0")) + line["amount"]
        quantities[line["channel"]] = (
            quantities.get(line["channel"], Decimal("0")) + line["quantity"]
        )
    return {
        "items": result,
        "channel_totals": totals,
        "channel_quantities": quantities,
        "total_sales": sum(totals.values(), Decimal("0")),
        "total_quantity": sum(quantities.values(), Decimal("0")),
    }


def _parse_summary(rows: list[dict[int, Any]], name_label: str) -> dict[str, Any]:
    lines = _detail_rows(rows, {"name": name_label, "amount": "اجمالي البيع", "serial": "ت"})
    return {"lines": lines, "total_sales": sum((line["amount"] for line in lines), Decimal("0"))}


def _parse_expenses(rows: list[dict[int, Any]]) -> dict[str, Any]:
    columns: dict[str, int] | None = None
    operator = ""
    lines: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=1):
        candidate = _header(row, {"name": "نوع الصرف", "amount": "المبلغ", "details": "التفاصيل"})
        if candidate is not None:
            columns = candidate
            continue
        if columns is None:
            continue
        name = row.get(columns["name"])
        amount = row.get(columns["amount"])
        details = row.get(columns["details"], "")
        if isinstance(details, str) and not name and not amount:
            operator = normalize_name(details)
            continue
        if isinstance(name, str) and isinstance(amount, Decimal):
            normalized = normalize_name(name)
            application = APPLICATION_ALIASES.get(normalized, "")
            lines.append(
                {
                    "row": row_number,
                    "operator": operator,
                    "type": normalized,
                    "details": normalize_name(details),
                    "amount": _decimal(amount),
                    "application": application,
                }
            )
    total = _number_on_label_row(rows, "اجمالي المبلغ")
    application_total = sum((line["amount"] for line in lines if line["application"]), Decimal("0"))
    return {
        "lines": lines,
        "total_expenses": total,
        "application_sales": application_total,
        "operational_expenses": total - application_total,
    }


def _parse_sales_final(rows: list[dict[int, Any]]) -> dict[str, Any]:
    return {
        "total_sales": _number_on_label_row(rows, "اجمالي البيع"),
        "total_expenses": _number_on_label_row(rows, "اجمالي المصاريف"),
        "net_sales": _number_on_label_row(rows, "صافي البيع", last=True),
        "net_cash": _number_on_label_row(rows, "صافي الوارد", last=True),
    }


def parse_report(content: bytes) -> ParsedReport:
    sheet = XlsxSheet(content)
    report_type = _detect_report(sheet.rows)
    parsers = {
        "sales_items": _parse_sales_items,
        "sales_final": _parse_sales_final,
        "item_sales_by_type": _parse_item_sales_by_type,
        "sales_by_type": lambda rows: _parse_summary(rows, "نوع الطلب"),
        "sales_by_category": lambda rows: _parse_summary(rows, "المجموعة"),
        "expenses": _parse_expenses,
    }
    return ParsedReport(report_type, _business_date(sheet.rows), parsers[report_type](sheet.rows))


def read_upload(upload: UploadedFile[bytes]) -> UploadedReport:
    name = str(upload.name)
    if not name.lower().endswith(".xlsx"):
        raise ValidationError(f"{name}: الصيغة المقبولة هي XLSX فقط.")
    if upload.size is None or upload.size > MAX_FILE_SIZE:
        raise ValidationError(f"{name}: حجم الملف يتجاوز 8 ميغابايت.")
    content = upload.read()
    upload.seek(0)
    if not content.startswith(b"PK"):
        raise ValidationError(f"{name}: محتوى الملف لا يطابق صيغة XLSX.")
    return UploadedReport(name, content, hashlib.sha256(content).hexdigest(), parse_report(content))


def _check(label: str, values: dict[str, Decimal]) -> dict[str, Any]:
    unique = set(values.values())
    return {
        "label": label,
        "ok": len(unique) == 1,
        "values": {key: format(value, "f") for key, value in values.items()},
    }


def reconcile(reports: dict[str, ParsedReport]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sales_items = reports["sales_items"].data
    final = reports["sales_final"].data
    item_types = reports["item_sales_by_type"].data
    sale_types = reports["sales_by_type"].data
    categories = reports["sales_by_category"].data
    expenses = reports["expenses"].data
    checks = [
        _check(
            "إجمالي المبيعات",
            {
                "الأصناف": sales_items["total_sales"],
                "الأصناف حسب النوع": item_types["total_sales"],
                "أنواع المبيعات": sale_types["total_sales"],
                "المجموعات": categories["total_sales"],
                "التقرير الشامل": final["total_sales"],
            },
        ),
        _check(
            "إجمالي الكمية",
            {
                "الأصناف": sales_items["total_quantity"],
                "الأصناف حسب النوع": item_types["total_quantity"],
            },
        ),
        _check(
            "إجمالي المصاريف الظاهر",
            {"المصاريف": expenses["total_expenses"], "التقرير الشامل": final["total_expenses"]},
        ),
    ]
    application_from_types = sum(
        (line["amount"] for line in sale_types["lines"] if "تطبيق" in normalize_name(line["name"])),
        Decimal("0"),
    )
    checks.append(
        _check(
            "مبيعات التطبيقات",
            {
                "أنواع المبيعات": application_from_types,
                "تفصيل التطبيقات": expenses["application_sales"],
            },
        )
    )
    expected_cash = (
        final["total_sales"] - expenses["application_sales"] - expenses["operational_expenses"]
    )
    checks.append(
        _check("صافي النقد", {"محسوب": expected_cash, "التقرير الشامل": final["net_cash"]})
    )
    failures = [check["label"] for check in checks if not check["ok"]]
    if failures:
        raise ValidationError("تعذر الاستيراد لأن التقارير لا تتطابق في: " + "، ".join(failures))
    headline = {
        "total_sales": final["total_sales"],
        "application_sales": expenses["application_sales"],
        "reported_expenses": expenses["total_expenses"],
        "operational_expenses": expenses["operational_expenses"],
        "net_cash": final["net_cash"],
        "total_quantity": sales_items["total_quantity"],
    }
    return headline, checks


def _unmatched_item_names(*, organization: Any, item_names: set[str]) -> list[str]:
    menu_names = {
        normalize_name(name)
        for name in MenuItem.objects.filter(organization=organization, is_active=True).values_list(
            "name", flat=True
        )
    }
    mapped_names = set(
        PosMenuItemMapping.objects.filter(organization=organization).values_list(
            "normalized_source_name", flat=True
        )
    )
    return sorted(item_names - menu_names - mapped_names)


@transaction.atomic
def create_pos_menu_item_mapping(
    *, organization: Any, source_name: str, menu_item: MenuItem, actor: User
) -> PosMenuItemMapping:
    normalized = normalize_name(source_name)
    if not normalized:
        raise ValidationError("اسم صنف POS مطلوب.")
    if menu_item.organization_id != organization.pk:
        raise ValidationError("لا يمكن ربط صنف POS بصنف من مؤسسة أخرى.")
    existing = PosMenuItemMapping.objects.filter(
        organization=organization, normalized_source_name=normalized
    ).first()
    if existing is not None:
        if existing.menu_item_id == menu_item.pk:
            return existing
        raise ValidationError(f"اسم POS «{source_name}» مرتبط مسبقاً بصنف آخر.")
    mapping = PosMenuItemMapping(
        organization=organization,
        source_name=normalize_name(source_name),
        normalized_source_name=normalized,
        menu_item=menu_item,
        created_by=actor,
    )
    mapping.full_clean()
    mapping.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=mapping,
        organization=organization,
        new_state={
            "source_name": mapping.source_name,
            "menu_item_id": menu_item.pk,
            "menu_item_code": menu_item.code,
        },
    )
    return mapping


@transaction.atomic
def refresh_pos_import_mapping_status(batch: PosSalesImportBatch) -> PosSalesImportBatch:
    item_names = {
        normalize_name(line["name"])
        for line in batch.report_data.get("sales_items", {}).get("items", [])
    }
    unmatched_items = _unmatched_item_names(organization=batch.organization, item_names=item_names)
    warnings = [warning for warning in batch.warnings if warning.get("code") != "UNMATCHED_ITEMS"]
    if unmatched_items:
        warnings.insert(
            0,
            {
                "code": "UNMATCHED_ITEMS",
                "label": "أصناف POS غير مربوطة بالمنيو",
                "count": len(unmatched_items),
                "items": unmatched_items,
            },
        )
    batch.warnings = warnings
    if batch.status in {
        PosSalesImportStatus.DRAFT,
        PosSalesImportStatus.RETURNED_TO_CASHIER,
        PosSalesImportStatus.AWAITING_CASHIER,
    }:
        batch.status = (
            PosSalesImportStatus.DRAFT if warnings else PosSalesImportStatus.AWAITING_CASHIER
        )
    batch.save(update_fields=["warnings", "status", "updated_at"])
    return batch


@transaction.atomic
def import_pos_sales(
    *, branch: Branch, actor: User, uploads: list[UploadedFile[bytes]]
) -> tuple[PosSalesImportBatch, bool]:
    if len(uploads) != len(EXPECTED_REPORTS):
        raise ValidationError("يجب رفع التقارير الستة معاً في عملية واحدة.")
    loaded = [read_upload(upload) for upload in uploads]
    reports = {item.parsed.report_type: item.parsed for item in loaded}
    if set(reports) != set(EXPECTED_REPORTS) or len(reports) != len(loaded):
        missing = [label for key, label in EXPECTED_REPORTS.items() if key not in reports]
        raise ValidationError(
            "الملفات لا تمثل التقارير الستة المطلوبة. المفقود: " + "، ".join(missing)
        )
    dates = {report.business_date for report in reports.values()}
    if len(dates) != 1:
        raise ValidationError("كل التقارير يجب أن تخص تاريخ العمل نفسه.")
    business_date = dates.pop()
    headline, checks = reconcile(reports)
    digest = hashlib.sha256(
        "|".join(
            f"{item.parsed.report_type}:{item.checksum}"
            for item in sorted(loaded, key=lambda row: row.parsed.report_type)
        ).encode()
    ).hexdigest()
    existing = PosSalesImportBatch.objects.filter(
        organization=branch.organization, source_hash=digest
    ).first()
    if existing is not None:
        return existing, False

    item_names = {normalize_name(line["name"]) for line in reports["sales_items"].data["items"]}
    unmatched_items = _unmatched_item_names(organization=branch.organization, item_names=item_names)
    imported_apps = {
        line["application"] for line in reports["expenses"].data["lines"] if line["application"]
    }
    known_apps = {
        normalize_name(name)
        for name in DeliveryApplication.objects.filter(
            organization=branch.organization, is_active=True
        ).values_list("name", flat=True)
    }
    unmatched_apps = sorted(
        name for name in imported_apps if normalize_name(name) not in known_apps
    )
    warnings: list[dict[str, Any]] = []
    if unmatched_items:
        warnings.append(
            {
                "code": "UNMATCHED_ITEMS",
                "label": "أصناف POS غير مربوطة بالمنيو",
                "count": len(unmatched_items),
                "items": unmatched_items,
            }
        )
    if unmatched_apps:
        warnings.append(
            {
                "code": "UNMATCHED_APPLICATIONS",
                "label": "تطبيقات غير مربوطة",
                "count": len(unmatched_apps),
                "items": unmatched_apps,
            }
        )
    status = PosSalesImportStatus.DRAFT if warnings else PosSalesImportStatus.AWAITING_CASHIER
    batch = PosSalesImportBatch.objects.create(
        organization=branch.organization,
        branch=branch,
        business_date=business_date,
        status=status,
        source_hash=digest,
        total_sales=headline["total_sales"],
        application_sales=headline["application_sales"],
        reported_expenses=headline["reported_expenses"],
        operational_expenses=headline["operational_expenses"],
        net_cash=headline["net_cash"],
        total_quantity=headline["total_quantity"],
        report_data=_json_value({key: report.data for key, report in reports.items()}),
        checks=checks,
        warnings=warnings,
        created_by=actor,
    )
    for item in loaded:
        stored = PosSalesImportFile(
            batch=batch,
            report_type=item.parsed.report_type,
            original_name=item.original_name,
            checksum=item.checksum,
            size=len(item.content),
        )
        stored.file.save(item.original_name, ContentFile(item.content), save=False)
        stored.save()
    record_audit_event(
        action=AuditAction.IMPORTED,
        target=batch,
        branch=branch,
        new_state={
            "business_date": business_date,
            "source_hash": digest,
            "total_sales": headline["total_sales"],
            "application_sales": headline["application_sales"],
            "operational_expenses": headline["operational_expenses"],
            "file_count": len(loaded),
            "warning_count": len(warnings),
        },
        metadata={"report_types": sorted(reports)},
    )
    return batch, True


__all__ = [
    "EXPECTED_REPORTS",
    "create_pos_menu_item_mapping",
    "import_pos_sales",
    "normalize_name",
    "parse_report",
    "reconcile",
    "refresh_pos_import_mapping_status",
]
