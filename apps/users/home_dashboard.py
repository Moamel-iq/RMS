"""
The landing dashboard, composed from every module's own overview.

This module owns no figure. Each number here is produced by the module that
owns it — `inventory_overview`, `procurement_overview`, `kitchen_overview`,
`hr_overview`, and the sales `headline_for` — called with the same scope and
the same redaction flag that module's own screen would use. The home page
therefore cannot disagree with a module page, and it cannot show a figure a
module page would hide: a storekeeper who sees no stock value in Inventory
sees no stock value here either.

Two gates per module, and they are different questions:

* **Is the section shown at all?** The module's base view permission. A user
  with no post in a module gets no card for it rather than a card of zeros.
* **Is the money shown?** The module's cost permission, passed through as
  `include_*`. The module omits the figure; the template omits the card.

The readiness checklist is the one thing this module derives itself, and it
derives it from the overviews above: "are recipes sellable" is
`kitchen.sellable`, not a second query.

Everything here is a read. Nothing writes, posts, or caches.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.users.models import User

if TYPE_CHECKING:
    from apps.hr.dashboard import HrOverview
    from apps.inventory.dashboard import InventoryOverview
    from apps.kitchen.dashboard import KitchenOverview
    from apps.procurement.dashboard import ProcurementOverview
    from apps.sales.dashboard import Headline, MixRow


@dataclass(frozen=True)
class ReadinessItem:
    """One thing the business needs before its figures mean anything."""

    label: str
    done: bool
    detail: str
    url_name: str


@dataclass(frozen=True)
class SalesSlice:
    """Sales over the window, with the channel mix, for callers who may see it."""

    headline: Headline
    channels: list[MixRow]
    date_from: datetime.date
    date_to: datetime.date


@dataclass(frozen=True)
class HomeOverview:
    """
    What the landing page renders. Every section is optional: `None` means
    the caller holds no post in that module, and the template shows nothing
    for it — not a zero, not a placeholder.
    """

    inventory: InventoryOverview | None = None
    procurement: ProcurementOverview | None = None
    kitchen: KitchenOverview | None = None
    hr: HrOverview | None = None
    sales: SalesSlice | None = None
    readiness: list[ReadinessItem] = field(default_factory=list)

    @property
    def blocked_by_drafts(self) -> bool:
        """Nothing sold because nothing is approved — the opening alert."""
        if self.kitchen is None or self.kitchen.recipe_count == 0:
            return False
        no_sales = self.sales is None or self.sales.headline.day_count == 0
        return no_sales and not self.kitchen.sellable


def _sales(user: User) -> SalesSlice | None:
    from apps.organizations.authorization import organizations_with_permission
    from apps.sales.dashboard import (
        DashboardScope,
        channel_mix,
        default_window,
        headline_for,
    )
    from apps.sales.permissions import VIEW_SALES_REPORTS

    organization = organizations_with_permission(user, VIEW_SALES_REPORTS).order_by("code").first()
    if organization is None:
        return None
    date_from, date_to = default_window(timezone.localdate())
    scope = DashboardScope(organization_id=organization.pk, date_from=date_from, date_to=date_to)
    return SalesSlice(
        headline=headline_for(user, scope),
        channels=channel_mix(user, scope),
        date_from=date_from,
        date_to=date_to,
    )


def home_overview(user: User) -> HomeOverview:
    """Assemble the landing page for `user`, module by module, permission by permission."""
    from apps.hr.dashboard import hr_overview
    from apps.hr.permissions import VIEW_EMPLOYEE, VIEW_EMPLOYEE_SALARY
    from apps.inventory.dashboard import inventory_overview
    from apps.inventory.permissions import VIEW_STOCK, VIEW_VALUATION
    from apps.kitchen.dashboard import kitchen_overview
    from apps.kitchen.permissions import VIEW_RECIPE, VIEW_RECIPE_COST
    from apps.procurement.dashboard import procurement_overview
    from apps.procurement.permissions import VIEW_SUPPLIER_COST, VIEW_SUPPLIER_INVOICE

    inventory = (
        inventory_overview(user, include_valuation=user.has_perm(VIEW_VALUATION))
        if user.has_perm(VIEW_STOCK)
        else None
    )
    procurement = (
        procurement_overview(user, include_cost=user.has_perm(VIEW_SUPPLIER_COST))
        if user.has_perm(VIEW_SUPPLIER_INVOICE)
        else None
    )
    kitchen = (
        kitchen_overview(user, include_cost=user.has_perm(VIEW_RECIPE_COST))
        if user.has_perm(VIEW_RECIPE)
        else None
    )
    hr = (
        hr_overview(user, include_salary=user.has_perm(VIEW_EMPLOYEE_SALARY))
        if user.has_perm(VIEW_EMPLOYEE)
        else None
    )
    sales = _sales(user)

    readiness: list[ReadinessItem] = []
    if inventory is not None:
        readiness.append(
            ReadinessItem(
                label="المخزون مقيَّم",
                done=inventory.stocked_item_count > 0,
                detail=f"{inventory.stocked_item_count} صنفاً له رصيد",
                url_name="inventory:overview",
            )
        )
    if procurement is not None:
        readiness.append(
            ReadinessItem(
                label="المشتريات مرحّلة",
                done=procurement.posted_count > 0,
                detail=f"{procurement.posted_count} فاتورة مرحّلة",
                url_name="procurement:overview",
            )
        )
    if kitchen is not None:
        readiness.append(
            ReadinessItem(
                label="الوصفات معتمدة للبيع",
                done=kitchen.sellable,
                detail=f"{kitchen.active_version_count} نسخة فعّالة من {kitchen.recipe_count}",
                url_name="kitchen:overview",
            )
        )
    if sales is not None:
        readiness.append(
            ReadinessItem(
                label="المبيعات مرحّلة",
                done=sales.headline.day_count > 0,
                detail=f"{sales.headline.day_count} يوم مبيعات في الفترة",
                url_name="sales:dashboard",
            )
        )
    if hr is not None:
        readiness.append(
            ReadinessItem(
                label="الموظفون مُدخلون",
                done=hr.total_count > 0,
                detail=f"{hr.active_count} موظفاً نشطاً",
                url_name="hr:overview",
            )
        )

    return HomeOverview(
        inventory=inventory,
        procurement=procurement,
        kitchen=kitchen,
        hr=hr,
        sales=sales,
        readiness=readiness,
    )


def readiness_share(items: list[ReadinessItem]) -> Decimal:
    """Done items as a percentage, 0 dp, for the single meter on the page."""
    if not items:
        return Decimal("0")
    done = sum(1 for item in items if item.done)
    return (Decimal(done) * 100 / Decimal(len(items))).quantize(Decimal("1"))
