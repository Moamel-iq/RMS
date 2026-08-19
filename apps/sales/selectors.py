"""
Sales reads. Nothing here writes.

Every selector starts from the **caller**, never from an identifier. A function
that took an id and returned an object would have to be checked by whoever
called it, and the first caller to forget is the leak — so an out-of-scope row
is simply not in the queryset, and "absent" and "foreign" are
indistinguishable from outside (ADR-016).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.inventory.selectors import reachable_organization_ids
from apps.organizations.authorization import OutOfScope
from apps.organizations.selectors import accessible_branches
from apps.sales.models import (
    DeliveryAgreement,
    DeliveryApplication,
    DiscountProgram,
    MenuCategory,
    MenuItem,
    MenuItemBranchSetting,
    MenuPriceVersion,
    PriceScope,
    SalesChannel,
)

if TYPE_CHECKING:
    from apps.organizations.models import Branch
    from apps.users.models import User


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


def visible_menu_categories(user: User) -> QuerySet[MenuCategory]:
    """Every menu category in an organization the caller reaches."""
    return MenuCategory.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization")


def visible_menu_items(user: User) -> QuerySet[MenuItem]:
    """
    Every menu item in an organization the caller reaches — **archived ones
    included**.

    Archived items stay visible on purpose, exactly as archived suppliers and
    recipes do. Their code is still reserved, every posted sales line still
    points at them, and a screen that hid them would make a taken code look
    free and leave no way to reactivate one archived by mistake.
    """
    return MenuItem.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization", "category", "recipe")


def resolve_menu_item(user: User, menu_item_id: int) -> MenuItem:
    """
    Turn a submitted id into a menu item the caller may reach.

    Resolved **with** the caller rather than fetched and then checked: there is
    no moment in which an out-of-scope item exists in a local variable waiting
    to be misused.
    """
    item = visible_menu_items(user).filter(pk=menu_item_id).first()
    if item is None:
        raise OutOfScope(_("Menu item %(id)s does not exist.") % {"id": menu_item_id})
    return item


def visible_sales_channels(user: User) -> QuerySet[SalesChannel]:
    """Every sales channel in an organization the caller reaches."""
    return SalesChannel.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization", "cost_center", "revenue_account")


def resolve_sales_channel(user: User, channel_id: int) -> SalesChannel:
    channel = visible_sales_channels(user).filter(pk=channel_id).first()
    if channel is None:
        raise OutOfScope(_("Sales channel %(id)s does not exist.") % {"id": channel_id})
    return channel


def visible_menu_prices(user: User) -> QuerySet[MenuPriceVersion]:
    """
    Every price row for an item the caller reaches, at a branch they reach.

    Both halves matter. The item test alone would show one branch's manager the
    prices of a branch they have nothing to do with; the branch test alone
    would be unfiltered for a caller with no branch membership at all.
    """
    return MenuPriceVersion.objects.filter(
        menu_item__organization_id__in=reachable_organization_ids(user),
        branch__in=accessible_branches(user),
    ).select_related("menu_item", "branch", "channel")


def visible_branch_settings(user: User) -> QuerySet[MenuItemBranchSetting]:
    return MenuItemBranchSetting.objects.filter(
        menu_item__organization_id__in=reachable_organization_ids(user),
        branch__in=accessible_branches(user),
    ).select_related("menu_item", "branch")


# ---------------------------------------------------------------------------
# Availability and price resolution
# ---------------------------------------------------------------------------


def is_available_at(menu_item: MenuItem, branch: Branch) -> bool:
    """
    Whether this branch sells this item today.

    A **missing** row means never sold here; a row with `is_available=False`
    means sold here normally but not at the moment. Both answer `False`, and
    the distinction survives in the data for anybody who needs it.
    """
    setting = MenuItemBranchSetting.objects.filter(menu_item=menu_item, branch=branch).first()
    return setting is not None and setting.is_available


def effective_prices(
    menu_item: MenuItem,
    branch: Branch,
    on_date: datetime.date,
    *,
    channel: SalesChannel | None = None,
    delivery_application: DeliveryApplication | None = None,
) -> list[MenuPriceVersion]:
    """
    Every price in force for this item, branch and date, most specific first.

    Returned as a list rather than a single row because the caller decides.
    `resolve_price` picks the head; a maintenance screen shows all of them so
    an operator can see *why* the narrow one won, which is the question that
    actually gets asked when a price looks wrong.

    Ordering is `CHANNEL` before `BRANCH_DEFAULT`, and within a scope the
    latest `effective_from` first. The exclusion constraints in migration
    `0002` guarantee at most one row per scope, so the second key only ever
    breaks a tie that cannot exist — it is there so the order is total rather
    than left to the database's insertion order.
    """
    rows = MenuPriceVersion.objects.filter(
        menu_item=menu_item,
        branch=branch,
        is_active=True,
        effective_from__lte=on_date,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))

    # Which scopes are even *candidates* depends on what the caller named. A
    # channel price cannot apply to a read that did not name a channel, and an
    # application price cannot apply to a sale that no application took.
    candidates = Q(scope=PriceScope.BRANCH_DEFAULT)
    if channel is not None:
        candidates |= Q(scope=PriceScope.CHANNEL, channel=channel)
    if delivery_application is not None:
        candidates |= Q(scope=PriceScope.APPLICATION, delivery_application=delivery_application)
    rows = rows.filter(candidates)

    #: Most specific wins. The rank is explicit rather than derived from the
    #: enum's declaration order, because reordering a `TextChoices` for display
    #: must never silently repriced anything.
    rank = {PriceScope.APPLICATION: 0, PriceScope.CHANNEL: 1, PriceScope.BRANCH_DEFAULT: 2}
    return sorted(
        rows, key=lambda row: (rank[PriceScope(row.scope)], -row.effective_from.toordinal())
    )


def resolve_price(
    menu_item: MenuItem,
    branch: Branch,
    on_date: datetime.date,
    *,
    channel: SalesChannel | None = None,
    delivery_application: DeliveryApplication | None = None,
) -> MenuPriceVersion | None:
    """
    The one price that applies, or `None` when nothing does.

    `None` rather than zero, and rather than an exception. Zero would be a
    price — a free plate — and an exception here would make a maintenance
    screen unable to render an item whose price has lapsed, which is exactly
    the item somebody opened the screen to fix. The *sale* refuses a line with
    no price; the read reports the absence.
    """
    rows = effective_prices(
        menu_item, branch, on_date, channel=channel, delivery_application=delivery_application
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Delivery applications, agreements and discounts — checkpoint 2
# ---------------------------------------------------------------------------


def visible_delivery_applications(user: User) -> QuerySet[DeliveryApplication]:
    """
    Every delivery application in an organization the caller reaches.

    Archived ones included, for the reason archived suppliers are: their code
    is still reserved, every posted receivable entry still points at them, and
    a screen that hid them would make a taken code look free.
    """
    return DeliveryApplication.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization", "receivable_account")


def resolve_delivery_application(user: User, application_id: int) -> DeliveryApplication:
    application = visible_delivery_applications(user).filter(pk=application_id).first()
    if application is None:
        raise OutOfScope(_("Delivery application %(id)s does not exist.") % {"id": application_id})
    return application


def visible_agreements(user: User) -> QuerySet[DeliveryAgreement]:
    """
    Every agreement for a branch the caller reaches.

    Scoped on the **branch**, not on the organization, because an agreement is
    what one branch pays one company. A caller who reaches the organization but
    not the branch has no business reading its commercial terms.
    """
    return DeliveryAgreement.objects.filter(branch__in=accessible_branches(user)).select_related(
        "branch", "delivery_application", "organization"
    )


def resolve_agreement_row(user: User, agreement_id: int) -> DeliveryAgreement:
    agreement = visible_agreements(user).filter(pk=agreement_id).first()
    if agreement is None:
        raise OutOfScope(_("Delivery agreement %(id)s does not exist.") % {"id": agreement_id})
    return agreement


def visible_discount_programs(user: User) -> QuerySet[DiscountProgram]:
    """
    Every discount programme in an organization the caller reaches.

    Organization-scoped even though a programme may name a branch, because an
    organization-wide promotion has no branch to scope by and filtering those
    out would hide exactly the programmes that affect everybody.
    """
    return DiscountProgram.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization", "branch", "channel", "delivery_application", "menu_item")


def resolve_discount_program(user: User, program_id: int) -> DiscountProgram:
    program = visible_discount_programs(user).filter(pk=program_id).first()
    if program is None:
        raise OutOfScope(_("Discount program %(id)s does not exist.") % {"id": program_id})
    return program


def application_is_live_at(application: DeliveryApplication, branch: Branch) -> bool:
    """Whether this branch trades with this application. Absence means no."""
    from apps.sales.models import DeliveryApplicationBranchSetting

    setting = DeliveryApplicationBranchSetting.objects.filter(
        delivery_application=application, branch=branch
    ).first()
    return setting is not None and setting.is_active


__all__ = [
    "application_is_live_at",
    "effective_prices",
    "is_available_at",
    "resolve_agreement_row",
    "resolve_delivery_application",
    "resolve_discount_program",
    "resolve_menu_item",
    "resolve_price",
    "resolve_sales_channel",
    "visible_agreements",
    "visible_branch_settings",
    "visible_delivery_applications",
    "visible_discount_programs",
    "visible_menu_categories",
    "visible_menu_items",
    "visible_menu_prices",
    "visible_sales_channels",
]
