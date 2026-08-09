"""
Inventory master-data commands.

Every write goes through here. Models stay free of business logic, and no
caller can construct a half-valid row by assigning fields directly.

Master data is not a posted ledger, so these are ordinary create/update
services rather than the command-and-reversal shape the accounting kernel
needs. What they share with the kernel is the discipline: validate first, take
the `previous_state` from the database and not from a form-mutated instance,
and record an audit event for anything consequential.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import (
    MAX_CATEGORY_DEPTH,
    BranchItemSetting,
    ConversionType,
    InventoryItem,
    ItemCategory,
    ItemPackageConversion,
    PackageUnit,
    Warehouse,
    WarehouseType,
)
from apps.organizations.models import Branch, Organization
from apps.units.models import UnitOfMeasure


def canonical_code(value: str) -> str:
    """
    The one form a code is ever stored in.

    `strip().upper()` before validation and before storage, so uniqueness is
    case-insensitive in effect without a functional index: `rice-272`,
    `Rice-272`, and `RICE-272 ` are one code because they are one thing on one
    shelf.
    """
    return value.strip().upper()


def _require_code(value: str, *, field: str = "code") -> str:
    canonical = canonical_code(value)
    if not canonical:
        raise ValidationError(
            _("A %(field)s is required."), code="code_required", params={"field": field}
        )
    return canonical


# ---------------------------------------------------------------------------
# Item categories
# ---------------------------------------------------------------------------


def _category_depth(parent: ItemCategory | None) -> int:
    return 1 if parent is None else parent.depth + 1


def _validate_category_placement(
    *, parent: ItemCategory | None, organization: Organization, moving: ItemCategory | None = None
) -> int:
    """
    Check a category may sit under this parent, and return its new depth.

    Three things go wrong when a tree is edited rather than built: it gets too
    deep, it eats itself, and a parent quietly acquires items. The first two
    are checked here; the third is a database trigger as well, because it
    spans two tables.
    """
    if parent is None:
        return 1

    if parent.organization_id != organization.pk:
        raise ValidationError(
            _("The parent category belongs to another organization."),
            code="parent_organization_mismatch",
        )

    # The cycle check comes first on purpose. Moving a category beneath its
    # own grandchild is both a cycle and too deep, and "you cannot put this
    # inside itself" is the useful message — the depth is a symptom.
    if moving is not None:
        # Walking upward is bounded by MAX_CATEGORY_DEPTH, so this cannot run
        # away even on a corrupted tree.
        ancestor: ItemCategory | None = parent
        for _step in range(MAX_CATEGORY_DEPTH + 1):
            if ancestor is None:
                break
            if ancestor.pk == moving.pk:
                raise ValidationError(
                    _("A category cannot be moved beneath itself."),
                    code="category_cycle",
                )
            ancestor = ancestor.parent

    depth = _category_depth(parent)
    if depth > MAX_CATEGORY_DEPTH:
        raise ValidationError(
            _("A category may be at most %(limit)s levels deep."),
            code="category_too_deep",
            params={"limit": MAX_CATEGORY_DEPTH},
        )

    if moving is not None:
        deepest = _deepest_descendant_depth(moving)
        if depth + (deepest - moving.depth) > MAX_CATEGORY_DEPTH:
            raise ValidationError(
                _("Moving this category would push its children past %(limit)s levels."),
                code="category_too_deep",
                params={"limit": MAX_CATEGORY_DEPTH},
            )

    if parent.items.exists():
        raise ValidationError(
            _("Category %(code)s already holds items and cannot acquire children."),
            code="category_has_items",
            params={"code": parent.code},
        )
    return depth


def _deepest_descendant_depth(category: ItemCategory) -> int:
    deepest = category.depth
    for child in category.children.all():
        deepest = max(deepest, _deepest_descendant_depth(child))
    return deepest


def _restamp_descendants(category: ItemCategory) -> None:
    """Re-derive `depth` for a moved subtree. Bounded by MAX_CATEGORY_DEPTH."""
    for child in category.children.all():
        child.depth = category.depth + 1
        child.save(update_fields=["depth", "updated_at"])
        _restamp_descendants(child)


@transaction.atomic
def create_item_category(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    name_en: str = "",
    parent: ItemCategory | None = None,
) -> ItemCategory:
    """Add a category. Depth and parentage are validated before anything is written."""
    depth = _validate_category_placement(parent=parent, organization=organization)

    category = ItemCategory(
        organization=organization,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        parent=parent,
        depth=depth,
    )
    category.full_clean()
    category.save()
    record_audit_event(action=AuditAction.CREATED, target=category, new_state=snapshot(category))
    return category


@transaction.atomic
def update_item_category(
    *,
    category: ItemCategory,
    name_ar: str,
    name_en: str = "",
    parent: ItemCategory | None = None,
    is_active: bool = True,
) -> ItemCategory:
    """
    Rename, re-parent, or archive a category.

    The code is not editable: it appears in reports and exports, and changing
    it would silently rewrite what historic figures claim to be about.
    """
    # Re-read: a ModelForm mutates its instance during validation, so an
    # in-memory snapshot here would already hold the new values.
    before = snapshot(ItemCategory.objects.get(pk=category.pk))

    reparenting = (parent.pk if parent else None) != category.parent_id
    if reparenting:
        category.depth = _validate_category_placement(
            parent=parent, organization=category.organization, moving=category
        )
        category.parent = parent

    category.name_ar = name_ar.strip()
    category.name_en = name_en.strip()
    category.is_active = is_active
    category.full_clean()
    category.save()

    if reparenting:
        _restamp_descendants(category)

    record_audit_event(
        action=AuditAction.UPDATED if is_active else AuditAction.DEACTIVATED,
        target=category,
        previous_state=before,
        new_state=snapshot(category),
    )
    return category


# ---------------------------------------------------------------------------
# Package units
# ---------------------------------------------------------------------------


@transaction.atomic
def create_package_unit(
    *, organization: Organization, code: str, name_ar: str, name_en: str = ""
) -> PackageUnit:
    """
    Add a package unit — carton, sack, tin.

    It carries no factor. How many kilograms are in *this item's* carton is
    recorded by `create_item_conversion`, per item, because a carton of
    chicken and a carton of oil share only the word.
    """
    package_unit = PackageUnit(
        organization=organization,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
    )
    package_unit.full_clean()
    package_unit.save()
    record_audit_event(
        action=AuditAction.CREATED, target=package_unit, new_state=snapshot(package_unit)
    )
    return package_unit


@transaction.atomic
def update_package_unit(
    *, package_unit: PackageUnit, name_ar: str, name_en: str = "", is_active: bool = True
) -> PackageUnit:
    before = snapshot(PackageUnit.objects.get(pk=package_unit.pk))
    package_unit.name_ar = name_ar.strip()
    package_unit.name_en = name_en.strip()
    package_unit.is_active = is_active
    package_unit.full_clean()
    package_unit.save()
    record_audit_event(
        action=AuditAction.UPDATED if is_active else AuditAction.DEACTIVATED,
        target=package_unit,
        previous_state=before,
        new_state=snapshot(package_unit),
    )
    return package_unit


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def _validate_category_is_leaf(category: ItemCategory) -> None:
    if category.children.exists():
        raise ValidationError(
            _("Category %(code)s has child categories and cannot hold items."),
            code="category_has_children",
            params={"code": category.code},
        )


@transaction.atomic
def create_item(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    category: ItemCategory,
    item_type: str,
    base_unit: UnitOfMeasure,
    name_en: str = "",
    tracks_lots: bool = False,
    tracks_expiry: bool = False,
    shelf_life_days: int | None = None,
    notes: str = "",
) -> InventoryItem:
    """Add an item to the master."""
    if category.organization_id != organization.pk:
        raise ValidationError(
            _("The category belongs to another organization."),
            code="category_organization_mismatch",
        )
    _validate_category_is_leaf(category)

    if not base_unit.is_active:
        raise ValidationError(
            _("Unit %(code)s is archived and cannot be a base unit."),
            code="base_unit_inactive",
            params={"code": base_unit.code},
        )

    item = InventoryItem(
        organization=organization,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        category=category,
        item_type=item_type,
        base_unit=base_unit,
        tracks_lots=tracks_lots,
        tracks_expiry=tracks_expiry,
        shelf_life_days=shelf_life_days,
        notes=notes.strip(),
    )
    item.full_clean()
    item.save()
    record_audit_event(action=AuditAction.CREATED, target=item, new_state=snapshot(item))
    return item


def _item_has_movements(item: InventoryItem) -> bool:
    """
    Whether posted stock movements reference this item.

    Always False until Task 1.2 creates `StockMovement`. Isolated here so the
    guards that depend on it are written once, now, and become live the moment
    the ledger exists — rather than being remembered later.
    """
    from django.apps import apps as django_apps

    try:
        movement_model = django_apps.get_model(  # type: ignore[misc]
            "inventory", "StockMovement"
        )
    except LookupError:
        return False
    return bool(movement_model.objects.filter(item=item).exists())  # pragma: no cover


@transaction.atomic
def update_item(
    *,
    item: InventoryItem,
    name_ar: str,
    category: ItemCategory,
    item_type: str,
    name_en: str = "",
    tracks_lots: bool | None = None,
    tracks_expiry: bool | None = None,
    shelf_life_days: int | None = None,
    notes: str = "",
    is_active: bool = True,
) -> InventoryItem:
    """
    Amend an item.

    `organization`, `code`, `base_unit`, `tracks_lots`, and `costing_method`
    are immutable once posted movements exist: each changes what a stored
    quantity or value *means*, so altering one silently restates history that
    has already been reported.
    """
    before = snapshot(InventoryItem.objects.get(pk=item.pk))
    locked = _item_has_movements(item)

    if category.organization_id != item.organization_id:
        raise ValidationError(
            _("The category belongs to another organization."),
            code="category_organization_mismatch",
        )
    _validate_category_is_leaf(category)

    if tracks_lots is not None and tracks_lots != item.tracks_lots and locked:
        raise ValidationError(
            _("Lot tracking cannot change once this item has posted movements."),
            code="item_locked_by_movements",
        )

    item.name_ar = name_ar.strip()
    item.name_en = name_en.strip()
    item.category = category
    item.item_type = item_type
    if tracks_lots is not None:
        item.tracks_lots = tracks_lots
    if tracks_expiry is not None:
        item.tracks_expiry = tracks_expiry
    item.shelf_life_days = shelf_life_days
    item.notes = notes.strip()
    item.is_active = is_active
    item.full_clean()
    item.save()

    record_audit_event(
        action=AuditAction.UPDATED if is_active else AuditAction.DEACTIVATED,
        target=item,
        previous_state=before,
        new_state=snapshot(item),
    )
    return item


# ---------------------------------------------------------------------------
# Item package conversions
# ---------------------------------------------------------------------------


@transaction.atomic
def create_item_conversion(
    *,
    item: InventoryItem,
    package_unit: PackageUnit,
    factor_to_base: Decimal,
    effective_from: datetime.date,
    conversion_type: str = ConversionType.FIXED,
    effective_to: datetime.date | None = None,
    allows_fractional: bool = True,
    minimum_increment: Decimal | None = None,
    is_default_purchase_package: bool = False,
) -> ItemPackageConversion:
    """
    Record how many base units are in one package of this item.

    The factor resolves **directly** to the item's base unit. There is no
    chaining: if a carton contains 24 cans of 0.8 kg, both `CAN -> 0.8 KG` and
    `CARTON -> 24 KG` are recorded, because a carton changing from 30 cans to
    24 does not change what a can is.
    """
    if package_unit.organization_id != item.organization_id:
        raise ValidationError(
            _("The package unit belongs to another organization."),
            code="package_organization_mismatch",
        )
    if not package_unit.is_active:
        raise ValidationError(
            _("Package unit %(code)s is archived."),
            code="package_unit_inactive",
            params={"code": package_unit.code},
        )
    if factor_to_base <= 0:
        raise ValidationError(
            _("A conversion factor must be greater than zero."),
            code="factor_not_positive",
        )

    if is_default_purchase_package:
        ItemPackageConversion.objects.filter(
            item=item, is_default_purchase_package=True, is_active=True
        ).update(is_default_purchase_package=False)

    latest = (
        ItemPackageConversion.objects.filter(item=item, package_unit=package_unit)
        .order_by("-version")
        .first()
    )
    conversion = ItemPackageConversion(
        organization=item.organization,
        item=item,
        package_unit=package_unit,
        conversion_type=conversion_type,
        factor_to_base=factor_to_base,
        effective_from=effective_from,
        effective_to=effective_to,
        allows_fractional=allows_fractional,
        minimum_increment=minimum_increment,
        is_default_purchase_package=is_default_purchase_package,
        version=(latest.version + 1) if latest else 1,
    )
    conversion.full_clean()
    conversion.save()
    record_audit_event(
        action=AuditAction.CREATED, target=conversion, new_state=snapshot(conversion)
    )
    return conversion


@transaction.atomic
def supersede_item_conversion(
    *,
    conversion: ItemPackageConversion,
    factor_to_base: Decimal,
    effective_from: datetime.date,
    reason: str = "",
) -> ItemPackageConversion:
    """
    Close a conversion and open its successor.

    A supplier changing a 30 kg sack to 25 kg is a **new packaging fact**, not
    a correction of the old one. Editing the factor in place would silently
    restate every movement posted under the old packaging, so the old row is
    closed the day before the new one starts and both remain readable.
    """
    if effective_from <= conversion.effective_from:
        raise ValidationError(
            _("The new factor must start after the one it replaces."),
            code="supersede_not_later",
        )

    before = snapshot(ItemPackageConversion.objects.get(pk=conversion.pk))
    conversion.effective_to = effective_from - datetime.timedelta(days=1)
    conversion.is_default_purchase_package = False
    conversion.save(update_fields=["effective_to", "is_default_purchase_package", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=conversion,
        previous_state=before,
        new_state=snapshot(conversion),
        reason=reason or "superseded",
    )

    return create_item_conversion(
        item=conversion.item,
        package_unit=conversion.package_unit,
        factor_to_base=factor_to_base,
        effective_from=effective_from,
        conversion_type=conversion.conversion_type,
        allows_fractional=conversion.allows_fractional,
        minimum_increment=conversion.minimum_increment,
        is_default_purchase_package=True,
    )


# ---------------------------------------------------------------------------
# Branch item settings
# ---------------------------------------------------------------------------


@transaction.atomic
def set_branch_item_setting(
    *,
    branch: Branch,
    item: InventoryItem,
    is_stocked: bool = True,
    reorder_point: Decimal | None = None,
    reorder_quantity: Decimal | None = None,
    notes: str = "",
) -> BranchItemSetting:
    """
    Say whether a branch stocks an item, and at what levels it reorders.

    Controls pickers and reorder reports only. It never blocks an incoming
    transfer: stock arriving at a branch that has not configured the item is
    an operational fact, and refusing it would strand goods in transit.
    """
    if branch.organization_id != item.organization_id:
        raise ValidationError(
            _("The item belongs to another organization."),
            code="item_organization_mismatch",
        )

    setting, created = BranchItemSetting.objects.get_or_create(
        branch=branch,
        item=item,
        defaults={
            "is_stocked": is_stocked,
            "reorder_point": reorder_point,
            "reorder_quantity": reorder_quantity,
            "notes": notes.strip(),
        },
    )
    before = None
    if not created:
        before = snapshot(BranchItemSetting.objects.get(pk=setting.pk))
        setting.is_stocked = is_stocked
        setting.reorder_point = reorder_point
        setting.reorder_quantity = reorder_quantity
        setting.notes = notes.strip()
        setting.full_clean()
        setting.save()

    record_audit_event(
        action=AuditAction.CREATED if created else AuditAction.UPDATED,
        target=setting,
        branch=branch,
        previous_state=before,
        new_state=snapshot(setting),
    )
    return setting


# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------


@transaction.atomic
def create_warehouse(
    *,
    branch: Branch,
    code: str,
    name_ar: str,
    name_en: str = "",
    warehouse_type: str = WarehouseType.PHYSICAL,
) -> Warehouse:
    """
    Add a warehouse to a branch.

    `IN_TRANSIT` cannot be created this way. It is a system warehouse, made by
    `ensure_in_transit_warehouse`, because a user-creatable system warehouse
    would be a way to make an ordinary store exempt from the rules that
    protect the ledger.
    """
    if warehouse_type == WarehouseType.IN_TRANSIT:
        raise ValidationError(
            _("An in-transit warehouse is created by the system, not by hand."),
            code="system_warehouse_not_user_creatable",
        )

    warehouse = Warehouse(
        branch=branch,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        warehouse_type=warehouse_type,
        is_system=False,
    )
    warehouse.full_clean()
    warehouse.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=warehouse,
        branch=branch,
        new_state=snapshot(warehouse),
    )
    return warehouse


@transaction.atomic
def update_warehouse(
    *, warehouse: Warehouse, name_ar: str, name_en: str = "", is_active: bool = True
) -> Warehouse:
    """
    Rename or archive a warehouse.

    A system warehouse is refused: renaming in-transit stock's home, or
    archiving it, would break transfers that are mid-flight.
    """
    if warehouse.is_system:
        raise ValidationError(
            _("Warehouse %(code)s is system-controlled and cannot be changed."),
            code="system_warehouse_protected",
            params={"code": warehouse.code},
        )

    before = snapshot(Warehouse.objects.get(pk=warehouse.pk))
    warehouse.name_ar = name_ar.strip()
    warehouse.name_en = name_en.strip()
    warehouse.is_active = is_active
    warehouse.full_clean()
    warehouse.save()
    record_audit_event(
        action=AuditAction.UPDATED if is_active else AuditAction.DEACTIVATED,
        target=warehouse,
        branch=warehouse.branch,
        previous_state=before,
        new_state=snapshot(warehouse),
    )
    return warehouse


@transaction.atomic
def ensure_in_transit_warehouse(*, branch: Branch) -> Warehouse:
    """
    The branch's in-transit warehouse, created once.

    Stock that has left one place and not arrived at another has to sit
    somewhere, and it stays on the dispatching branch's books until received —
    which is both the accounting truth and the answer to "who is responsible
    for it right now".
    """
    existing = Warehouse.objects.filter(
        branch=branch, warehouse_type=WarehouseType.IN_TRANSIT
    ).first()
    if existing is not None:
        return existing

    warehouse = Warehouse(
        branch=branch,
        code="IN-TRANSIT",
        name_ar="بضاعة بالطريق",
        name_en="In Transit",
        warehouse_type=WarehouseType.IN_TRANSIT,
        is_system=True,
    )
    warehouse.full_clean()
    warehouse.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=warehouse,
        branch=branch,
        new_state=snapshot(warehouse),
        reason="system in-transit warehouse",
    )
    return warehouse
