"""
The import boundary: parse, judge, show, and only then write.

Four services and a validator per kind. The shape is the same for all of them
and the shape is the point:

    create_batch   parse the upload into rows. Writes nothing but the batch.
    validate_batch judge every row. Writes nothing but the verdicts.
    apply_batch    write, in one transaction, or write nothing.
    cancel_batch   close a batch nobody will apply, with a reason.

## Why preview is a separate step rather than a dry run flag

A dry run that shares a code path with the real thing is one `if` away from
writing during the preview, and the failure is silent — the operator sees a
preview and the database has already changed. Here validation *cannot* write
because it has nothing to write with: it produces `ImportRowResult` verdicts,
and only `apply_batch` knows how to turn a verdict into a record.

## Atomicity, and the 99-of-100 case

`apply_batch` refuses a batch with any invalid row. It does not apply the 99
and report the 1.

The alternative was considered and rejected. A spreadsheet is one act of
intent; applying most of it leaves the operator holding a file that is neither
applied nor not, and the only way to find out which rows landed is to read
them back one at a time. Refusing is recoverable in a way that partial success
is not — fix the row, upload again.

The preview shows all 100 rows, valid and invalid, because knowing *which* row
is wrong is exactly what makes it fixable.

## Idempotency

`content_hash` fingerprints the normalised rows. A partial unique index allows
only one APPLIED batch per (organization, kind, content_hash), so re-applying
the same spreadsheet is recognised as a retry rather than doubling every row it
touches. Re-*uploading* is fine — people lose tabs.

Apply is also idempotent within a batch: every writer is an
`update_or_create`-shaped operation keyed on the row's natural identity, so a
row that asks for a value the record already holds counts as `unchanged`
rather than as a change. That is why `applied_row_count` can be lower than
`valid_row_count` without either being wrong.

## Decimals

Every numeric field goes through `apps.core.quantity` / `apps.core.money`. The
payload stores what the file said, as text. Nothing here calls `float()`.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.context import get_actor
from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import (
    BRANCH_SCOPED_IMPORT_KINDS,
    BranchItemSetting,
    ImportBatch,
    ImportBatchStatus,
    ImportKind,
    ImportRowResult,
    InventoryItem,
    ItemCategory,
    PackageUnit,
)
from apps.inventory.services import set_branch_item_setting
from apps.organizations.models import Branch, Organization

#: Bigger than any legitimate master-data sheet and small enough that a hostile
#: upload cannot exhaust memory before it is rejected. Checked on the bytes,
#: before anything is decoded.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

#: More rows than this is not a master-data import, it is a migration, and it
#: should be done with a reviewed script rather than a web form.
MAX_ROWS = 5_000

#: Only these. `.xlsm` and `.xlsb` are absent deliberately: a macro-enabled
#: workbook is a program, and this accepts data.
ALLOWED_EXTENSIONS = (".csv",)

#: What the header row must contain, per kind. Extra columns are ignored;
#: missing ones fail the whole file before any row is judged, because a file
#: with the wrong shape is a file somebody exported from the wrong report.
#:
#: One entry per `ImportKind`, and a test asserts that. The enum, the columns,
#: the validators and the writers are four lists that have to agree, and the
#: way they stop agreeing is somebody adding a kind to one of them.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    ImportKind.ITEM_CATEGORY: ("code", "name"),
    ImportKind.PACKAGE_UNIT: ("code", "name"),
    ImportKind.BRANCH_ITEM_SETTING: ("item_code", "is_stocked"),
}


class ImportRefusedError(ValidationError):
    """The file itself is unusable — wrong shape, wrong size, wrong type."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _decode(raw: bytes) -> str:
    """
    UTF-8, with or without the BOM Excel writes.

    Refused rather than guessed at when it is neither: a mis-decoded Arabic
    item name would be imported as mojibake and look like a data-entry error
    forever after.
    """
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportRefusedError(
        _("الملف ليس بترميز UTF-8. صدّر الملف من جديد بترميز UTF-8."),
        code="import_bad_encoding",
    )


def parse_rows(raw: bytes, *, filename: str, kind: str) -> list[dict[str, str]]:
    """
    The uploaded bytes as a list of string dicts. Nothing is coerced here.

    Values stay text all the way into `ImportRowResult.payload`, so the record
    of what the file said is exactly what the file said. Coercion happens in
    validation, through the approved Decimal utilities, where a failure has a
    row number to attach itself to.
    """
    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise ImportRefusedError(
            _("النوع غير مدعوم. المسموح: %(allowed)s."),
            code="import_bad_extension",
            params={"allowed": ", ".join(ALLOWED_EXTENSIONS)},
        )
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ImportRefusedError(
            _("حجم الملف يتجاوز الحد المسموح (%(limit)s بايت)."),
            code="import_too_large",
            params={"limit": MAX_UPLOAD_BYTES},
        )
    if not raw.strip():
        raise ImportRefusedError(_("الملف فارغ."), code="import_empty")

    reader = csv.DictReader(io.StringIO(_decode(raw)))
    headers = [name.strip() for name in (reader.fieldnames or [])]

    # `DictReader` keeps the last of a repeated header and drops the rest
    # silently, so a file with two `reorder_point` columns would import one of
    # them and the operator would never learn which. Refused instead.
    duplicated = sorted({name for name in headers if headers.count(name) > 1})
    if duplicated:
        raise ImportRefusedError(
            _("أعمدة مكررة: %(columns)s."),
            code="import_duplicate_columns",
            params={"columns": ", ".join(duplicated)},
        )

    missing = [column for column in REQUIRED_COLUMNS[kind] if column not in headers]
    if missing:
        raise ImportRefusedError(
            _("أعمدة مفقودة: %(missing)s."),
            code="import_missing_columns",
            params={"missing": ", ".join(missing)},
        )

    rows: list[dict[str, str]] = []
    for row in reader:
        if len(rows) >= MAX_ROWS:
            raise ImportRefusedError(
                _("عدد السطور يتجاوز %(limit)s."),
                code="import_too_many_rows",
                params={"limit": MAX_ROWS},
            )
        rows.append(
            {
                (key or "").strip(): (value or "").strip()
                for key, value in row.items()
                if key is not None
            }
        )
    if not rows:
        raise ImportRefusedError(_("لا توجد سطور بيانات بعد العناوين."), code="import_no_rows")
    return rows


def fingerprint(kind: str, rows: Sequence[dict[str, str]]) -> str:
    """
    A stable hash of what the file asks for, ignoring how it was written.

    Sorted keys and a canonical separator, so the same content saved by two
    different spreadsheets fingerprints the same — otherwise the retry guard
    would depend on whether Excel quoted a field.
    """
    payload = json.dumps(
        [dict(sorted(row.items())) for row in rows], ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(f"{kind}\n{payload}".encode()).hexdigest()


def sanitise_filename(name: str) -> str:
    """
    Keep it for the audit trail; strip anything that could act.

    No directory separators, no control characters, no bidirectional overrides
    (a right-to-left override can make `evil.csv.exe` render as `exe.csv.live`),
    and bounded length.
    """
    cleaned = "".join(
        character
        for character in name
        if character.isprintable()
        and character not in "\\/\x00"
        and not (0x202A <= ord(character) <= 0x202E)
    )
    return cleaned.strip()[:255] or "upload.csv"


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------
#
# A validator reads one row's strings and returns `(errors, cleaned)`. It may
# read the database — it must, to know whether an item code exists — and it
# must never write to it. Everything a validator resolves is resolved *inside*
# the batch's own organization and branch, so a row naming another
# organization's item finds nothing rather than finding it.


def _decimal(
    raw: str, field: str, errors: dict[str, list[str]], *, quantity: bool = True
) -> Decimal | None:
    """One numeric cell, through the approved utilities. Never `float`."""
    if not raw:
        errors.setdefault(field, []).append(_("قيمة مطلوبة."))
        return None
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation, ArithmeticError, ValueError:
        errors.setdefault(field, []).append(_("ليست قيمة رقمية صحيحة."))
        return None
    try:
        return quantize_quantity(value) if quantity else quantize_money(value)
    except ValidationError:
        errors.setdefault(field, []).append(_("عدد المنازل العشرية غير مقبول."))
        return None


def _boolean(raw: str, field: str, errors: dict[str, list[str]]) -> bool | None:
    normalised = raw.strip().lower()
    if normalised in ("1", "true", "yes", "y", "نعم"):
        return True
    if normalised in ("0", "false", "no", "n", "لا"):
        return False
    errors.setdefault(field, []).append(_("القيمة يجب أن تكون نعم أو لا."))
    return None


def _validate_branch_item_setting(
    row: dict[str, str], *, organization: Organization, branch: Branch
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """
    Reorder settings for an item this organization already stocks.

    Resolved **within the batch's organization**: a row naming another
    organization's item code finds nothing and fails as unknown, which is the
    same answer somebody would get from the screen and reveals nothing about
    whether that code exists elsewhere.
    """
    errors: dict[str, list[str]] = {}
    cleaned: dict[str, Any] = {}

    code = row.get("item_code", "")
    if not code:
        errors["item_code"] = [_("رمز الصنف مطلوب.")]
    else:
        item = InventoryItem.objects.filter(organization=organization, code=code).first()
        if item is None:
            errors["item_code"] = [_("لا يوجد صنف بهذا الرمز في هذه المؤسسة.")]
        elif not item.is_active:
            errors["item_code"] = [_("الصنف موقوف.")]
        else:
            cleaned["item_id"] = item.pk

    stocked = _boolean(row.get("is_stocked", ""), "is_stocked", errors)
    if stocked is not None:
        cleaned["is_stocked"] = stocked

    for field in ("reorder_point", "reorder_quantity"):
        raw = row.get(field, "")
        if not raw:
            cleaned[field] = None
            continue
        value = _decimal(raw, field, errors)
        if value is None:
            continue
        if value < 0:
            errors.setdefault(field, []).append(_("لا يمكن أن تكون القيمة سالبة."))
        else:
            cleaned[field] = value

    cleaned["branch_id"] = branch.pk
    return errors, cleaned


def _validate_item_category(
    row: dict[str, str], *, organization: Organization, branch: Branch | None
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    errors: dict[str, list[str]] = {}
    cleaned: dict[str, Any] = {}
    code = row.get("code", "").strip().upper()
    if not code:
        errors["code"] = [_("الرمز مطلوب.")]
    else:
        cleaned["code"] = code
    if not row.get("name", ""):
        errors["name"] = [_("الاسم مطلوب.")]
    else:
        cleaned["name"] = row["name"]

    parent_code = row.get("parent_code", "").strip().upper()
    if parent_code:
        parent = ItemCategory.objects.filter(organization=organization, code=parent_code).first()
        if parent is None:
            errors["parent_code"] = [_("لا توجد مجموعة أب بهذا الرمز.")]
        else:
            cleaned["parent_id"] = parent.pk
    return errors, cleaned


def _validate_package_unit(
    row: dict[str, str], *, organization: Organization, branch: Branch | None
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    errors: dict[str, list[str]] = {}
    cleaned: dict[str, Any] = {}
    code = row.get("code", "").strip().upper()
    if not code:
        errors["code"] = [_("الرمز مطلوب.")]
    else:
        cleaned["code"] = code
    if not row.get("name", ""):
        errors["name"] = [_("الاسم مطلوب.")]
    else:
        cleaned["name"] = row["name"]
    return errors, cleaned


#: kind -> validator. A kind with no validator cannot be uploaded, which is
#: how the unsupported ones stay unsupported rather than half-working.
VALIDATORS: dict[str, Callable[..., tuple[dict[str, list[str]], dict[str, Any]]]] = {
    ImportKind.BRANCH_ITEM_SETTING: _validate_branch_item_setting,
    ImportKind.ITEM_CATEGORY: _validate_item_category,
    ImportKind.PACKAGE_UNIT: _validate_package_unit,
}


def supported_kinds() -> list[str]:
    """The kinds this release can actually import."""
    return sorted(VALIDATORS)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


@transaction.atomic
def create_batch(
    *,
    organization: Organization,
    kind: str,
    raw: bytes,
    filename: str,
    branch: Branch | None = None,
    notes: str = "",
) -> ImportBatch:
    """
    Parse an upload into a batch and its rows. Changes nothing else.

    The batch is created even when every row will turn out to be wrong: the
    record of an attempted import is worth keeping, and a failed batch is one
    of the things §K asks to be visible.
    """
    if kind not in VALIDATORS:
        raise ImportRefusedError(
            _("نوع الاستيراد غير مدعوم في هذا الإصدار."), code="import_kind_unsupported"
        )
    needs_branch = kind in BRANCH_SCOPED_IMPORT_KINDS
    if needs_branch and branch is None:
        raise ImportRefusedError(_("هذا النوع يحتاج فرعاً."), code="import_branch_required")
    if not needs_branch and branch is not None:
        raise ImportRefusedError(_("هذا النوع لا يقبل فرعاً."), code="import_branch_not_allowed")
    if branch is not None and branch.organization_id != organization.pk:
        raise ImportRefusedError(_("الفرع يتبع مؤسسة أخرى."), code="import_branch_mismatch")

    rows = parse_rows(raw, filename=filename, kind=kind)
    batch = ImportBatch.objects.create(
        organization=organization,
        branch=branch,
        kind=kind,
        status=ImportBatchStatus.UPLOADED,
        original_filename=sanitise_filename(filename),
        content_hash=fingerprint(kind, rows),
        byte_size=len(raw),
        row_count=len(rows),
        valid_row_count=0,
        error_row_count=len(rows),
        uploaded_by=get_actor(),
        notes=notes,
    )
    ImportRowResult.objects.bulk_create(
        [
            ImportRowResult(
                batch=batch,
                # The header is row 1, so the first data row is 2 — the number
                # the operator sees in their own spreadsheet.
                row_number=index + 2,
                external_key=_external_key(kind, row),
                is_valid=False,
                errors={},
                payload=row,
            )
            for index, row in enumerate(rows)
        ]
    )
    record_audit_event(action=AuditAction.CREATED, target=batch, new_state=snapshot(batch))
    return batch


#: kind -> a function deriving the row's natural identity, for kinds whose
#: identity is compound (a catalogue row is supplier *and* item; either alone
#: would flag two different rows as in-file duplicates). Registered by the
#: owning module, exactly like `VALIDATORS`; absent means the default columns.
EXTERNAL_KEYS: dict[str, Callable[[dict[str, str]], str]] = {}

#: kind -> the permission it needs, for kinds another module registered.
#: Filled the same way `VALIDATORS` is — from the owning module at app
#: ready — so an unregistered kind still falls back to this module's own
#: master-data permission and can never arrive permissionless.
#:
#: This lived in `import_views` until the import-log screens were withdrawn
#: from the product. It belongs beside the other registries anyway: it is part
#: of the framework a module registers into, not part of a screen.
KIND_PERMISSIONS: dict[str, str] = {}


def permission_for_kind(kind: str) -> str:
    """Which right this kind needs. Data, so the two cannot drift apart."""
    from apps.inventory.models import OPENING_IMPORT_KINDS
    from apps.inventory.permissions import IMPORT_MASTER_DATA, IMPORT_OPENING_DRAFT

    registered = KIND_PERMISSIONS.get(kind)
    if registered is not None:
        return registered
    return IMPORT_OPENING_DRAFT if kind in OPENING_IMPORT_KINDS else IMPORT_MASTER_DATA


def _external_key(kind: str, row: dict[str, str]) -> str:
    derive = EXTERNAL_KEYS.get(kind)
    if derive is not None:
        return derive(row)[:200]
    for column in ("code", "item_code", "warehouse_code"):
        if row.get(column):
            return row[column][:200]
    return ""


@transaction.atomic
def validate_batch(*, batch: ImportBatch) -> ImportBatch:
    """
    Judge every row and record the verdict. Writes no master data.

    Re-runnable while the batch is not terminal: an operator who fixed a
    reference record legitimately wants the same upload judged again.
    """
    locked = ImportBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.is_terminal:
        raise ValidationError(
            _("الدفعة مغلقة ولا يمكن إعادة تدقيقها."), code="import_batch_terminal"
        )

    validator = VALIDATORS[locked.kind]
    before = snapshot(locked)
    valid = 0
    duplicates: set[str] = set()
    seen: set[str] = set()

    rows = list(locked.rows.select_for_update().order_by("row_number"))
    for row in rows:
        errors, cleaned = validator(
            row.payload, organization=locked.organization, branch=locked.branch
        )
        # A file that names the same record twice is asking for two different
        # answers to one question. Which one wins would depend on row order,
        # so neither does.
        key = _external_key(locked.kind, row.payload)
        if key:
            if key in seen:
                errors.setdefault("__all__", []).append(_("مكرر داخل الملف نفسه."))
                duplicates.add(key)
            seen.add(key)
        row.errors = errors
        row.is_valid = not errors
        row.payload = (
            {**row.payload, "__cleaned__": _jsonable(cleaned)} if not errors else row.payload
        )
        valid += 1 if row.is_valid else 0

    # A duplicate poisons every copy, not just the second one.
    if duplicates:
        for row in rows:
            if _external_key(locked.kind, row.payload) in duplicates and row.is_valid:
                row.errors = {"__all__": [_("مكرر داخل الملف نفسه.")]}
                row.is_valid = False
                valid -= 1

    ImportRowResult.objects.bulk_update(rows, ["errors", "is_valid", "payload"])

    locked.valid_row_count = valid
    locked.error_row_count = locked.row_count - valid
    locked.status = (
        ImportBatchStatus.VALIDATED
        if valid == locked.row_count
        else ImportBatchStatus.FAILED_VALIDATION
    )
    locked.validated_at = timezone.now()
    locked.save(
        update_fields=["valid_row_count", "error_row_count", "status", "validated_at", "updated_at"]
    )
    record_audit_event(
        action=AuditAction.UPDATED, target=locked, previous_state=before, new_state=snapshot(locked)
    )
    return locked


def _jsonable(cleaned: dict[str, Any]) -> dict[str, Any]:
    """Decimals as strings, dates as ISO — the payload stays JSON and exact."""
    out: dict[str, Any] = {}
    for key, value in cleaned.items():
        if isinstance(value, Decimal):
            out[key] = format(value, "f")
        elif isinstance(value, datetime.date):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


@transaction.atomic
def apply_batch(*, batch: ImportBatch) -> ImportBatch:
    """
    Write every row, or write none.

    Refuses outright when any row is invalid — see the module docstring for why
    99-of-100 is not offered. One transaction, so a failure part way through
    leaves the master data exactly as it was.
    """
    locked = ImportBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.status == ImportBatchStatus.APPLIED:
        raise ValidationError(_("الدفعة مطبّقة مسبقاً."), code="import_already_applied")
    if locked.is_terminal:
        raise ValidationError(_("الدفعة مغلقة."), code="import_batch_terminal")
    if locked.status != ImportBatchStatus.VALIDATED:
        raise ValidationError(_("لا يمكن التطبيق قبل نجاح التدقيق."), code="import_not_validated")
    if locked.error_row_count:
        raise ValidationError(
            _("الدفعة تحتوي سطوراً غير صالحة؛ التطبيق كامل أو لا شيء."),
            code="import_has_invalid_rows",
        )

    # The partial unique index is the guarantee; this is the message. Without
    # it the second apply of the same spreadsheet reaches the database and
    # comes back as an IntegrityError, which a screen can only render as a
    # crash — and the operator's question ("did it already run?") has a good
    # answer that the crash throws away.
    earlier = (
        ImportBatch.objects.filter(
            organization=locked.organization,
            kind=locked.kind,
            content_hash=locked.content_hash,
            status=ImportBatchStatus.APPLIED,
        )
        .exclude(pk=locked.pk)
        .first()
    )
    if earlier is not None:
        raise ValidationError(
            _("نفس المحتوى مطبّق مسبقاً في الدفعة %(batch)s بتاريخ %(when)s."),
            code="import_content_already_applied",
            params={
                "batch": earlier.batch_number or earlier.public_id,
                "when": earlier.applied_at,
            },
        )

    before = snapshot(locked)
    writer = WRITERS[locked.kind]
    changed = 0
    rows = list(locked.rows.order_by("row_number"))
    for row in rows:
        cleaned = row.payload.get("__cleaned__", {})
        action = writer(cleaned, organization=locked.organization, branch=locked.branch)
        row.applied_action = action
        changed += 1 if action != "unchanged" else 0
    ImportRowResult.objects.bulk_update(rows, ["applied_action"])

    locked.status = ImportBatchStatus.APPLIED
    locked.applied_by = get_actor()
    locked.applied_at = timezone.now()
    locked.applied_row_count = changed
    locked.save(
        update_fields=["status", "applied_by", "applied_at", "applied_row_count", "updated_at"]
    )
    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        previous_state=before,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def cancel_batch(*, batch: ImportBatch, reason: str) -> ImportBatch:
    """Close a batch nobody will apply. The reason is required by constraint."""
    if not reason.strip():
        raise ValidationError(_("سبب الإلغاء مطلوب."), code="import_cancel_reason_required")
    locked = ImportBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.is_terminal:
        raise ValidationError(_("الدفعة مغلقة بالفعل."), code="import_batch_terminal")
    before = snapshot(locked)
    locked.status = ImportBatchStatus.CANCELLED
    locked.cancelled_by = get_actor()
    locked.cancelled_at = timezone.now()
    locked.reason = reason.strip()
    locked.save(update_fields=["status", "cancelled_by", "cancelled_at", "reason", "updated_at"])
    record_audit_event(
        action=AuditAction.CANCELLED,
        target=locked,
        previous_state=before,
        new_state=snapshot(locked),
    )
    return locked


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
#
# One per kind, each returning "created", "updated" or "unchanged". They go
# through the master-data services wherever one exists, so an import cannot
# reach a validation rule the screens enforce.


def _write_branch_item_setting(
    cleaned: dict[str, Any], *, organization: Organization, branch: Branch | None
) -> str:
    if branch is None:  # pragma: no cover - the kind/branch constraint prevents it
        raise ValidationError(_("هذا النوع يحتاج فرعاً."), code="import_branch_required")
    item = InventoryItem.objects.get(pk=cleaned["item_id"], organization=organization)
    existing = BranchItemSetting.objects.filter(branch=branch, item=item).first()
    point = Decimal(cleaned["reorder_point"]) if cleaned.get("reorder_point") else None
    quantity = Decimal(cleaned["reorder_quantity"]) if cleaned.get("reorder_quantity") else None
    if (
        existing is not None
        and existing.is_stocked == cleaned["is_stocked"]
        and existing.reorder_point == point
        and existing.reorder_quantity == quantity
    ):
        return "unchanged"
    set_branch_item_setting(
        branch=branch,
        item=item,
        is_stocked=cleaned["is_stocked"],
        reorder_point=point,
        reorder_quantity=quantity,
    )
    return "updated" if existing is not None else "created"


def _write_item_category(
    cleaned: dict[str, Any], *, organization: Organization, branch: Branch | None
) -> str:
    from apps.inventory.services import create_item_category, update_item_category

    parent = ItemCategory.objects.get(pk=cleaned["parent_id"]) if cleaned.get("parent_id") else None
    existing = ItemCategory.objects.filter(organization=organization, code=cleaned["code"]).first()
    if existing is None:
        create_item_category(
            organization=organization,
            code=cleaned["code"],
            name=cleaned["name"],
            parent=parent,
        )
        return "created"
    if existing.name == cleaned["name"] and existing.parent_id == (parent.pk if parent else None):
        return "unchanged"
    update_item_category(
        category=existing,
        name=cleaned["name"],
        parent=parent,
    )
    return "updated"


def _write_package_unit(
    cleaned: dict[str, Any], *, organization: Organization, branch: Branch | None
) -> str:
    from apps.inventory.services import create_package_unit, update_package_unit

    existing = PackageUnit.objects.filter(organization=organization, code=cleaned["code"]).first()
    if existing is None:
        create_package_unit(
            organization=organization,
            code=cleaned["code"],
            name=cleaned["name"],
        )
        return "created"
    if existing.name == cleaned["name"]:
        return "unchanged"
    update_package_unit(package_unit=existing, name=cleaned["name"])
    return "updated"


WRITERS: dict[str, Callable[..., str]] = {
    ImportKind.BRANCH_ITEM_SETTING: _write_branch_item_setting,
    ImportKind.ITEM_CATEGORY: _write_item_category,
    ImportKind.PACKAGE_UNIT: _write_package_unit,
}
