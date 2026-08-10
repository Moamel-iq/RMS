"""
Inventory master-data reads. Nothing here writes.

Every selector starts from the caller, not from an identifier. A function that
took an id and returned an object would have to be checked by whoever called
it, and the first caller to forget is the leak — so out-of-scope objects are
simply not in the queryset.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.inventory.models import (
    BranchItemSetting,
    InventoryItem,
    ItemCategory,
    ItemPackageConversion,
    PackageUnit,
    Warehouse,
)
from apps.organizations.authorization import OutOfScope, accessible_warehouses
from apps.organizations.models import Organization
from apps.organizations.selectors import accessible_branches
from apps.users.models import User


def reachable_organization_ids(user: User) -> list[int]:
    """
    Organizations this caller reaches — through organization authority or any
    branch of theirs.

    The same definition `apps/organizations/authorization.resolve_organization`
    uses, expressed as a list so it can filter a queryset in one round trip.
    """
    from apps.organizations.authorization import organization_scope

    reachable = set(organization_scope(user))
    reachable.update(accessible_branches(user).values_list("organization_id", flat=True))
    return list(reachable)


def visible_categories(user: User) -> QuerySet[ItemCategory]:
    return ItemCategory.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization", "parent")


def visible_package_units(user: User) -> QuerySet[PackageUnit]:
    return PackageUnit.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization")


def visible_items(user: User) -> QuerySet[InventoryItem]:
    return InventoryItem.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization", "category", "base_unit")


def visible_conversions(user: User) -> QuerySet[ItemPackageConversion]:
    return ItemPackageConversion.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("item", "package_unit")


def visible_warehouses(user: User) -> QuerySet[Warehouse]:
    """
    Warehouses the caller may act on — `ALL` or `SELECTED` scope respected.

    Delegates to `accessible_warehouses` rather than filtering by branch:
    warehouse scope narrows branch access, and a second implementation here
    would eventually disagree with the first.
    """
    return accessible_warehouses(user).select_related("branch", "branch__organization")


def readable_warehouses(user: User) -> QuerySet[Warehouse]:
    """
    Every warehouse in the caller's branches — **archived ones included**.

    Deliberately different from `visible_warehouses`, which delegates to
    `accessible_warehouses` and answers custody: where stock may actually
    move. Custody is only ever about active warehouses.

    A management screen needs the other answer. An archived warehouse still
    holds its code, its history is still referenced by everything posted
    against it, and a screen that hid it would make a reserved code look free
    and leave no way to reactivate what was archived by mistake.

    Scoped by branch, matching `manage_warehouses`, which is branch-scoped.
    Warehouse-level narrowing restricts custody, not the right to read the
    branch's own structure.
    """
    return Warehouse.objects.filter(branch__in=accessible_branches(user)).select_related(
        "branch", "branch__organization"
    )


def resolve_manageable_warehouse(user: User, warehouse_id: int) -> Warehouse:
    """
    Turn a submitted warehouse id into one the caller may manage.

    Reads from `readable_warehouses`, so an archived warehouse can still be
    reached and reactivated. Whether the caller may *change* it is a separate
    question, answered against the branch by `require_branch_permission`.
    """
    warehouse = readable_warehouses(user).filter(pk=warehouse_id).first()
    if warehouse is None:
        raise OutOfScope(_("Warehouse %(id)s does not exist.") % {"id": warehouse_id})
    return warehouse


def visible_branch_item_settings(user: User) -> QuerySet[BranchItemSetting]:
    return BranchItemSetting.objects.filter(branch__in=accessible_branches(user)).select_related(
        "branch", "item"
    )


# ---------------------------------------------------------------------------
# Scoped resolution — an id becomes an object only if the caller reaches it
# ---------------------------------------------------------------------------


def resolve_category(user: User, category_id: int) -> ItemCategory:
    category = visible_categories(user).filter(pk=category_id).first()
    if category is None:
        raise OutOfScope(_("Category %(id)s does not exist.") % {"id": category_id})
    return category


def resolve_package_unit(user: User, package_unit_id: int) -> PackageUnit:
    package_unit = visible_package_units(user).filter(pk=package_unit_id).first()
    if package_unit is None:
        raise OutOfScope(_("Package unit %(id)s does not exist.") % {"id": package_unit_id})
    return package_unit


def resolve_item(user: User, item_id: int) -> InventoryItem:
    item = visible_items(user).filter(pk=item_id).first()
    if item is None:
        raise OutOfScope(_("Item %(id)s does not exist.") % {"id": item_id})
    return item


def resolve_conversion(user: User, conversion_id: int) -> ItemPackageConversion:
    conversion = visible_conversions(user).filter(pk=conversion_id).first()
    if conversion is None:
        raise OutOfScope(_("Conversion %(id)s does not exist.") % {"id": conversion_id})
    return conversion


def items_for_organization(organization: Organization) -> QuerySet[InventoryItem]:
    """Unscoped by user — for management commands and reports that already scoped."""
    return InventoryItem.objects.filter(organization=organization)


def effective_conversion(
    *, item: InventoryItem, package_unit: PackageUnit, on_date: object
) -> ItemPackageConversion | None:
    """
    The conversion in force for this item and package on a given date.

    Resolved by effective date, not by "the latest row": a movement backdated
    to a period when a sack held 30 kg must convert at 30, whatever the sack
    holds today.
    """
    open_ended_or_still_current = Q(effective_to__isnull=True) | Q(effective_to__gte=on_date)
    return (
        ItemPackageConversion.objects.filter(
            item=item,
            package_unit=package_unit,
            is_active=True,
            effective_from__lte=on_date,
        )
        .filter(open_ended_or_still_current)
        .order_by("-effective_from")
        .first()
    )
