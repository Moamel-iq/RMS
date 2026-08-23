"""
Drafting a sales day: creating it, resolving a line, and submitting it.

Kept apart from `services.py` because the acts are different in kind. That file
maintains masters — a menu item, a channel, a price — none of which moves
money. This one builds the document that will, and its central function is
`add_sales_line`, which does the resolution ADR-027 §4 turns on.

## What resolution means, and why it happens once

A line arrives as a menu item, a channel, a quantity and a date. Everything
else is *resolved* from the state of the world on that business date and then
**stored**:

    the recipe version in force on that date
    the serving on that version with the item's serving code
    the price version, most specific scope first
    the delivery agreement, for an application line
    the discount programme, and the two funded shares
    the commission, from the agreement's own basis

Nothing re-resolves any of these afterwards — not a report, not the kitchen's
theoretical consumption, not the dashboard. A recipe changed in September must
not restate what August consumed, and a price changed on Monday must not
restate Sunday's revenue.

The corollary is that resolution can **fail**, and a failure is a refusal
rather than a fallback. An item whose effective recipe version has no serving
with its code cannot be sold that day; an application line with no effective
agreement cannot be priced. In both cases the service says so and writes
nothing, because a fallback is a silent answer to a question the operator did
not know they were asking.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.money import quantize_money
from apps.core.quantity import quantize_quantity
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import ItemType
from apps.sales.agreements import (
    NO_AGREEMENT_NOTICE,
    commission_for,
    inputs_from,
    resolve_agreement,
)
from apps.sales.discounts import applicable_programs, split_for
from apps.sales.models import (
    DeliveryApplication,
    DiscountProgram,
    FulfillmentSource,
    MenuItem,
    MenuItemBranchSetting,
    SalesChannel,
    SalesDay,
    SalesDayLine,
    SalesDayStatus,
    SalesTenderSummary,
    TenderDestination,
)
from apps.sales.selectors import resolve_price

if TYPE_CHECKING:
    from apps.inventory.models import InventoryItem, Warehouse
    from apps.kitchen.models import RecipeServing, RecipeVersion
    from apps.organizations.models import Branch, Organization
    from apps.users.models import User

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# The day
# ---------------------------------------------------------------------------


@transaction.atomic
def create_sales_day(
    *,
    organization: Organization,
    branch: Branch,
    business_date: datetime.date,
    actor: User,
    notes: str = "",
) -> SalesDay:
    """
    Open a branch's trading day.

    `business_date` is an argument, never `date(timezone.now())`. Which day a
    sale belongs to depends on the branch's own business-day start, and
    deriving it from a timestamp would file every sale taken after midnight
    under the wrong day — quietly, and only on the days that matter.
    """
    if branch.organization_id != organization.pk:
        raise ValidationError(
            _("A sales day's branch must belong to its organization."),
            code="branch_organization_mismatch",
        )
    if SalesDay.objects.filter(branch=branch, business_date=business_date).exists():
        raise ValidationError(
            _("This branch already has a sales day for %(date)s.")
            % {"date": business_date.isoformat()},
            code="day_already_exists",
        )

    day = SalesDay(
        organization=organization,
        branch=branch,
        business_date=business_date,
        notes=notes.strip(),
        created_by=actor,
    )
    day.full_clean()
    day.save()
    record_audit_event(
        action=AuditAction.CREATED, target=day, new_state=snapshot(day), branch=branch
    )
    return day


def _require_draft(day: SalesDay) -> None:
    if day.status != SalesDayStatus.DRAFT:
        raise ValidationError(_("Only a draft sales day can be changed."), code="day_not_draft")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedLine:
    """
    Everything a line needs, resolved from one business date.

    A value object rather than a saved row, so a screen can *preview* what a
    line would be before committing it — which is what the daily grid does when
    an operator picks an item and a channel.
    """

    menu_item: MenuItem
    channel: SalesChannel
    delivery_application: DeliveryApplication | None
    fulfillment_source: str
    recipe_version: RecipeVersion | None
    serving: RecipeServing | None
    inventory_item: InventoryItem | None
    source_warehouse: Warehouse | None
    direct_stock_qty_per_unit: Decimal | None
    direct_stock_total_base_qty: Decimal | None
    unit_price: Decimal
    quantity: Decimal
    order_count: int
    gross_amount: Decimal
    restaurant_discount: Decimal
    application_discount: Decimal
    commission_amount: Decimal
    other_fee_amount: Decimal
    customer_charge: Decimal
    net_amount: Decimal
    price_version_id: int | None
    agreement_id: int | None
    discount_program_id: int | None
    commission_basis: str
    commission_percent: Decimal
    commission_fixed_fee: Decimal


def _effective_version(menu_item: MenuItem, on_date: datetime.date) -> RecipeVersion:
    """
    The recipe version in force on this date — never the newest.

    Re-resolving to the newest version when reading history is the single error
    ADR-024 exists to prevent, and this is the function that would commit it.
    It resolves once, here, and the result is stored on the line.
    """
    from apps.kitchen.models import RecipeVersion, RecipeVersionStatus

    version = (
        RecipeVersion.objects.filter(
            recipe=menu_item.recipe,
            status=RecipeVersionStatus.ACTIVE,
            effective_from__lte=on_date,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        .order_by("-effective_from", "-version_number")
        .first()
    )
    if version is None:
        raise ValidationError(
            _("No active recipe version for %(recipe)s covers %(date)s.")
            % {
                "recipe": menu_item.recipe.code if menu_item.recipe else "—",
                "date": on_date.isoformat(),
            },
            code="no_effective_recipe_version",
        )
    return version


def _serving_on(version: RecipeVersion, serving_code: str) -> RecipeServing:
    """
    The serving with this code on this version, or a refusal.

    **No fallback to the primary serving.** An item sold as `HALF` on a version
    that only defines `WHOLE` is a configuration gap, and quietly selling the
    whole one would double every ingredient the kitchen is measured against.
    """
    serving = version.servings.filter(code=serving_code, is_active=True).first()
    if serving is None:
        raise ValidationError(
            _("Version %(version)s of %(recipe)s has no serving coded %(code)s.")
            % {
                "version": version.version_number,
                "recipe": version.recipe.code,
                "code": serving_code,
            },
            code="no_serving_on_effective_version",
        )
    return serving


def resolve_line(
    *,
    day: SalesDay,
    menu_item: MenuItem,
    channel: SalesChannel,
    quantity: Decimal,
    delivery_application: DeliveryApplication | None = None,
    order_count: int = 0,
    discount_program: DiscountProgram | None = None,
    manual_discount_amount: Decimal | None = None,
    other_fee_amount: Decimal = ZERO,
) -> ResolvedLine:
    """
    Work out what this line is worth, without writing anything.

    Every refusal below happens before a row exists, so a screen can show the
    operator the problem rather than an integrity error.
    """
    on_date = day.business_date

    if menu_item.organization_id != day.organization_id:
        raise ValidationError(
            _("A sales line's menu item must belong to the day's organization."),
            code="menu_item_organization_mismatch",
        )
    if channel.organization_id != day.organization_id:
        raise ValidationError(
            _("A sales line's channel must belong to the day's organization."),
            code="channel_organization_mismatch",
        )
    if channel.requires_delivery_application and delivery_application is None:
        raise ValidationError(
            _("A delivery-application channel needs the application that took the order."),
            code="application_required",
        )
    if not channel.requires_delivery_application and delivery_application is not None:
        raise ValidationError(
            _("Only a delivery-application channel may name an application."),
            code="application_not_allowed",
        )

    quantity = quantize_quantity(quantity)
    if quantity <= ZERO:
        raise ValidationError(_("A sales line needs a quantity."), code="quantity_required")

    branch_setting = (
        MenuItemBranchSetting.objects.select_related("source_warehouse")
        .filter(menu_item=menu_item, branch=day.branch, is_available=True)
        .first()
    )
    if branch_setting is None:
        raise ValidationError(
            _("%(item)s is not available at %(branch)s.")
            % {"item": menu_item.code, "branch": day.branch.code},
            code="menu_item_not_available",
        )

    version: RecipeVersion | None = None
    serving: RecipeServing | None = None
    inventory_item: InventoryItem | None = None
    source_warehouse: Warehouse | None = None
    direct_stock_qty_per_unit: Decimal | None = None
    direct_stock_total_base_qty: Decimal | None = None
    if menu_item.fulfillment_source == FulfillmentSource.RECIPE_SERVING:
        version = _effective_version(menu_item, on_date)
        serving = _serving_on(version, menu_item.serving_code)
    elif menu_item.fulfillment_source == FulfillmentSource.DIRECT_STOCK:
        inventory_item = menu_item.inventory_item
        source_warehouse = branch_setting.source_warehouse
        direct_stock_qty_per_unit = menu_item.direct_stock_base_quantity
        if inventory_item is None or source_warehouse is None or direct_stock_qty_per_unit is None:
            raise ValidationError(
                _("The direct-stock item is missing its item, quantity or warehouse."),
                code="direct_stock_configuration_incomplete",
            )
        if (
            inventory_item.organization_id != day.organization_id
            or source_warehouse.branch_id != day.branch_id
            or not inventory_item.is_active
            or not source_warehouse.is_active
            or source_warehouse.is_system
        ):
            raise ValidationError(
                _("The direct-stock item or source warehouse is no longer usable."),
                code="direct_stock_configuration_unusable",
            )
        if inventory_item.item_type != ItemType.GOODS_FOR_RESALE or inventory_item.tracks_lots:
            raise ValidationError(
                _("The direct-stock item is not an eligible non-lot resale item."),
                code="direct_stock_item_unusable",
            )
        direct_stock_total_base_qty = quantize_quantity(quantity * direct_stock_qty_per_unit)
        if direct_stock_total_base_qty <= ZERO:
            raise ValidationError(
                _("The resolved stock quantity rounds to zero."),
                code="direct_stock_quantity_rounds_to_zero",
            )
    else:  # pragma: no cover - constrained by MenuItem
        raise ValidationError(_("Unknown fulfillment source."), code="unknown_fulfillment_source")

    price = resolve_price(
        menu_item,
        day.branch,
        on_date,
        channel=channel,
        delivery_application=delivery_application,
    )
    if price is None:
        raise ValidationError(
            _("No price is in force for %(item)s at %(branch)s on %(date)s.")
            % {
                "item": menu_item.code,
                "branch": day.branch.code,
                "date": on_date.isoformat(),
            },
            code="no_effective_price",
        )

    gross_amount = quantize_money(quantity * price.unit_price)

    if discount_program is not None and manual_discount_amount is not None:
        raise ValidationError(
            _("A line takes a discount programme or a manual discount, never both."),
            code="discount_source_ambiguous",
        )
    if discount_program is not None:
        # The programme's own applicability, asked the one way that cannot
        # drift from the way a screen offers it: `applicable_programs` is the
        # single statement of which programme covers which line, and calling it
        # here means "offered" and "accepted" answer the same question.
        #
        # Applying an unchecked programme is not a cosmetic mistake. A discount
        # funded by one delivery application, applied to a line taken by
        # another, raises a receivable against a company that agreed to
        # reimburse nothing — and it does so *quietly*, because the funding
        # split still closes and every figure on the line still sums.
        covers = applicable_programs(
            organization_id=day.organization_id,
            branch_id=day.branch_id,
            on_date=on_date,
            menu_item_id=menu_item.pk,
            channel_id=channel.pk,
            delivery_application_id=(
                delivery_application.pk if delivery_application is not None else None
            ),
        ).filter(pk=discount_program.pk)
        if not covers.exists():
            raise ValidationError(
                _(
                    "Discount programme %(code)s does not cover this line on %(date)s. "
                    "Check its branch, channel, item, application and effective dates."
                )
                % {"code": discount_program.code, "date": on_date.isoformat()},
                code="discount_program_not_applicable",
            )
    split = split_for(
        discount_program, gross_amount=gross_amount, manual_amount=manual_discount_amount
    )
    if split.application_funded > ZERO and delivery_application is None:
        raise ValidationError(
            _("An application-funded discount needs the application that funds it."),
            code="application_discount_without_an_application",
        )

    agreement = None
    commission = ZERO
    basis = ""
    percent = ZERO
    fixed_fee = ZERO
    other_fee_amount = quantize_money(other_fee_amount)

    if delivery_application is not None:
        agreement = resolve_agreement(
            branch_id=day.branch_id,
            delivery_application_id=delivery_application.pk,
            on_date=on_date,
        )
        if agreement is None:
            raise ValidationError(NO_AGREEMENT_NOTICE, code="no_effective_agreement")
        inputs = inputs_from(
            agreement,
            gross_amount=gross_amount,
            restaurant_discount=split.restaurant_funded,
            application_discount=split.application_funded,
            order_count=order_count,
        )
        commission = commission_for(inputs).total
        basis = agreement.commission_basis
        percent = agreement.commission_percent
        fixed_fee = agreement.fixed_fee_per_order
    elif other_fee_amount > ZERO:
        raise ValidationError(
            _("Application fees belong to an application line."), code="fees_without_application"
        )

    # What the customer handed over, and what the restaurant is owed.
    customer_charge = quantize_money(
        gross_amount - split.restaurant_funded - split.application_funded
    )
    if delivery_application is not None:
        # The application-funded discount cancels out: the company pays back
        # what it discounted (ADR-028 §5).
        net_amount = quantize_money(
            gross_amount - split.restaurant_funded - commission - other_fee_amount
        )
    else:
        net_amount = quantize_money(gross_amount - split.restaurant_funded)

    if net_amount < ZERO:
        raise ValidationError(
            _(
                "This line leaves the restaurant owing money: the commission and fees "
                "exceed what is left after the discount. Check the agreement."
            ),
            code="net_is_negative",
        )

    return ResolvedLine(
        menu_item=menu_item,
        channel=channel,
        delivery_application=delivery_application,
        fulfillment_source=menu_item.fulfillment_source,
        recipe_version=version,
        serving=serving,
        inventory_item=inventory_item,
        source_warehouse=source_warehouse,
        direct_stock_qty_per_unit=direct_stock_qty_per_unit,
        direct_stock_total_base_qty=direct_stock_total_base_qty,
        unit_price=price.unit_price,
        quantity=quantity,
        order_count=order_count,
        gross_amount=gross_amount,
        restaurant_discount=split.restaurant_funded,
        application_discount=split.application_funded,
        commission_amount=commission,
        other_fee_amount=other_fee_amount,
        customer_charge=customer_charge,
        net_amount=net_amount,
        price_version_id=price.pk,
        agreement_id=agreement.pk if agreement is not None else None,
        discount_program_id=discount_program.pk if discount_program is not None else None,
        commission_basis=basis,
        commission_percent=percent,
        commission_fixed_fee=fixed_fee,
    )


@transaction.atomic
def add_sales_line(
    *,
    day: SalesDay,
    menu_item: MenuItem,
    channel: SalesChannel,
    quantity: Decimal,
    delivery_application: DeliveryApplication | None = None,
    order_count: int = 0,
    discount_program: DiscountProgram | None = None,
    manual_discount_amount: Decimal | None = None,
    manual_discount_reason: str = "",
    other_fee_amount: Decimal = ZERO,
    notes: str = "",
) -> SalesDayLine:
    """
    Resolve a line and store it, snapshots and all.

    A manual discount requires a reason, checked here and by a check
    constraint. A discount nobody explained is exactly the silent override the
    design refuses (ADR-028 §2).
    """
    _require_draft(day)

    if manual_discount_amount is not None and not manual_discount_reason.strip():
        raise ValidationError(
            _("A manual discount needs a reason."), code="manual_discount_reason_required"
        )

    resolved = resolve_line(
        day=day,
        menu_item=menu_item,
        channel=channel,
        quantity=quantity,
        delivery_application=delivery_application,
        order_count=order_count,
        discount_program=discount_program,
        manual_discount_amount=manual_discount_amount,
        other_fee_amount=other_fee_amount,
    )

    sequence = (day.lines.count() or 0) + 1
    line = SalesDayLine(
        sales_day=day,
        sequence=sequence,
        menu_item=resolved.menu_item,
        channel=resolved.channel,
        delivery_application=resolved.delivery_application,
        fulfillment_source=resolved.fulfillment_source,
        recipe=(
            resolved.menu_item.recipe
            if resolved.fulfillment_source == FulfillmentSource.RECIPE_SERVING
            else None
        ),
        recipe_version=resolved.recipe_version,
        serving=resolved.serving,
        inventory_item=resolved.inventory_item,
        source_warehouse=resolved.source_warehouse,
        direct_stock_qty_per_unit=resolved.direct_stock_qty_per_unit,
        direct_stock_total_base_qty=resolved.direct_stock_total_base_qty,
        price_version_id=resolved.price_version_id,
        agreement_id=resolved.agreement_id,
        discount_program_id=resolved.discount_program_id,
        unit_price=resolved.unit_price,
        quantity=resolved.quantity,
        order_count=resolved.order_count,
        gross_amount=resolved.gross_amount,
        restaurant_discount=resolved.restaurant_discount,
        application_discount=resolved.application_discount,
        manual_discount_reason=manual_discount_reason.strip(),
        commission_basis=resolved.commission_basis,
        commission_percent=resolved.commission_percent,
        commission_fixed_fee=resolved.commission_fixed_fee,
        commission_amount=resolved.commission_amount,
        other_fee_amount=resolved.other_fee_amount,
        customer_charge=resolved.customer_charge,
        net_amount=resolved.net_amount,
        notes=notes.strip(),
    )
    line.full_clean()
    line.save()
    record_audit_event(
        action=AuditAction.CREATED, target=line, new_state=snapshot(line), branch=day.branch
    )
    return line


@transaction.atomic
def remove_sales_line(*, line: SalesDayLine) -> None:
    """
    Take a line off a draft. The database refuses it on any other status.

    Sequences are **not** renumbered afterwards. A gap is honest — it records
    that a line was entered and removed — and renumbering would silently
    rewrite the identity of every line after it.
    """
    day = line.sales_day
    _require_draft(day)
    previous = snapshot(line)
    line.delete()
    record_audit_event(
        action=AuditAction.DELETED,
        target_type="sales.SalesDayLine",
        target_id=str(previous.get("public_id", "")),
        previous_state=previous,
        branch=day.branch,
    )


@transaction.atomic
def set_tender_summary(
    *, day: SalesDay, tender: str, declared_amount: Decimal, notes: str = ""
) -> SalesTenderSummary:
    """
    Record what the day's takings were *declared* to be, per tender.

    Declared rather than derived, and the difference is the reason this exists:
    the derived figure is what the lines say, and this is what the operator
    says. A day whose two disagree is a day worth looking at before it posts.
    """
    _require_draft(day)
    if tender not in TenderDestination.values:
        raise ValidationError(_("Unknown tender."), code="unknown_tender")

    summary = SalesTenderSummary.objects.filter(sales_day=day, tender=tender).first()
    previous = snapshot(summary) if summary is not None else None
    if summary is None:
        summary = SalesTenderSummary(sales_day=day, tender=tender)
    summary.declared_amount = quantize_money(declared_amount)
    summary.notes = notes.strip()
    summary.full_clean()
    summary.save()
    record_audit_event(
        action=AuditAction.CREATED if previous is None else AuditAction.UPDATED,
        target=summary,
        previous_state=previous,
        new_state=snapshot(summary),
        branch=day.branch,
    )
    return summary


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@transaction.atomic
def submit_sales_day(*, day: SalesDay, actor: User) -> SalesDay:
    """
    The cashier's statement: this is what I counted.

    Refuses an empty day. A submitted day with no lines would ask somebody to
    approve nothing, and the approval would be real.
    """
    locked = SalesDay.objects.select_for_update().get(pk=day.pk)
    if locked.status != SalesDayStatus.DRAFT:
        raise ValidationError(_("Only a draft sales day can be submitted."), code="day_not_draft")
    if not locked.lines.exists():
        raise ValidationError(_("An empty sales day cannot be submitted."), code="no_lines")

    previous = snapshot(locked)
    locked.status = SalesDayStatus.SUBMITTED
    locked.submitted_by = actor
    locked.submitted_at = timezone.now()
    locked.save(update_fields=["status", "submitted_by", "submitted_at", "updated_at"])
    record_audit_event(
        action=AuditAction.SUBMITTED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
        branch=locked.branch,
    )
    return locked


@transaction.atomic
def return_sales_day_to_draft(*, day: SalesDay, actor: User, reason: str) -> SalesDay:
    """
    Send a submitted day back for correction.

    The way a submitted day's figures become editable again, and the only one:
    the database refuses an edit while the status is `SUBMITTED`, so a
    correction has to pass through here and leave a reason on the trail.
    """
    if not reason.strip():
        raise ValidationError(_("Returning a sales day needs a reason."), code="reason_required")
    locked = SalesDay.objects.select_for_update().get(pk=day.pk)
    if locked.status != SalesDayStatus.SUBMITTED:
        raise ValidationError(
            _("Only a submitted sales day can be returned to draft."), code="day_not_submitted"
        )

    previous = snapshot(locked)
    locked.status = SalesDayStatus.DRAFT
    locked.submitted_by = None
    locked.submitted_at = None
    locked.save(update_fields=["status", "submitted_by", "submitted_at", "updated_at"])
    record_audit_event(
        action=AuditAction.REJECTED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
        branch=locked.branch,
    )
    return locked


# ---------------------------------------------------------------------------
# Reads a screen needs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayTotals:
    """What a day adds up to, derived from its lines every time."""

    gross: Decimal
    restaurant_discount: Decimal
    application_discount: Decimal
    commission: Decimal
    other_fees: Decimal
    customer_charge: Decimal
    net_cash: Decimal
    net_card: Decimal
    net_application: Decimal
    line_count: int

    @property
    def net_revenue(self) -> Decimal:
        """Gross less what the restaurant itself funded. Never less the app's."""
        return quantize_money(self.gross - self.restaurant_discount)


def totals_for(day: SalesDay) -> DayTotals:
    """
    Add a day up. Derived every time, never stored.

    A stored total is a second source of truth for a figure the lines already
    carry, and the one that drifts is always the stored one.
    """
    gross = restaurant = application = commission = fees = charge = ZERO
    cash = card = receivable = ZERO
    count = 0
    for line in day.lines.select_related("channel").all():
        count += 1
        gross += line.gross_amount
        restaurant += line.restaurant_discount
        application += line.application_discount
        commission += line.commission_amount
        fees += line.other_fee_amount
        charge += line.customer_charge
        if line.is_application_sale:
            receivable += line.net_amount
        elif line.channel.default_tender == TenderDestination.CARD:
            card += line.net_amount
        else:
            cash += line.net_amount
    return DayTotals(
        gross=quantize_money(gross),
        restaurant_discount=quantize_money(restaurant),
        application_discount=quantize_money(application),
        commission=quantize_money(commission),
        other_fees=quantize_money(fees),
        customer_charge=quantize_money(charge),
        net_cash=quantize_money(cash),
        net_card=quantize_money(card),
        net_application=quantize_money(receivable),
        line_count=count,
    )


def sellable_items(day: SalesDay) -> list[MenuItem]:
    """
    What this branch can actually sell on this day.

    Availability only — the grid still resolves each line, and a price or a
    version that has lapsed is refused there with a sentence. Filtering here
    keeps a hundred archived items out of a dropdown.
    """
    return list(
        MenuItem.objects.filter(
            organization_id=day.organization_id,
            is_active=True,
            branch_settings__branch=day.branch,
            branch_settings__is_available=True,
        )
        .filter(
            Q(fulfillment_source=FulfillmentSource.RECIPE_SERVING)
            | Q(
                fulfillment_source=FulfillmentSource.DIRECT_STOCK,
                branch_settings__source_warehouse__isnull=False,
            )
        )
        .select_related("category", "recipe", "inventory_item")
        .order_by("display_order", "code")
    )


def channels_for(day: SalesDay) -> list[SalesChannel]:
    return list(
        SalesChannel.objects.filter(organization_id=day.organization_id, is_active=True)
        .select_related("cost_center")
        .order_by("display_order", "code")
    )


def applications_for(day: SalesDay) -> list[DeliveryApplication]:
    """Applications this branch is live on. An inactive one takes no orders."""
    return list(
        DeliveryApplication.objects.filter(
            organization_id=day.organization_id,
            is_active=True,
            branch_settings__branch=day.branch,
            branch_settings__is_active=True,
        ).order_by("code")
    )


__all__ = [
    "DayTotals",
    "ResolvedLine",
    "add_sales_line",
    "applications_for",
    "channels_for",
    "create_sales_day",
    "remove_sales_line",
    "resolve_line",
    "return_sales_day_to_draft",
    "sellable_items",
    "set_tender_summary",
    "submit_sales_day",
    "totals_for",
]
