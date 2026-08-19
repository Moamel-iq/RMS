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
    CommissionBasis,
    DeliveryAgreement,
    DeliveryApplication,
    DeliveryApplicationBranchSetting,
    DiscountProgram,
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
    delivery_application: DeliveryApplication | None = None,
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
    if scope == PriceScope.APPLICATION and delivery_application is None:
        raise ValidationError(
            _("An application-scoped price needs a delivery application."),
            code="application_required",
        )
    if scope != PriceScope.APPLICATION and delivery_application is not None:
        raise ValidationError(
            _("Only an application-scoped price may name a delivery application."),
            code="application_not_allowed",
        )
    if (
        delivery_application is not None
        and delivery_application.organization_id != menu_item.organization_id
    ):
        raise ValidationError(
            _("A price and its delivery application must belong to the same organization."),
            code="application_organization_mismatch",
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
        delivery_application=delivery_application,
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


# ---------------------------------------------------------------------------
# Delivery applications — checkpoint 2
# ---------------------------------------------------------------------------


@transaction.atomic
def create_delivery_application(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    name_en: str = "",
    settlement_cycle_days: int = 30,
    receivable_account: Account | None = None,
    contact_name: str = "",
    phone: str = "",
    notes: str = "",
) -> DeliveryApplication:
    """
    Register a delivery company.

    No balance is created, because there is no balance field: a new application
    is owed nothing because nothing has been posted against it, not because a
    zero was written somewhere that could later disagree with the ledger
    (ADR-027 §5).

    No commission rate either. Rates are effective-dated contract terms and
    live on `DeliveryAgreement`; one here would be a second answer that no
    posted sale could be traced back to.
    """
    if receivable_account is not None and receivable_account.organization_id != organization.pk:
        raise ValidationError(
            _("An application and its receivable account must belong to the same organization."),
            code="account_organization_mismatch",
        )
    application = DeliveryApplication(
        organization=organization,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        settlement_cycle_days=settlement_cycle_days,
        receivable_account=receivable_account,
        contact_name=contact_name.strip(),
        phone=phone.strip(),
        notes=notes.strip(),
    )
    application.full_clean()
    application.save()
    record_audit_event(
        action=AuditAction.CREATED, target=application, new_state=snapshot(application)
    )
    return application


@transaction.atomic
def update_delivery_application(
    *,
    application: DeliveryApplication,
    name_ar: str,
    name_en: str = "",
    settlement_cycle_days: int = 30,
    receivable_account: Account | None = None,
    contact_name: str = "",
    phone: str = "",
    notes: str = "",
    is_active: bool = True,
) -> DeliveryApplication:
    """
    Correct an application, or archive one.

    Changing the receivable account **does not** move anything already posted.
    A posted journal names the account it used; this decides where the next one
    lands. Screens say so, because the alternative reading — that the balance
    follows the setting — is the one an operator naturally assumes.
    """
    if (
        receivable_account is not None
        and receivable_account.organization_id != application.organization_id
    ):
        raise ValidationError(
            _("An application and its receivable account must belong to the same organization."),
            code="account_organization_mismatch",
        )
    previous = snapshot(application)
    application.name_ar = name_ar.strip()
    application.name_en = name_en.strip()
    application.settlement_cycle_days = settlement_cycle_days
    application.receivable_account = receivable_account
    application.contact_name = contact_name.strip()
    application.phone = phone.strip()
    application.notes = notes.strip()
    application.is_active = is_active
    application.full_clean()
    application.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=application,
        previous_state=previous,
        new_state=snapshot(application),
    )
    return application


@transaction.atomic
def set_application_branch_setting(
    *,
    application: DeliveryApplication,
    branch: Branch,
    is_active: bool = True,
    external_store_code: str = "",
    notes: str = "",
) -> DeliveryApplicationBranchSetting:
    """Say whether one branch trades with one application."""
    if branch.organization_id != application.organization_id:
        raise ValidationError(
            _("An application can only be activated at its own organization's branches."),
            code="branch_organization_mismatch",
        )
    setting = DeliveryApplicationBranchSetting.objects.filter(
        delivery_application=application, branch=branch
    ).first()
    previous = snapshot(setting) if setting is not None else None
    if setting is None:
        setting = DeliveryApplicationBranchSetting(delivery_application=application, branch=branch)
    setting.is_active = is_active
    setting.external_store_code = external_store_code.strip()
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
# Agreements — checkpoint 2
# ---------------------------------------------------------------------------


@transaction.atomic
def create_delivery_agreement(
    *,
    branch: Branch,
    delivery_application: DeliveryApplication,
    effective_from: datetime.date,
    commission_percent: Decimal = Decimal("0"),
    fixed_fee_per_order: Decimal = Decimal("0"),
    commission_basis: str = CommissionBasis.GROSS_LIST_AMOUNT,
    settlement_lag_days: int = 30,
    effective_to: datetime.date | None = None,
    evidence_reference: str = "",
    notes: str = "",
) -> DeliveryAgreement:
    """
    Record what one application charges one branch, from a date.

    **Evidence is required**, and this is the one master in the module that
    insists on it. An agreement is a claim about a contract with another
    company, and it accrues an expense on every order from the day it starts;
    an unevidenced rate is a number somebody typed that nobody can check
    against anything.

    Overlaps are refused by the exclusion constraint in migration `0003`
    rather than here, for the reason the price overlap is: two concurrent
    requests both read a clean table before either writes.
    """
    if branch.organization_id != delivery_application.organization_id:
        raise ValidationError(
            _("An agreement's branch and application must belong to the same organization."),
            code="branch_organization_mismatch",
        )
    if not evidence_reference.strip():
        raise ValidationError(
            _("A commission agreement needs the contract or approval it rests on."),
            code="evidence_required",
        )
    if commission_basis not in CommissionBasis.values:
        raise ValidationError(
            _("%(basis)s is not an approved commission basis.") % {"basis": commission_basis},
            code="unknown_commission_basis",
        )

    agreement = DeliveryAgreement(
        organization_id=branch.organization_id,
        branch=branch,
        delivery_application=delivery_application,
        commission_percent=commission_percent,
        fixed_fee_per_order=fixed_fee_per_order,
        commission_basis=commission_basis,
        settlement_lag_days=settlement_lag_days,
        effective_from=effective_from,
        effective_to=effective_to,
        evidence_reference=evidence_reference.strip(),
        notes=notes.strip(),
    )
    agreement.full_clean()
    agreement.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=agreement,
        new_state=snapshot(agreement),
        branch=branch,
    )
    return agreement


@transaction.atomic
def close_delivery_agreement(
    *, agreement: DeliveryAgreement, effective_to: datetime.date, reason: str
) -> DeliveryAgreement:
    """
    End an agreement on a date. **The rate is never edited.**

    A rate that has accrued a commission is evidence, and correcting it in
    place would restate an expense that has already posted. Ending it and
    recording the replacement is the only correction, exactly as with a price
    and a recipe version.
    """
    if effective_to < agreement.effective_from:
        raise ValidationError(
            _("An agreement cannot end before it started."), code="range_out_of_order"
        )
    if not reason.strip():
        raise ValidationError(_("Ending an agreement needs a reason."), code="reason_required")
    previous = snapshot(agreement)
    agreement.effective_to = effective_to
    agreement.full_clean()
    agreement.save(update_fields=["effective_to", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=agreement,
        previous_state=previous,
        new_state=snapshot(agreement),
        reason=reason.strip(),
        branch=agreement.branch,
    )
    return agreement


# ---------------------------------------------------------------------------
# Discount programmes — checkpoint 2
# ---------------------------------------------------------------------------


@transaction.atomic
def create_discount_program(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    effective_from: datetime.date,
    discount_percent: Decimal | None = None,
    discount_amount: Decimal | None = None,
    restaurant_funded_share: Decimal = Decimal("100"),
    application_funded_share: Decimal = Decimal("0"),
    branch: Branch | None = None,
    channel: SalesChannel | None = None,
    delivery_application: DeliveryApplication | None = None,
    menu_item: MenuItem | None = None,
    maximum_amount: Decimal | None = None,
    name_en: str = "",
    effective_to: datetime.date | None = None,
    evidence_reference: str = "",
    notes: str = "",
) -> DiscountProgram:
    """
    Create a discount, and record who funds it.

    The funding equality is checked here **and** by a check constraint. Both,
    because they do different jobs: the constraint is what makes it true even
    against a raw `INSERT`, and this is what makes it *explainable* on a form
    before a save fails with a database error nobody can read.

    An application-funded share must name the application that promised it. A
    "50% funded by the app" promotion with no application attached could be
    applied to a cash sale in the hall, and the receivable it implies would be
    owed by nobody.
    """
    if discount_percent is None and discount_amount is None:
        raise ValidationError(
            _("A discount needs a percentage or an amount."), code="discount_value_required"
        )
    if discount_percent is not None and discount_amount is not None:
        raise ValidationError(
            _("A discount states a percentage or an amount, never both."),
            code="discount_value_ambiguous",
        )
    if restaurant_funded_share + application_funded_share != Decimal("100"):
        raise ValidationError(
            _(
                "The funding shares must add up to the whole discount: "
                "%(restaurant)s%% + %(application)s%% is not 100%%."
            )
            % {"restaurant": restaurant_funded_share, "application": application_funded_share},
            code="funding_does_not_close",
        )
    if application_funded_share > Decimal("0") and delivery_application is None:
        raise ValidationError(
            _("A discount funded by an application must name the application that funds it."),
            code="application_funding_needs_an_application",
        )
    for related, message in (
        (branch, _("A discount and its branch must belong to the same organization.")),
        (channel, _("A discount and its channel must belong to the same organization.")),
        (
            delivery_application,
            _("A discount and its application must belong to the same organization."),
        ),
        (menu_item, _("A discount and its menu item must belong to the same organization.")),
    ):
        if related is not None and related.organization_id != organization.pk:
            raise ValidationError(message, code="organization_mismatch")

    program = DiscountProgram(
        organization=organization,
        branch=branch,
        code=_require_code(code),
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        maximum_amount=maximum_amount,
        restaurant_funded_share=restaurant_funded_share,
        application_funded_share=application_funded_share,
        channel=channel,
        delivery_application=delivery_application,
        menu_item=menu_item,
        effective_from=effective_from,
        effective_to=effective_to,
        evidence_reference=evidence_reference.strip(),
        notes=notes.strip(),
    )
    program.full_clean()
    program.save()
    record_audit_event(
        action=AuditAction.CREATED, target=program, new_state=snapshot(program), branch=branch
    )
    return program


@transaction.atomic
def close_discount_program(
    *, program: DiscountProgram, effective_to: datetime.date, reason: str
) -> DiscountProgram:
    """End a programme on a date. Its amounts and shares are never edited."""
    if effective_to < program.effective_from:
        raise ValidationError(
            _("A discount cannot end before it started."), code="range_out_of_order"
        )
    if not reason.strip():
        raise ValidationError(_("Ending a discount needs a reason."), code="reason_required")
    previous = snapshot(program)
    program.effective_to = effective_to
    program.full_clean()
    program.save(update_fields=["effective_to", "updated_at"])
    record_audit_event(
        action=AuditAction.UPDATED,
        target=program,
        previous_state=previous,
        new_state=snapshot(program),
        reason=reason.strip(),
        branch=program.branch,
    )
    return program


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
    # --- checkpoint 2 -----------------------------------------------------
    "close_delivery_agreement",
    "close_discount_program",
    "create_delivery_agreement",
    "create_delivery_application",
    "create_discount_program",
    "set_application_branch_setting",
    "update_delivery_application",
]
