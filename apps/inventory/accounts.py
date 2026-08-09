"""
Inventory account resolution and the item/category mapping overrides.

The dependency direction is the whole architecture (ADR-019): this module
imports accounting; accounting never imports this. `AccountRole` and the
organization defaults live in `apps.accounting`; the overrides live here
because they name `InventoryItem` and `ItemCategory`.

## Resolver precedence

    1. exact item mapping
    2. nearest matching category ancestor, leaf towards root
    3. the organization default (`accounting.resolve_default_account`)
    4. `account_role_unmapped` — never a guess

Resolution happens **at posting** and is snapshotted onto the document line.
Nothing ever re-resolves a posted effect through today's mapping; that is what
keeps reconciliation honest after the chart changes.

## The reclassification guard

An `INVENTORY_CONTROL` mapping change must not silently strand standing stock
value in an old GL account: the balance would still say account A while every
new posting said account B, and no journal ever moved the money. Until an
explicit GL reclassification workflow exists, any mapping change — here or on
the organization default — that would alter the resolved control account of an
item with non-zero stock is refused with
`inventory_account_reclassification_required`. The organization-default half
is enforced through the guard hook accounting exposes, so the rule holds no
matter which screen made the change.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounting.models import (
    INVENTORY_CONTROL,
    Account,
    AccountRole,
    AccountRoleDomain,
    AccountRoleMappingScope,
    OrganizationAccountMapping,
)
from apps.accounting.services import mapping_is_used, resolve_default_account
from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import (
    InventoryAccountMapping,
    InventoryItem,
    ItemCategory,
    StockBalance,
)
from apps.organizations.models import Organization


@dataclass(frozen=True)
class ResolvedAccount:
    """One resolution, with the row that produced it — for the snapshot."""

    role: AccountRole
    account: Account
    #: Set when an item- or category-specific override answered.
    inventory_mapping: InventoryAccountMapping | None
    #: Set when the organization default answered.
    organization_mapping: OrganizationAccountMapping | None


def category_ancestry(category: ItemCategory) -> list[ItemCategory]:
    """The category and its ancestors, leaf first. Bounded: depth is <= 3."""
    chain: list[ItemCategory] = []
    current: ItemCategory | None = category
    while current is not None:
        chain.append(current)
        current = current.parent
    return chain


def _covers(on_date: datetime.date) -> Q:
    """Rows whose effective range includes the date (paired with a `__lte` filter)."""
    return Q(effective_to__isnull=True) | Q(effective_to__gte=on_date)


def _override_for(
    *,
    organization: Organization,
    role: AccountRole,
    item: InventoryItem,
    on_date: datetime.date,
) -> InventoryAccountMapping | None:
    """The most specific active override covering the date, or None."""
    base = InventoryAccountMapping.objects.filter(
        organization=organization,
        account_role=role,
        is_active=True,
        effective_from__lte=on_date,
    ).filter(_covers(on_date))

    exact = base.filter(item=item).select_related("account").order_by("-version").first()
    if exact is not None:
        return exact

    for category in category_ancestry(item.category):
        nearest = (
            base.filter(category=category).select_related("account").order_by("-version").first()
        )
        if nearest is not None:
            return nearest
    return None


def _role(role: AccountRole | str) -> AccountRole:
    if isinstance(role, AccountRole):
        return role
    found = AccountRole.objects.filter(code=role, is_active=True).first()
    if found is None:
        raise ValidationError(
            _("%(code)s is not an active account role."),
            code="account_role_unmapped",
            params={"code": role},
        )
    return found


def resolve_inventory_account(
    *,
    organization: Organization,
    role: AccountRole | str,
    item: InventoryItem | None,
    on_date: datetime.date,
) -> ResolvedAccount:
    """
    Resolve which account carries a role for an item on a date.

    `item` is None for roles that have no per-item meaning (opening equity);
    those resolve straight from the organization default. The account is
    checked postable and active **at use time** — a mapping made valid last
    year does not excuse posting into an account archived since.
    """
    resolved_role = _role(role)

    override: InventoryAccountMapping | None = None
    if item is not None and resolved_role.mapping_scope == AccountRoleMappingScope.ITEM:
        override = _override_for(
            organization=organization, role=resolved_role, item=item, on_date=on_date
        )

    if override is not None:
        account = override.account
        organization_mapping = None
    else:
        organization_mapping = resolve_default_account(
            organization=organization, account_role=resolved_role, on_date=on_date
        )
        account = organization_mapping.account

    if not account.is_active:
        raise ValidationError(
            _("Account %(code)s for role %(role)s is archived."),
            code="account_inactive",
            params={"code": account.code, "role": resolved_role.code},
        )
    if not account.is_postable:  # pragma: no cover - refused at mapping time as well
        raise ValidationError(
            _("Account %(code)s for role %(role)s is not postable."),
            code="account_not_postable",
            params={"code": account.code, "role": resolved_role.code},
        )
    return ResolvedAccount(
        role=resolved_role,
        account=account,
        inventory_mapping=override,
        organization_mapping=organization_mapping,
    )


# ---------------------------------------------------------------------------
# The reclassification guard (§G)
# ---------------------------------------------------------------------------


def _no_verify() -> None:
    """The verifier for roles that carry no standing value: nothing to protect."""


def _items_with_stock(organization: Organization) -> list[InventoryItem]:
    """Items whose standing stock the control account currently carries."""
    item_ids = (
        StockBalance.objects.filter(organization=organization)
        .exclude(quantity=0)
        .values_list("item_id", flat=True)
        .distinct()
    )
    return list(
        InventoryItem.objects.filter(pk__in=item_ids).select_related("category", "category__parent")
    )


def _control_account_id(
    *, organization: Organization, item: InventoryItem, on_date: datetime.date
) -> int | None:
    """The resolved control account for one item, or None while unmapped."""
    try:
        return resolve_inventory_account(
            organization=organization, role=INVENTORY_CONTROL, item=item, on_date=on_date
        ).account.pk
    except ValidationError:
        return None


def control_stability_verifier(
    *, organization: Organization, items: list[InventoryItem]
) -> Callable[[], None]:
    """
    Capture the control resolution for these items now; return a verifier that
    re-resolves after a mutation and refuses any difference.

    Run inside the mutating transaction, so a refusal rolls the change back.
    Compared **as of today**: today's resolution is the account the standing
    value currently reconciles against, and that is the figure a silent change
    would strand.
    """
    today = timezone.localdate()
    before = {
        item.pk: _control_account_id(organization=organization, item=item, on_date=today)
        for item in items
    }

    def verify() -> None:
        for item in items:
            after = _control_account_id(organization=organization, item=item, on_date=today)
            if after != before[item.pk]:
                raise ValidationError(
                    _(
                        "Item %(item)s holds stock whose inventory-control account would "
                        "change. Reclassifying standing stock value needs an explicit GL "
                        "reclassification, which does not exist yet."
                    ),
                    code="inventory_account_reclassification_required",
                    params={"item": item.code},
                )

    return verify


def organization_mapping_guard(
    organization: Organization, account_role: AccountRole
) -> Callable[[], None]:
    """
    The guard inventory registers with accounting at app-ready.

    Only `INVENTORY_CONTROL` carries standing value, so only it is guarded;
    every other role's mapping may change freely.
    """
    if account_role.code != INVENTORY_CONTROL:
        return lambda: None
    return control_stability_verifier(
        organization=organization, items=_items_with_stock(organization)
    )


def item_category_change_verifier(*, item: InventoryItem) -> Callable[[], None]:
    """
    Guard an item's move between categories, for `update_item`.

    Captures the control resolution before the move; the returned verifier
    re-reads the item and re-resolves, so it judges what was actually saved.
    Items without standing stock move freely — there is no value to strand.
    """
    has_stock = StockBalance.objects.filter(item=item).exclude(quantity=0).exists()
    if not has_stock:
        return _no_verify

    today = timezone.localdate()
    before = _control_account_id(organization=item.organization, item=item, on_date=today)

    def verify() -> None:
        moved = InventoryItem.objects.select_related(
            "organization", "category", "category__parent", "category__parent__parent"
        ).get(pk=item.pk)
        after = _control_account_id(organization=moved.organization, item=moved, on_date=today)
        if after != before:
            raise ValidationError(
                _(
                    "Item %(item)s holds stock whose inventory-control account would "
                    "change. Reclassifying standing stock value needs an explicit GL "
                    "reclassification, which does not exist yet."
                ),
                code="inventory_account_reclassification_required",
                params={"item": item.code},
            )

    return verify


def _affected_items(mapping: InventoryAccountMapping) -> list[InventoryItem]:
    """The stocked items a specific override could re-home."""
    stocked = _items_with_stock(mapping.organization)
    if mapping.item_id is not None:
        return [item for item in stocked if item.pk == mapping.item_id]
    affected_category_ids = {mapping.category_id}
    # Descendants too: an override on Food re-homes items filed under Meat.
    children = ItemCategory.objects.filter(parent_id=mapping.category_id).values_list(
        "id", flat=True
    )
    affected_category_ids.update(children)
    grandchildren = ItemCategory.objects.filter(parent_id__in=children).values_list("id", flat=True)
    affected_category_ids.update(grandchildren)
    return [item for item in stocked if item.category_id in affected_category_ids]


# ---------------------------------------------------------------------------
# Override services
# ---------------------------------------------------------------------------


def _validate_override_shape(
    *,
    organization: Organization,
    role: AccountRole,
    account: Account,
    item: InventoryItem | None,
    category: ItemCategory | None,
) -> None:
    if (item is None) == (category is None):
        raise ValidationError(
            _("An override targets exactly one of an item or a category."),
            code="override_target_required",
        )
    if role.domain != AccountRoleDomain.INVENTORY:
        raise ValidationError(
            _("Role %(code)s does not belong to the inventory domain."),
            code="role_not_inventory",
            params={"code": role.code},
        )
    if role.mapping_scope != AccountRoleMappingScope.ITEM:
        raise ValidationError(
            _("Role %(code)s takes only an organization default, not item overrides."),
            code="role_not_overridable",
            params={"code": role.code},
        )
    if not role.is_active:
        raise ValidationError(
            _("Role %(code)s is not active."),
            code="account_role_inactive",
            params={"code": role.code},
        )
    if account.organization_id != organization.pk:
        raise ValidationError(
            _("Account %(code)s belongs to another organization."),
            code="account_organization_mismatch",
            params={"code": account.code},
        )
    if not account.is_postable:
        raise ValidationError(
            _("Account %(code)s is a group account and cannot carry a role mapping."),
            code="account_not_postable",
            params={"code": account.code},
        )
    if not account.is_active:
        raise ValidationError(
            _("Account %(code)s is archived."),
            code="account_inactive",
            params={"code": account.code},
        )
    if item is not None and item.organization_id != organization.pk:
        raise ValidationError(
            _("Item %(code)s belongs to another organization."),
            code="item_organization_mismatch",
            params={"code": item.code},
        )
    if category is not None and category.organization_id != organization.pk:
        raise ValidationError(
            _("Category %(code)s belongs to another organization."),
            code="category_organization_mismatch",
            params={"code": category.code},
        )


def _validate_no_override_overlap(
    *,
    organization: Organization,
    role: AccountRole,
    item: InventoryItem | None,
    category: ItemCategory | None,
    effective_from: datetime.date,
    effective_to: datetime.date | None,
    exclude_pk: int | None = None,
) -> None:
    existing = InventoryAccountMapping.objects.filter(
        organization=organization, account_role=role, item=item, category=category, is_active=True
    )
    if exclude_pk is not None:
        existing = existing.exclude(pk=exclude_pk)
    for row in existing:
        starts_before_end = row.effective_to is None or effective_from <= row.effective_to
        ends_after_start = effective_to is None or effective_to >= row.effective_from
        if starts_before_end and ends_after_start:
            raise ValidationError(
                _("%(role)s already has an override for this target in that period."),
                code="mapping_period_overlaps",
                params={"role": role.code},
            )


@transaction.atomic
def create_inventory_mapping(
    *,
    organization: Organization,
    role: AccountRole | str,
    account: Account,
    item: InventoryItem | None = None,
    category: ItemCategory | None = None,
    effective_from: datetime.date,
    effective_to: datetime.date | None = None,
) -> InventoryAccountMapping:
    """Record an item- or category-specific override of the organization default."""
    resolved_role = _role(role)
    _validate_override_shape(
        organization=organization,
        role=resolved_role,
        account=account,
        item=item,
        category=category,
    )
    if effective_to is not None and effective_to < effective_from:
        raise ValidationError(
            _("The effective range ends before it starts."), code="period_inverted"
        )
    _validate_no_override_overlap(
        organization=organization,
        role=resolved_role,
        item=item,
        category=category,
        effective_from=effective_from,
        effective_to=effective_to,
    )

    latest = (
        InventoryAccountMapping.objects.select_for_update()
        .filter(organization=organization, account_role=resolved_role, item=item, category=category)
        .order_by("-version")
        .first()
    )
    mapping = InventoryAccountMapping(
        organization=organization,
        account_role=resolved_role,
        account=account,
        item=item,
        category=category,
        effective_from=effective_from,
        effective_to=effective_to,
        version=(latest.version + 1) if latest is not None else 1,
    )
    mapping.full_clean()
    mapping.save()

    if resolved_role.code == INVENTORY_CONTROL:
        _control_change_verifier(mapping)()

    record_audit_event(action=AuditAction.CREATED, target=mapping, new_state=snapshot(mapping))
    return mapping


def _control_change_verifier(mapping: InventoryAccountMapping) -> Callable[[], None]:
    """
    Refuse a just-saved override that re-homed standing stock.

    The organization-default guard captures "before" ahead of the change; here
    the new row already exists, so "before" is computed by resolving with the
    row excluded and "after" by resolving normally. Both reads happen inside
    the mutating transaction, so raising rolls the row back out.
    """
    today = timezone.localdate()
    items = _affected_items(mapping)

    def account_id_without_row(item: InventoryItem) -> int | None:
        base = (
            InventoryAccountMapping.objects.exclude(pk=mapping.pk)
            .filter(
                organization=mapping.organization,
                account_role=mapping.account_role,
                is_active=True,
                effective_from__lte=today,
            )
            .filter(_covers(today))
        )
        override = base.filter(item=item).order_by("-version").first()
        if override is None:
            for node in category_ancestry(item.category):
                override = base.filter(category=node).order_by("-version").first()
                if override is not None:
                    break
        if override is not None:
            return int(override.account_id)
        try:
            return int(
                resolve_default_account(
                    organization=mapping.organization,
                    account_role=mapping.account_role,
                    on_date=today,
                ).account_id
            )
        except ValidationError:
            return None

    def verify() -> None:
        for item in items:
            without = account_id_without_row(item)
            with_row = _control_account_id(
                organization=mapping.organization, item=item, on_date=today
            )
            if without != with_row:
                raise ValidationError(
                    _(
                        "Item %(item)s holds stock whose inventory-control account would "
                        "change. Reclassifying standing stock value needs an explicit GL "
                        "reclassification, which does not exist yet."
                    ),
                    code="inventory_account_reclassification_required",
                    params={"item": item.code},
                )

    return verify


@transaction.atomic
def close_inventory_mapping(
    *, mapping: InventoryAccountMapping, effective_to: datetime.date, reason: str = ""
) -> InventoryAccountMapping:
    """End an override's effective range — permitted even when used."""
    locked = InventoryAccountMapping.objects.select_for_update().get(pk=mapping.pk)
    if effective_to < locked.effective_from:
        raise ValidationError(
            _("The effective range ends before it starts."), code="period_inverted"
        )
    before = snapshot(locked)
    if locked.account_role.code == INVENTORY_CONTROL:
        verifier = control_stability_verifier(
            organization=locked.organization, items=_affected_items(locked)
        )
    else:
        verifier = _no_verify
    locked.effective_to = effective_to
    locked.full_clean()
    locked.save(update_fields=["effective_to", "updated_at"])
    verifier()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        previous_state=before,
        new_state=snapshot(locked),
        reason=reason,
    )
    return locked


@transaction.atomic
def archive_inventory_mapping(
    *, mapping: InventoryAccountMapping, reason: str = ""
) -> InventoryAccountMapping:
    """Withdraw an override recorded in error and never used."""
    locked = InventoryAccountMapping.objects.select_for_update().get(pk=mapping.pk)
    if mapping_is_used(locked):
        raise ValidationError(
            _("Mapping v%(version)s has been used by postings. Close it instead."),
            code="mapping_in_use",
            params={"version": locked.version},
        )
    before = snapshot(locked)
    if locked.account_role.code == INVENTORY_CONTROL:
        verifier = control_stability_verifier(
            organization=locked.organization, items=_affected_items(locked)
        )
    else:
        verifier = _no_verify
    locked.is_active = False
    locked.save(update_fields=["is_active", "updated_at"])
    verifier()
    record_audit_event(
        action=AuditAction.DEACTIVATED,
        target=locked,
        previous_state=before,
        new_state=snapshot(locked),
        reason=reason,
    )
    return locked
