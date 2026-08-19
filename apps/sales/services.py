"""
Sales master-data writes.

Every mutation goes through a function here rather than through a form's
`save()` or an admin action, for the reason every other module does it: the
validation, the audit event and the transaction boundary have to be in one
place, and a second write path is a second set of rules that will eventually
disagree with the first.

Checkpoint 1 covers the menu, its branch settings, its prices and the channels.
The documents that *move money* — a sales day, an adjustment, a settlement, a
cashier shift — arrive with their own checkpoints and their own services.
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.sales.models import (
    CODE_PATTERN,
    FulfillmentSource,
    MenuCategory,
    MenuItem,
    MenuItemBranchSetting,
    MenuPriceVersion,
    PriceScope,
    SalesChannel,
    SalesChannelCategory,
    TenderDestination,
)

if TYPE_CHECKING:
    from apps.accounting.models import Account, CostCenter
    from apps.kitchen.models import Recipe
    from apps.organizations.models import Branch, Organization

_CODE_RE = re.compile(CODE_PATTERN)


def _require_code(code: str) -> str:
    """
    Canonicalise a code, or refuse it.

    Uppercased before the pattern check, so `dine-in` and `DINE-IN` are the
    same code rather than two — which is what makes the uniqueness constraint
    mean what a reader assumes it means.
    """
    cleaned = code.strip().upper()
    if not cleaned or not _CODE_RE.match(cleaned):
        raise ValidationError(
            _("%(code)s is not a valid code.") % {"code": code}, code="invalid_code"
        )
    return cleaned


# ---------------------------------------------------------------------------
# Menu categories
# ---------------------------------------------------------------------------


@transaction.atomic
def create_menu_category(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    name_en: str = "",
    display_order: int = 1,
) -> MenuCategory:
    category = MenuCategory(
        organization=organization,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        display_order=display_order,
    )
    category.full_clean()
    category.save()
    record_audit_event(action=AuditAction.CREATED, target=category, new_state=snapshot(category))
    return category


@transaction.atomic
def update_menu_category(
    *,
    category: MenuCategory,
    name_ar: str,
    name_en: str = "",
    display_order: int = 1,
    is_active: bool = True,
) -> MenuCategory:
    """
    Correct a category, or archive one.

    The code and the organization are absent from the signature on purpose: a
    code is what reports group by, and re-homing a category would move a slice
    of the menu across an organization boundary.
    """
    previous = snapshot(category)
    category.name_ar = name_ar.strip()
    category.name_en = name_en.strip()
    category.display_order = display_order
    category.is_active = is_active
    category.full_clean()
    category.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=category,
        previous_state=previous,
        new_state=snapshot(category),
    )
    return category


# ---------------------------------------------------------------------------
# Menu items
# ---------------------------------------------------------------------------


def _require_recipe_serving(recipe: Recipe | None, serving_code: str) -> str:
    """
    A recipe-served item needs a recipe and a serving code that some version
    of that recipe actually offers.

    Checked against **every** version rather than against the one in force
    today, and the difference matters: an item being set up for a recipe whose
    new version starts next Sunday is a legitimate thing to configure, and
    demanding a currently-effective serving would refuse it. What is refused
    here is a code no version has *ever* carried, which is a typo rather than a
    schedule.

    The narrower question — is there a serving with this code on the version in
    force on *this business date* — belongs to the sale, and the sale asks it.
    """
    cleaned = serving_code.strip().upper()
    if recipe is None or not cleaned:
        raise ValidationError(
            _("A recipe-served menu item needs a recipe and a serving code."),
            code="recipe_serving_required",
        )
    from apps.kitchen.models import RecipeServing

    exists = RecipeServing.objects.filter(version__recipe=recipe, code=cleaned).exists()
    if not exists:
        raise ValidationError(
            _("No version of %(recipe)s offers a serving coded %(code)s.")
            % {"recipe": recipe.code, "code": cleaned},
            code="unknown_serving_code",
        )
    return cleaned


@transaction.atomic
def create_menu_item(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    recipe: Recipe | None = None,
    serving_code: str = "",
    category: MenuCategory | None = None,
    name_en: str = "",
    description_ar: str = "",
    fulfillment_source: str = FulfillmentSource.RECIPE_SERVING,
    display_order: int = 1,
    notes: str = "",
) -> MenuItem:
    """
    Add something sellable to the menu.

    `DIRECT_STOCK` is refused here **and** by a check constraint, so the
    refusal survives a shell session and a CSV import. Release 1 has no
    certified sales-and-COGS route out of a warehouse, and improvising one
    would open a second stock-consumption path beside production (ADR-027 §10).
    """
    if fulfillment_source != FulfillmentSource.RECIPE_SERVING:
        raise ValidationError(
            _(
                "Release 1 sells recipe servings only. Direct stock sales need an "
                "approved cost-of-sales route that does not exist yet."
            ),
            code="direct_stock_deferred",
        )
    if recipe is not None and recipe.organization_id != organization.pk:
        raise ValidationError(
            _("A menu item and its recipe must belong to the same organization."),
            code="recipe_organization_mismatch",
        )

    item = MenuItem(
        organization=organization,
        category=category,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        description_ar=description_ar.strip(),
        fulfillment_source=fulfillment_source,
        recipe=recipe,
        serving_code=_require_recipe_serving(recipe, serving_code),
        display_order=display_order,
        notes=notes.strip(),
    )
    item.full_clean()
    item.save()
    record_audit_event(action=AuditAction.CREATED, target=item, new_state=snapshot(item))
    return item


@transaction.atomic
def update_menu_item(
    *,
    item: MenuItem,
    name_ar: str,
    recipe: Recipe | None = None,
    serving_code: str = "",
    category: MenuCategory | None = None,
    name_en: str = "",
    description_ar: str = "",
    display_order: int = 1,
    notes: str = "",
    is_active: bool = True,
) -> MenuItem:
    """
    Correct a menu item, or archive and reactivate one.

    Changing the recipe or the serving is permitted and does **not** restate
    anything already sold: every posted sales line carries its own snapshot of
    the recipe, the version, the serving and the price it used, so the history
    is unaffected by definition (ADR-027 §4). What changes here is what the
    *next* sale resolves.
    """
    if recipe is not None and recipe.organization_id != item.organization_id:
        raise ValidationError(
            _("A menu item and its recipe must belong to the same organization."),
            code="recipe_organization_mismatch",
        )
    previous = snapshot(item)
    item.category = category
    item.name_ar = name_ar.strip()
    item.name_en = name_en.strip()
    item.description_ar = description_ar.strip()
    item.recipe = recipe
    item.serving_code = _require_recipe_serving(recipe, serving_code)
    item.display_order = display_order
    item.notes = notes.strip()
    item.is_active = is_active
    item.full_clean()
    item.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=item,
        previous_state=previous,
        new_state=snapshot(item),
    )
    return item


@transaction.atomic
def set_branch_availability(
    *,
    item: MenuItem,
    branch: Branch,
    is_available: bool = True,
    local_name_ar: str = "",
    notes: str = "",
) -> MenuItemBranchSetting:
    """
    Say whether one branch sells one item.

    Creates the row if it is missing, which is the act that turns "never sold
    here" into "sold here". Removing the offer entirely is
    `remove_branch_availability`; setting `is_available=False` is the softer
    statement that it is temporarily off.
    """
    if branch.organization_id != item.organization_id:
        raise ValidationError(
            _("A menu item can only be offered at its own organization's branches."),
            code="branch_organization_mismatch",
        )
    setting = MenuItemBranchSetting.objects.filter(menu_item=item, branch=branch).first()
    previous = snapshot(setting) if setting is not None else None
    if setting is None:
        setting = MenuItemBranchSetting(menu_item=item, branch=branch)
    setting.is_available = is_available
    setting.local_name_ar = local_name_ar.strip()
    setting.notes = notes.strip()
    setting.full_clean()
    setting.save()
    record_audit_event(
        action=AuditAction.CREATED if previous is None else AuditAction.UPDATED,
        target=setting,
        previous_state=previous,
        new_state=snapshot(setting),
        branch=branch,
    )
    return setting


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def _tender_for(category: str, requested: str) -> str:
    """
    The tender a channel category permits.

    An application channel settles and never counts, so its tender is fixed;
    everything else chooses between cash and card. Derived rather than trusted
    because the database refuses the disagreement anyway, and a service that
    let the caller submit a contradiction would surface it as an
    `IntegrityError` instead of as a sentence.
    """
    if category == SalesChannelCategory.DELIVERY_APPLICATION:
        return TenderDestination.APPLICATION_RECEIVABLE
    if requested == TenderDestination.APPLICATION_RECEIVABLE:
        raise ValidationError(
            _("Only a delivery-application channel settles into a receivable."),
            code="tender_needs_application_channel",
        )
    return requested


@transaction.atomic
def create_sales_channel(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    category: str,
    cost_center: CostCenter,
    name_en: str = "",
    default_tender: str = TenderDestination.CASH,
    revenue_account: Account | None = None,
    requires_cashier: bool = True,
    display_order: int = 1,
    notes: str = "",
) -> SalesChannel:
    """
    Add a route sales arrive by.

    The cost centre is required rather than optional, and that is the field
    worth pausing on: revenue, discount, commission and fee accounts are all in
    classes that require one (ADR-014), so a channel without a cost centre is a
    channel whose sales cannot post. Refusing it here means the failure lands
    on the person configuring the channel rather than on the cashier trying to
    close a day.
    """
    if cost_center.organization_id != organization.pk:
        raise ValidationError(
            _("A channel and its cost center must belong to the same organization."),
            code="cost_center_organization_mismatch",
        )
    if revenue_account is not None and revenue_account.organization_id != organization.pk:
        raise ValidationError(
            _("A channel and its revenue account must belong to the same organization."),
            code="account_organization_mismatch",
        )

    channel = SalesChannel(
        organization=organization,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        category=category,
        default_tender=_tender_for(category, default_tender),
        cost_center=cost_center,
        revenue_account=revenue_account,
        requires_cashier=requires_cashier,
        requires_delivery_application=(category == SalesChannelCategory.DELIVERY_APPLICATION),
        display_order=display_order,
        notes=notes.strip(),
    )
    channel.full_clean()
    channel.save()
    record_audit_event(action=AuditAction.CREATED, target=channel, new_state=snapshot(channel))
    return channel


@transaction.atomic
def update_sales_channel(
    *,
    channel: SalesChannel,
    name_ar: str,
    cost_center: CostCenter,
    name_en: str = "",
    default_tender: str = TenderDestination.CASH,
    revenue_account: Account | None = None,
    requires_cashier: bool = True,
    display_order: int = 1,
    notes: str = "",
    is_active: bool = True,
) -> SalesChannel:
    """
    Correct a channel, or archive one.

    **The category is absent from the signature**, and deliberately. It decides
    whether a channel settles or is counted, which sales days already posted
    against it were recorded under; changing it would silently reinterpret
    history that has already reached the ledger. A channel that turns out to be
    the wrong category is archived and replaced.
    """
    if cost_center.organization_id != channel.organization_id:
        raise ValidationError(
            _("A channel and its cost center must belong to the same organization."),
            code="cost_center_organization_mismatch",
        )
    if revenue_account is not None and revenue_account.organization_id != channel.organization_id:
        raise ValidationError(
            _("A channel and its revenue account must belong to the same organization."),
            code="account_organization_mismatch",
        )
    previous = snapshot(channel)
    channel.name_ar = name_ar.strip()
    channel.name_en = name_en.strip()
    channel.cost_center = cost_center
    channel.revenue_account = revenue_account
    channel.default_tender = _tender_for(channel.category, default_tender)
    channel.requires_cashier = requires_cashier
    channel.display_order = display_order
    channel.notes = notes.strip()
    channel.is_active = is_active
    channel.full_clean()
    channel.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=channel,
        previous_state=previous,
        new_state=snapshot(channel),
    )
    return channel


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


@transaction.atomic
def create_menu_price(
    *,
    menu_item: MenuItem,
    branch: Branch,
    unit_price: Decimal,
    effective_from: datetime.date,
    scope: str = PriceScope.BRANCH_DEFAULT,
    channel: SalesChannel | None = None,
    effective_to: datetime.date | None = None,
    evidence_reference: str = "",
    notes: str = "",
) -> MenuPriceVersion:
    """
    State what an item costs the customer, from a date.

    Overlaps are caught by the exclusion constraints in migration `0002`, not
    here, and that is on purpose: two concurrent requests both read a clean
    table before either writes, so a service check would pass for both. What
    this function does is turn the resulting `IntegrityError` into an error
    somebody can act on, by refusing the obvious cases first.
    """
    if branch.organization_id != menu_item.organization_id:
        raise ValidationError(
            _("A price and its menu item must belong to the same organization."),
            code="branch_organization_mismatch",
        )
    if scope == PriceScope.APPLICATION:
        raise ValidationError(
            _("Application-scoped prices arrive with the delivery application master."),
            code="application_scope_not_available",
        )
    if scope == PriceScope.CHANNEL and channel is None:
        raise ValidationError(_("A channel-scoped price needs a channel."), code="channel_required")
    if scope != PriceScope.CHANNEL and channel is not None:
        raise ValidationError(
            _("Only a channel-scoped price may name a channel."), code="channel_not_allowed"
        )
    if channel is not None and channel.organization_id != menu_item.organization_id:
        raise ValidationError(
            _("A price and its channel must belong to the same organization."),
            code="channel_organization_mismatch",
        )

    price = MenuPriceVersion(
        menu_item=menu_item,
        branch=branch,
        scope=scope,
        channel=channel,
        unit_price=unit_price,
        effective_from=effective_from,
        effective_to=effective_to,
        evidence_reference=evidence_reference.strip(),
        notes=notes.strip(),
    )
    price.full_clean()
    price.save()
    record_audit_event(
        action=AuditAction.CREATED, target=price, new_state=snapshot(price), branch=branch
    )
    return price


@transaction.atomic
def close_menu_price(
    *, price: MenuPriceVersion, effective_to: datetime.date, reason: str = ""
) -> MenuPriceVersion:
    """
    End a price on a date. **The amount is never edited.**

    A price that has been used to sell something is evidence, and correcting it
    in place would restate revenue that has already posted. Ending it and
    creating a replacement is the only correction, exactly as a recipe version
    is superseded rather than amended (ADR-024).
    """
    if effective_to < price.effective_from:
        raise ValidationError(_("A price cannot end before it started."), code="range_out_of_order")
    previous = snapshot(price)
    price.effective_to = effective_to
    price.full_clean()
    price.save(update_fields=["effective_to", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=price,
        previous_state=previous,
        new_state=snapshot(price),
        reason=reason,
        branch=price.branch,
    )
    return price


@transaction.atomic
def archive_menu_price(*, price: MenuPriceVersion, reason: str = "") -> MenuPriceVersion:
    """
    Withdraw a price that should never have applied.

    Distinct from closing one: closing says "this was right until Tuesday",
    archiving says "this was never right". The row stays, because a sales line
    may point at it and the history has to remain readable.
    """
    previous = snapshot(price)
    price.is_active = False
    price.save(update_fields=["is_active", "updated_at"])
    record_audit_event(
        action=AuditAction.DEACTIVATED,
        target=price,
        previous_state=previous,
        new_state=snapshot(price),
        reason=reason,
        branch=price.branch,
    )
    return price


__all__ = [
    "archive_menu_price",
    "close_menu_price",
    "create_menu_category",
    "create_menu_item",
    "create_menu_price",
    "create_sales_channel",
    "set_branch_availability",
    "update_menu_category",
    "update_menu_item",
    "update_sales_channel",
]
