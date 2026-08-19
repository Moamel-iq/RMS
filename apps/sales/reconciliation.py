"""
The Sales equations, written once, so `verify_sales` composes rather than repeats.

Every function here answers one question about whether the Sales module still
agrees with the ledgers underneath it. None of them writes, none of them repairs,
and none of them is allowed to grow a `--fix` (RCP-050): the one situation where
a repair is tempting is the situation where a human has to see the disagreement
first.

## Three severities, and only one is a failure

`ERROR`, `ADVISORY` and `COVERAGE_LIMITATION` — the same three
`apps/kitchen/consumption_reconciliation.py` defines, imported rather than
redeclared, so `verify_sales` and `verify_kitchen` can be read side by side and
a finding means the same thing in both.

An `ERROR` is something that should be impossible: a posted day with no journal,
a receivable subledger that disagrees with its control account, an adjustment
that took back more than was sold. An `ADVISORY` is something real that a person
decides about: a commission the counterparty computes differently, a till that
came up short. A `COVERAGE_LIMITATION` is something knowably absent — a menu item
with no cost snapshot behind it — and it is **not** a defect in this module.

## The one advisory that must never become an error

`verify_settlement_commission` reports a gap between the commission this system
accrued at the sale and the commission the counterparty's statement claims. That
gap is a *commercial* fact: rates get renegotiated mid-month, promotions get
funded differently than agreed, and a statement is a claim rather than a
measurement. A verifier that exited non-zero on it would be red every month and
would therefore be ignored every month, which is worse than not checking.

What **is** an error is the thing ADR-028 §6 actually forbids: a settlement
journal that debits `DELIVERY_COMMISSION_EXPENSE` a second time. Commission was
recognised at the sale. Expensing it again at settlement overstates cost of
selling and understates margin by the same amount, and both figures look
individually defensible afterwards. `verify_settlement_journals` is that check
and it is an `ERROR`.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.accounting.models import (
    DELIVERY_APP_RECEIVABLE,
    DELIVERY_COMMISSION_EXPENSE,
    SALES_CASH_ON_HAND,
    SALES_CASH_OVER_SHORT,
    SALES_DISCOUNT,
    SALES_REVENUE,
    Account,
    JournalEntry,
    JournalLine,
    SourceEvent,
)
from apps.accounting.services import resolve_default_account
from apps.core.money import quantize_money
from apps.kitchen.consumption_reconciliation import (
    ADVISORY,
    COVERAGE_LIMITATION,
    ERROR,
    Finding,
)
from apps.sales.adjustment_posting import (
    REVERSAL_RECEIVABLE_ID_SUFFIX,
)
from apps.sales.adjustment_posting import (
    SOURCE_DOCUMENT_TYPE as ADJUSTMENT_SOURCE,
)
from apps.sales.models import (
    ApplicationReceivableEntry,
    CashierShift,
    CashierShiftStatus,
    DeliveryApplication,
    DeliveryApplicationSettlement,
    DiscountProgram,
    FulfillmentSource,
    MenuItem,
    MenuPriceVersion,
    PriceScope,
    ReceivableSource,
    SalesAdjustment,
    SalesAdjustmentLine,
    SalesAdjustmentStatus,
    SalesDay,
    SalesDayStatus,
    SettlementStatus,
)
from apps.sales.posting import SOURCE_DOCUMENT_TYPE as DAY_SOURCE
from apps.sales.posting import build_plan
from apps.sales.settlement_posting import SOURCE_DOCUMENT_TYPE as SETTLEMENT_SOURCE
from apps.sales.shift_posting import SOURCE_DOCUMENT_TYPE as SHIFT_SOURCE

if TYPE_CHECKING:
    from apps.organizations.models import Organization

ZERO = Decimal("0")

#: Every source-document type this module writes. Spelled upper-case, because
#: `canonical_source_identity` case-folds the type before persisting it and a
#: verifier looking for `sales.SalesDay` would find nothing and report clean.
SALES_SOURCES: tuple[str, ...] = (
    DAY_SOURCE,
    ADJUSTMENT_SOURCE,
    SETTLEMENT_SOURCE,
    SHIFT_SOURCE,
)


def _error(code: str, message: str) -> Finding:
    return Finding(severity=ERROR, code=code, message=message)


def _advisory(code: str, message: str) -> Finding:
    return Finding(severity=ADVISORY, code=code, message=message)


def _limitation(code: str, message: str) -> Finding:
    return Finding(severity=COVERAGE_LIMITATION, code=code, message=message)


def _role_account(organization: Organization, role: str, on_date: datetime.date) -> Account | None:
    """The organization's account for a role, or `None` when nothing is mapped."""
    try:
        return resolve_default_account(
            organization=organization, account_role=role, on_date=on_date
        ).account
    except ValidationError:
        return None


def _entry_for(organization: Organization, source: str, document_id: str) -> JournalEntry | None:
    return JournalEntry.objects.filter(
        organization=organization,
        source_document_type=source,
        source_document_id=document_id,
        source_event=SourceEvent.POSTED,
    ).first()


def _nets_by_account(entry: JournalEntry) -> dict[int, Decimal]:
    """Debit minus credit per account, which is how a plan is comparable."""
    nets: dict[int, Decimal] = {}
    for line in entry.lines.all():
        nets[line.account_id] = nets.get(line.account_id, ZERO) + (line.debit - line.credit)
    return {account_id: net for account_id, net in nets.items() if net != ZERO}


# ---------------------------------------------------------------------------
# 1. Master data: the menu, its servings, its prices
# ---------------------------------------------------------------------------


def verify_menu(organization: Organization) -> list[Finding]:
    """
    Every sellable item names a recipe, and that recipe still offers its serving.

    An item whose serving code no longer exists on any version cannot be sold —
    `day_services._serving_on` refuses it — and the refusal arrives at the till
    at nine in the evening. Finding it here is the point of the check.

    `DIRECT_STOCK` is reported as an error rather than a limitation: the
    vocabulary declares it and the service refuses it (task 4.0 §3), so a row
    carrying it is a row that was written around the service.
    """
    findings: list[Finding] = []
    items = MenuItem.objects.filter(organization=organization).select_related("recipe")
    for item in items:
        if item.fulfillment_source == FulfillmentSource.DIRECT_STOCK:
            findings.append(
                _error(
                    "menu_item_direct_stock_is_not_sellable",
                    f"{item.code}: DIRECT_STOCK is declared but refused by the service; "
                    "there is no certified sales-and-COGS route out of a warehouse.",
                )
            )
            continue
        if item.recipe_id is None:
            findings.append(
                _error(
                    "menu_item_has_no_recipe",
                    f"{item.code}: a RECIPE_SERVING item must name a recipe.",
                )
            )
            continue
        if not item.serving_code:
            findings.append(
                _error(
                    "menu_item_has_no_serving",
                    f"{item.code}: a RECIPE_SERVING item must name a serving code.",
                )
            )
            continue
        offered = _serving_codes_of(item.recipe_id)
        if item.serving_code not in offered:
            # An archived item with a lapsed serving is an advisory: nothing can
            # be sold off it, and every posted line that used it still explains
            # itself from its own frozen snapshot.
            severity = _error if item.is_active else _advisory
            recipe_code = item.recipe.code if item.recipe is not None else str(item.recipe_id)
            findings.append(
                severity(
                    "menu_item_serving_is_not_offered",
                    f"{item.code}: serving {item.serving_code} is on no version of "
                    f"{recipe_code}. Known: {sorted(offered) or 'none'}.",
                )
            )
    return findings


def _serving_codes_of(recipe_id: int | None) -> set[str]:
    from apps.kitchen.models import RecipeServing

    if recipe_id is None:  # pragma: no cover - the caller has already refused it
        return set()
    return set(
        RecipeServing.objects.filter(version__recipe_id=recipe_id).values_list("code", flat=True)
    )


def verify_prices(organization: Organization) -> list[Finding]:
    """
    No two active price rows overlap within one scope, and an active item has one.

    The overlap half duplicates an exclusion constraint on purpose. A constraint
    is only a guarantee for rows written after it, and this is the cheapest
    possible proof that nothing predates it — the same reason the inventory
    verifier re-checks its own invariants.

    An active item with no price in force **today** is an advisory rather than an
    error: a seasonal dish between price versions is an ordinary state, and the
    sale refuses the line honestly when somebody tries.
    """
    findings: list[Finding] = []
    rows = list(
        MenuPriceVersion.objects.filter(
            menu_item__organization=organization, is_active=True
        ).select_related("menu_item", "branch", "channel", "delivery_application")
    )
    seen: dict[tuple[Any, ...], list[MenuPriceVersion]] = {}
    for row in rows:
        key = (
            row.menu_item_id,
            row.branch_id,
            row.scope,
            row.channel_id,
            row.delivery_application_id,
        )
        seen.setdefault(key, []).append(row)

    for key, group in seen.items():
        ordered = sorted(group, key=lambda price: price.effective_from)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if earlier.effective_to is None or earlier.effective_to >= later.effective_from:
                findings.append(
                    _error(
                        "menu_price_ranges_overlap",
                        f"{ordered[0].menu_item.code} at {ordered[0].branch.code} "
                        f"scope {key[2]}: {earlier.effective_from}..{earlier.effective_to} "
                        f"overlaps {later.effective_from}.",
                    )
                )

    today = timezone.localdate()
    priced = {
        (row.menu_item_id, row.branch_id)
        for row in rows
        if row.scope == PriceScope.BRANCH_DEFAULT
        and row.effective_from <= today
        and (row.effective_to is None or row.effective_to >= today)
    }
    from apps.sales.models import MenuItemBranchSetting

    for setting in MenuItemBranchSetting.objects.filter(
        menu_item__organization=organization, is_available=True, menu_item__is_active=True
    ).select_related("menu_item", "branch"):
        if (setting.menu_item_id, setting.branch_id) not in priced:
            findings.append(
                _advisory(
                    "menu_item_has_no_price_today",
                    f"{setting.menu_item.code} is available at {setting.branch.code} "
                    "and has no branch-default price in force today.",
                )
            )
    return findings


def verify_discount_funding(organization: Organization) -> list[Finding]:
    """
    Every discount programme's two funding shares add to exactly one hundred.

    A programme that funded 60 and 30 would leave ten percent of the discount
    belonging to nobody, and the sale would either invent a funder or drop it —
    both of which are worse than a refusal (ADR-028 §3).
    """
    findings: list[Finding] = []
    for program in DiscountProgram.objects.filter(organization=organization):
        total = program.restaurant_funded_share + program.application_funded_share
        if total != Decimal("100"):
            findings.append(
                _error(
                    "discount_funding_is_incomplete",
                    f"{program.code}: restaurant {program.restaurant_funded_share} + "
                    f"application {program.application_funded_share} = {total}, not 100.",
                )
            )
        if program.application_funded_share > ZERO and program.delivery_application_id is None:
            findings.append(
                _error(
                    "discount_application_share_names_no_application",
                    f"{program.code}: an application-funded share is a promise by a "
                    "delivery company, and this programme names none.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 2. Line arithmetic
# ---------------------------------------------------------------------------


def verify_line_arithmetic(organization: Organization) -> list[Finding]:
    """
    Every posted line's stored figures still reconstruct from each other.

    Four equalities, and each has a different failure behind it:

    * `gross = quantity × unit_price` — a price edited after the fact.
    * `customer_charge = gross − restaurant_discount − application_discount` —
      a discount recorded on one side only.
    * `net = gross − restaurant_discount − commission − other_fees` for an
      application line — the equality that makes ADR-027 §6's journal balance by
      construction rather than by luck.
    * `net = gross − restaurant_discount` otherwise.

    Checked against the **stored** columns rather than recomputed from the
    masters, because the masters are allowed to have moved and the line is not.
    """
    findings: list[Finding] = []
    lines = (
        SalesDay.objects.filter(organization=organization, status=SalesDayStatus.POSTED)
        .values_list("pk", flat=True)
        .iterator()
    )
    from apps.sales.models import SalesDayLine

    for row in SalesDayLine.objects.filter(sales_day_id__in=list(lines)).select_related(
        "sales_day", "menu_item"
    ):
        label = (
            f"{row.sales_day.number or row.sales_day_id} line {row.sequence} {row.menu_item.code}"
        )
        expected_gross = quantize_money(row.quantity * row.unit_price)
        if row.gross_amount != expected_gross:
            findings.append(
                _error(
                    "sales_line_gross_does_not_reconstruct",
                    f"{label}: {row.quantity} x {row.unit_price} = {expected_gross}, "
                    f"stored {row.gross_amount}.",
                )
            )
        expected_charge = quantize_money(
            row.gross_amount - row.restaurant_discount - row.application_discount
        )
        if row.customer_charge != expected_charge:
            findings.append(
                _error(
                    "sales_line_customer_charge_does_not_reconstruct",
                    f"{label}: expected {expected_charge}, stored {row.customer_charge}.",
                )
            )
        if row.is_application_sale:
            expected_net = quantize_money(
                row.gross_amount
                - row.restaurant_discount
                - row.commission_amount
                - row.other_fee_amount
            )
        else:
            expected_net = quantize_money(row.gross_amount - row.restaurant_discount)
        if row.net_amount != expected_net:
            findings.append(
                _error(
                    "sales_line_net_does_not_reconstruct",
                    f"{label}: expected {expected_net}, stored {row.net_amount}.",
                )
            )
        if row.is_application_sale and row.agreement_id is None:
            findings.append(
                _error(
                    "sales_line_application_has_no_agreement",
                    f"{label}: an application line must carry the agreement it accrued under.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 3. The day journal, and revenue's grossness
# ---------------------------------------------------------------------------


def verify_day_journals(organization: Organization) -> list[Finding]:
    """
    Every posted day has exactly one journal, and it still agrees with its plan.

    The plan is **rebuilt** rather than the stored journal trusted, because a
    journal that is right and a journal that is wrong look identical from the
    outside — the same reason `verify_production` recomputes per-account nets
    rather than reading `journal_entry` (RCP-112 proof 5).

    A plan that no longer rebuilds — because a mapping row was removed rather
    than closed — is an `ADVISORY` and not an error. That is a master-data
    problem, and reporting it as a ledger disagreement would send somebody
    looking in the wrong place.
    """
    findings: list[Finding] = []
    days = (
        SalesDay.objects.filter(
            organization=organization,
            status__in=[SalesDayStatus.POSTED, SalesDayStatus.REVERSED],
        )
        .select_related("organization", "branch")
        .order_by("business_date", "pk")
    )
    for day in days:
        entry = _entry_for(organization, DAY_SOURCE, str(day.public_id))
        if entry is None:
            findings.append(
                _error(
                    "sales_day_has_no_journal",
                    f"{day.number or day.pk}: posted with no journal at {DAY_SOURCE}.",
                )
            )
            continue
        duplicates = JournalEntry.objects.filter(
            organization=organization,
            source_document_type=DAY_SOURCE,
            source_document_id=str(day.public_id),
            source_event=SourceEvent.POSTED,
        ).count()
        if duplicates > 1:
            findings.append(
                _error(
                    "sales_day_has_more_than_one_journal",
                    f"{day.number or day.pk}: {duplicates} POSTED journals at one source identity.",
                )
            )

        lines = list(
            day.lines.select_related(
                "channel",
                "channel__cost_center",
                "channel__revenue_account",
                "delivery_application",
                "delivery_application__receivable_account",
            ).order_by("sequence")
        )
        try:
            plan = build_plan(day, lines)
        except ValidationError as refusal:
            findings.append(
                _advisory(
                    "sales_day_plan_is_not_reproducible",
                    f"{day.number or day.pk}: {'; '.join(refusal.messages)}",
                )
            )
            continue

        planned = {
            row.account.pk: row.debit - row.credit for row in plan.posting_lines if row.account
        }
        actual = _nets_by_account(entry)
        for account_id in sorted(set(planned) | set(actual)):
            if planned.get(account_id, ZERO) != actual.get(account_id, ZERO):
                account = Account.objects.filter(pk=account_id).first()
                findings.append(
                    _error(
                        "sales_day_journal_disagrees_with_its_plan",
                        f"{day.number or day.pk} account "
                        f"{account.code if account else account_id}: plan "
                        f"{planned.get(account_id, ZERO)}, journal {actual.get(account_id, ZERO)}.",
                    )
                )
    return findings


def verify_revenue_is_gross(organization: Organization) -> list[Finding]:
    """
    `SALES_REVENUE` carries the **gross** figure, with nothing netted inside it.

    Σ posted `gross_amount` must equal Σ credits to the revenue accounts less any
    debits to them. A system that netted discounts into the credit would produce
    a revenue figure that cannot answer "what did we give away", and every
    deduction beside it would then be double counted (ADR-027 §2).
    """
    from apps.sales.models import SalesDayLine

    day_ids = list(
        SalesDay.objects.filter(
            organization=organization, status=SalesDayStatus.POSTED
        ).values_list("pk", flat=True)
    )
    if not day_ids:
        return []

    lines = SalesDayLine.objects.filter(sales_day_id__in=day_ids).select_related(
        "sales_day", "channel"
    )
    gross_by_account: dict[int, Decimal] = {}
    for line in lines:
        account = line.channel.revenue_account_id or _default_revenue_id(
            organization, line.sales_day.business_date
        )
        if account is None:
            continue
        gross_by_account[account] = gross_by_account.get(account, ZERO) + line.gross_amount

    findings: list[Finding] = []
    for account_id, gross in sorted(gross_by_account.items()):
        # **Every** entry at a sales-day source identity, the reversing mirrors
        # included. A reversed day's credit and its mirror debit net to zero, so
        # the ledger side already excludes it — and filtering to `POSTED` here
        # would compare gross over posted days against credits that still carry
        # every day this branch ever took back.
        credited = _account_net_from_sources(organization, account_id, sources=(DAY_SOURCE,))
        # Revenue is a credit, so its net is negative; compare magnitudes.
        if quantize_money(gross) != quantize_money(-credited):
            row = Account.objects.filter(pk=account_id).first()
            findings.append(
                _error(
                    "sales_revenue_is_not_gross",
                    f"{row.code if row is not None else account_id}: posted gross "
                    f"{quantize_money(gross)}, revenue credited {quantize_money(-credited)}.",
                )
            )
    return findings


def _default_revenue_id(organization: Organization, on_date: datetime.date) -> int | None:
    account = _role_account(organization, SALES_REVENUE, on_date)
    return account.pk if account is not None else None


def _account_net_from_sources(
    organization: Organization,
    account_id: int,
    *,
    sources: Iterable[str],
    event: str | None = None,
) -> Decimal:
    """Debit minus credit on one account, restricted to the sales documents named."""
    rows = JournalLine.objects.filter(
        entry__organization=organization,
        entry__source_document_type__in=list(sources),
        account_id=account_id,
    )
    if event is not None:
        rows = rows.filter(entry__source_event=event)
    totals = rows.aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return (totals["debit"] or ZERO) - (totals["credit"] or ZERO)


def verify_application_discount_never_posts(organization: Organization) -> list[Finding]:
    """
    The application-funded discount reaches `SALES_DISCOUNT` in no amount at all.

    Σ posted `application_discount` is compared against what the discount account
    actually holds: if the two ever coincided, the funded share would have been
    posted as a restaurant discount, understating both revenue and the receivable
    by the same amount — and both figures would look internally consistent
    afterwards (ADR-028 §3).

    Stated as an equality on the restaurant share rather than as an absence,
    because "the account does not contain a number" is not testable and "the
    account contains exactly the restaurant-funded total" is.
    """
    from apps.sales.models import SalesDayLine

    day_ids = list(
        SalesDay.objects.filter(
            organization=organization, status=SalesDayStatus.POSTED
        ).values_list("pk", flat=True)
    )
    if not day_ids:
        return []
    totals = SalesDayLine.objects.filter(sales_day_id__in=day_ids).aggregate(
        restaurant=Sum("restaurant_discount"), application=Sum("application_discount")
    )
    restaurant = totals["restaurant"] or ZERO
    application = totals["application"] or ZERO

    day = SalesDay.objects.filter(pk__in=day_ids).order_by("business_date").first()
    assert day is not None  # noqa: S101 - day_ids is non-empty above
    account = _role_account(organization, SALES_DISCOUNT, day.business_date)
    if account is None:
        return [
            _advisory(
                "sales_discount_role_is_not_mapped",
                "SALES_DISCOUNT has no mapped account, so the discount split cannot be checked.",
            )
        ]

    # As in `verify_revenue_is_gross`: reversing mirrors are included so a
    # reversed day contributes nothing to either side of the equality.
    posted = _account_net_from_sources(organization, account.pk, sources=(DAY_SOURCE,))
    findings: list[Finding] = []
    if quantize_money(restaurant) != quantize_money(posted):
        findings.append(
            _error(
                "sales_discount_does_not_equal_the_restaurant_share",
                f"{account.code}: restaurant-funded {quantize_money(restaurant)}, "
                f"posted {quantize_money(posted)}. Application-funded "
                f"{quantize_money(application)} must reach this account in no amount.",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 4. Adjustments
# ---------------------------------------------------------------------------


def verify_adjustment_journals(organization: Organization) -> list[Finding]:
    """
    An adjustment journal exists, and it touches `SALES_REVENUE` in no amount.

    Debiting revenue would restate a posted gross figure and destroy the whole
    point of ADR-027 §2 — that revenue is gross and every deduction sits beside
    it as an identifiable claim. `SALES_RETURNS` exists for exactly this, and is
    kept apart from `SALES_DISCOUNT` because a discount is a pricing decision
    made before the sale and a return is a sale that stopped being one after it.
    """
    findings: list[Finding] = []
    adjustments = SalesAdjustment.objects.filter(
        organization=organization,
        status__in=[SalesAdjustmentStatus.POSTED, SalesAdjustmentStatus.REVERSED],
    ).order_by("business_date", "pk")

    revenue_ids: set[int] = set()
    for adjustment in adjustments:
        entry = _entry_for(organization, ADJUSTMENT_SOURCE, str(adjustment.public_id))
        if entry is None:
            findings.append(
                _error(
                    "sales_adjustment_has_no_journal",
                    f"{adjustment.number or adjustment.pk}: posted with no journal "
                    f"at {ADJUSTMENT_SOURCE}.",
                )
            )
            continue
        revenue = _role_account(organization, SALES_REVENUE, adjustment.business_date)
        if revenue is not None:
            revenue_ids.add(revenue.pk)
        touched = set(_nets_by_account(entry))
        if revenue is not None and revenue.pk in touched:
            findings.append(
                _error(
                    "sales_adjustment_touches_revenue",
                    f"{adjustment.number or adjustment.pk}: journal reaches "
                    f"{revenue.code}. A return posts to SALES_RETURNS; revenue stays gross.",
                )
            )
        # Every channel override is a revenue account too, and an adjustment
        # must reach none of them either.
        for account_id in touched:
            if account_id in revenue_ids and account_id != (revenue.pk if revenue else None):
                findings.append(
                    _error(
                        "sales_adjustment_touches_revenue",
                        f"{adjustment.number or adjustment.pk}: journal reaches a revenue account.",
                    )
                )
    return findings


def verify_adjustments_are_within_their_originals(organization: Organization) -> list[Finding]:
    """
    No original line has more taken back from it than was sold off it.

    A database trigger already refuses this on INSERT and UPDATE. Re-checking it
    is not redundant: the trigger guarantees rows written after it, and a
    verifier is how anybody learns that something predates it or that a
    restore replayed around it.
    """
    findings: list[Finding] = []
    claimed = (
        SalesAdjustmentLine.objects.filter(
            adjustment__organization=organization,
            adjustment__status=SalesAdjustmentStatus.POSTED,
        )
        .values("original_line_id")
        .annotate(quantity=Sum("adjusted_quantity"), gross=Sum("adjusted_gross"))
    )
    from apps.sales.models import SalesDayLine

    originals = {
        row.pk: row
        for row in SalesDayLine.objects.filter(
            pk__in=[entry["original_line_id"] for entry in claimed]
        ).select_related("sales_day", "menu_item")
    }
    for entry in claimed:
        original = originals.get(entry["original_line_id"])
        if original is None:  # pragma: no cover - a PROTECT FK guarantees it
            continue
        label = (
            f"{original.sales_day.number or original.sales_day_id} "
            f"line {original.sequence} {original.menu_item.code}"
        )
        if (entry["quantity"] or ZERO) > original.quantity:
            findings.append(
                _error(
                    "sales_adjustment_exceeds_the_original_quantity",
                    f"{label}: adjusted {entry['quantity']} of {original.quantity}.",
                )
            )
        if (entry["gross"] or ZERO) > original.gross_amount:
            findings.append(
                _error(
                    "sales_adjustment_exceeds_the_original_gross",
                    f"{label}: adjusted {entry['gross']} of {original.gross_amount}.",
                )
            )
    return findings


def verify_theoretical_quantities(organization: Organization) -> list[Finding]:
    """
    Only a cancellation reduces the quantity the kitchen is measured against.

    The check is the asymmetry itself: the cancelled map must contain exactly the
    posted `CANCELLED_BEFORE_FULFILLMENT` quantities, and nothing from a return
    or a financial correction may appear in it.

    The intuitive implementation — subtract every adjustment — reads perfectly
    well, is one filter shorter than the correct one, and manufactures an
    unexplained usage variance of exactly the returned quantity in every branch
    that ever takes a plate back (ADR-028 §8). This check exists because that
    regression would otherwise be invisible.
    """
    from apps.sales.consumption_source import cancelled_quantities
    from apps.sales.models import (
        SalesAdjustmentReasonKind,
        SalesDayLine,
    )

    line_ids = list(
        SalesDayLine.objects.filter(
            sales_day__organization=organization, sales_day__status=SalesDayStatus.POSTED
        ).values_list("pk", flat=True)
    )
    if not line_ids:
        return []

    reported = cancelled_quantities(line_ids)
    posted = SalesAdjustmentLine.objects.filter(
        original_line_id__in=line_ids, adjustment__status=SalesAdjustmentStatus.POSTED
    )
    expected = {
        row["original_line_id"]: row["total"] or ZERO
        for row in posted.filter(
            adjustment__reason_kind=SalesAdjustmentReasonKind.CANCELLED_BEFORE_FULFILLMENT
        )
        .values("original_line_id")
        .annotate(total=Sum("adjusted_quantity"))
    }
    #: What the *wrong* implementation would produce — every posted adjustment,
    #: one filter short. Kept so the finding can name the regression rather than
    #: only report a mismatch, because "adapter reports more than the
    #: cancellations" and "somebody deleted the reason-kind filter" send a
    #: reader to two different places.
    everything = {
        row["original_line_id"]: row["total"] or ZERO
        for row in posted.values("original_line_id").annotate(total=Sum("adjusted_quantity"))
    }

    findings: list[Finding] = []
    for line_id in sorted(set(reported) | set(expected)):
        mine = reported.get(line_id, ZERO)
        cancelled = expected.get(line_id, ZERO)
        if mine == cancelled:
            continue
        if mine == everything.get(line_id, ZERO):
            findings.append(
                _error(
                    "returns_reduced_the_theoretical_quantity",
                    f"line {line_id}: the adapter is subtracting every posted "
                    f"adjustment ({mine}) rather than the cancellations alone "
                    f"({cancelled}). A return was cooked; subtracting it invents "
                    "an unexplained usage variance of exactly that quantity.",
                )
            )
            continue
        findings.append(
            _error(
                "cancelled_quantity_does_not_match_posted_cancellations",
                f"line {line_id}: adapter reports {mine}, posted cancellations total {cancelled}.",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 5. The receivable subledger
# ---------------------------------------------------------------------------


def verify_receivable_ledger(organization: Organization) -> list[Finding]:
    """
    The subledger equals its control account, and every entry names a document.

    Two checks, and both are about the same claim: `ApplicationReceivableEntry`
    is the *only* statement of what a delivery company owes — there is no balance
    field anywhere in this module — so if it disagrees with the general ledger,
    one of the two is fiction and the disagreement always surfaces mid-argument
    with the counterparty (ADR-027 §5).

    `AUTHORIZED_ADJUSTMENT` entries are matched on the `str(public_id)` **prefix**
    rather than on equality, because a reversal names itself in the one field the
    canonicaliser does not case-fold, by appending `:REVERSED` to the id. ADR-027
    §5 fixes the source vocabulary at five values and there is no
    `ADJUSTMENT_REVERSED`, so the document id is the only component left.

    The comparison is made **per control account, not per application**, and that
    is the whole subtlety of this check. Three applications with no override all
    reach the same `DELIVERY_APP_RECEIVABLE` account, so the general ledger holds
    one balance for all three: comparing each application's own subledger against
    that shared balance would report three failures on a set of books that is
    perfectly correct. An application whose `receivable_account` override points
    somewhere else forms its own group, which is exactly what the override means.
    """
    findings: list[Finding] = []
    applications = DeliveryApplication.objects.filter(organization=organization).select_related(
        "receivable_account"
    )
    by_account: dict[int, tuple[Account, list[str], Decimal]] = {}
    for application in applications:
        entries = ApplicationReceivableEntry.objects.filter(
            organization=organization, delivery_application=application
        )
        if not entries.exists():
            continue
        totals = entries.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        subledger = (totals["debit"] or ZERO) - (totals["credit"] or ZERO)

        first = entries.order_by("business_date").first()
        assert first is not None  # noqa: S101 - existence checked above
        account = application.receivable_account or _role_account(
            organization, DELIVERY_APP_RECEIVABLE, first.business_date
        )
        if account is None:
            findings.append(
                _advisory(
                    "receivable_account_is_not_mapped",
                    f"{application.code}: no receivable account, so the subledger "
                    "cannot be compared with the general ledger.",
                )
            )
            continue
        known, codes, running = by_account.get(account.pk, (account, [], ZERO))
        codes.append(application.code)
        by_account[account.pk] = (known, codes, running + subledger)

    for account, codes, subledger in by_account.values():
        general = _account_net_from_sources(organization, account.pk, sources=SALES_SOURCES)
        if quantize_money(subledger) != quantize_money(general):
            findings.append(
                _error(
                    "receivable_subledger_disagrees_with_the_general_ledger",
                    f"{account.code} ({', '.join(sorted(codes))}): subledger "
                    f"{quantize_money(subledger)}, general ledger {quantize_money(general)}.",
                )
            )

    findings += _verify_adjustment_receivable_entries(organization)
    return findings


def _verify_adjustment_receivable_entries(organization: Organization) -> list[Finding]:
    findings: list[Finding] = []
    posted = {
        str(row.public_id)
        for row in SalesAdjustment.objects.filter(
            organization=organization,
            status__in=[SalesAdjustmentStatus.POSTED, SalesAdjustmentStatus.REVERSED],
        )
    }
    rows = ApplicationReceivableEntry.objects.filter(
        organization=organization, source=ReceivableSource.AUTHORIZED_ADJUSTMENT
    )
    for row in rows:
        document_id = row.source_document_id
        prefix = document_id.removesuffix(REVERSAL_RECEIVABLE_ID_SUFFIX)
        if prefix not in posted:
            findings.append(
                _error(
                    "receivable_adjustment_names_no_posted_document",
                    f"entry {row.pk}: {document_id} has no posted SalesAdjustment behind it.",
                )
            )
        if row.source_document_type != ADJUSTMENT_SOURCE:
            findings.append(
                _error(
                    "receivable_adjustment_names_the_wrong_source_type",
                    f"entry {row.pk}: {row.source_document_type} is not {ADJUSTMENT_SOURCE}.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 6. Settlements
# ---------------------------------------------------------------------------


def verify_settlement_journals(organization: Organization) -> list[Finding]:
    """
    A settlement journal exists and contains **no** commission line.

    This is the double-recognition check ADR-028 §6 asks for by name. Commission
    was accrued at the sale; expensing it again at settlement overstates selling
    expense and understates gross margin by the same amount, and both figures are
    individually defensible afterwards — which is why nobody finds it by reading.
    """
    findings: list[Finding] = []
    for settlement in DeliveryApplicationSettlement.objects.filter(
        organization=organization,
        status__in=[SettlementStatus.POSTED, SettlementStatus.REVERSED],
    ).order_by("business_date", "pk"):
        entry = _entry_for(organization, SETTLEMENT_SOURCE, str(settlement.public_id))
        if entry is None:
            findings.append(
                _error(
                    "settlement_has_no_journal",
                    f"{settlement.number or settlement.pk}: posted with no journal "
                    f"at {SETTLEMENT_SOURCE}.",
                )
            )
            continue
        commission = _role_account(
            organization, DELIVERY_COMMISSION_EXPENSE, settlement.business_date
        )
        if commission is not None and commission.pk in _nets_by_account(entry):
            findings.append(
                _error(
                    "settlement_recognises_commission_twice",
                    f"{settlement.number or settlement.pk}: journal reaches "
                    f"{commission.code}. Commission was accrued at the sale; a "
                    "statement disagreement is a variance, not a second expense.",
                )
            )
    return findings


def verify_settlement_allocations(organization: Organization) -> list[Finding]:
    """
    Every posted settlement's claims still add up, and nothing is paid twice.

    Three equalities:

    * `Σ allocations == expected_amount` — the journal credits the stamped
      figure, so the stamp and the claims must still be the same number.
    * both leg equations — every dinar of each gap is claimed by an adjustment
      carrying that leg (ADR-028 §7).
    * no receivable entry has more allocated against it, across posted
      settlements, than it owes.
    """
    from apps.sales.models import DeliveryApplicationSettlementAllocation
    from apps.sales.settlement_services import allocated_total, three_way_for

    findings: list[Finding] = []
    for settlement in DeliveryApplicationSettlement.objects.filter(
        organization=organization, status=SettlementStatus.POSTED
    ).order_by("business_date", "pk"):
        label = settlement.number or str(settlement.pk)
        allocated = allocated_total(settlement)
        if quantize_money(allocated) != quantize_money(settlement.expected_amount):
            findings.append(
                _error(
                    "settlement_allocations_do_not_equal_expected",
                    f"{label}: allocations {quantize_money(allocated)}, "
                    f"expected {quantize_money(settlement.expected_amount)}.",
                )
            )
        three_way = three_way_for(settlement)
        if three_way.unexplained_statement != ZERO:
            findings.append(
                _error(
                    "settlement_statement_leg_is_unexplained",
                    f"{label}: {three_way.unexplained_statement} of the statement gap "
                    "is claimed by no adjustment.",
                )
            )
        if three_way.unexplained_remittance != ZERO:
            findings.append(
                _error(
                    "settlement_remittance_leg_is_unexplained",
                    f"{label}: {three_way.unexplained_remittance} of the remittance gap "
                    "is claimed by no adjustment.",
                )
            )

    over = (
        DeliveryApplicationSettlementAllocation.objects.filter(
            settlement__organization=organization,
            settlement__status=SettlementStatus.POSTED,
        )
        .values("receivable_entry_id")
        .annotate(allocated=Sum("allocated_amount"))
    )
    entries = {
        row.pk: row
        for row in ApplicationReceivableEntry.objects.filter(
            pk__in=[item["receivable_entry_id"] for item in over]
        ).select_related("delivery_application")
    }
    for item in over:
        entry = entries.get(item["receivable_entry_id"])
        if entry is None:  # pragma: no cover - a PROTECT FK guarantees it
            continue
        if (item["allocated"] or ZERO) > entry.debit:
            findings.append(
                _error(
                    "receivable_entry_is_over_allocated",
                    f"{entry.delivery_application.code} entry {entry.pk}: allocated "
                    f"{item['allocated']} against a debit of {entry.debit}.",
                )
            )
    return findings


def verify_settlement_commission(organization: Organization) -> list[Finding]:
    """
    The accrued commission against the statement's, reported as an **ADVISORY**.

    Never an error, and that is a deliberate limit on this verifier's authority.
    A rate dispute is a commercial fact between two companies; a verifier that
    failed on one would be red every month and would therefore be ignored every
    month. What the system owes here is visibility, not a verdict.
    """
    from apps.sales.settlement_services import three_way_for

    findings: list[Finding] = []
    for settlement in DeliveryApplicationSettlement.objects.filter(
        organization=organization, status=SettlementStatus.POSTED
    ).order_by("business_date", "pk"):
        three_way = three_way_for(settlement)
        if three_way.commission_gap != ZERO:
            findings.append(
                _advisory(
                    "settlement_commission_gap",
                    f"{settlement.number or settlement.pk} "
                    f"{settlement.delivery_application.code}: accrued "
                    f"{three_way.accrued_commission}, statement "
                    f"{three_way.statement_commission}, gap {three_way.commission_gap}.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 7. The till
# ---------------------------------------------------------------------------


def verify_shift_journals(organization: Organization) -> list[Finding]:
    """
    A shift journal touches exactly two accounts, and the zero case has none.

    `SALES_CASH_OVER_SHORT` and `SALES_CASH_ON_HAND`, and **nothing else** — no
    revenue, no card clearing, no receivable. The intuitive design, in which the
    closing records the day's takings, would double every cash sales figure in
    the system and the duplication would be invisible because both entries would
    name a real document (ADR-027 §8).

    A variance of exactly zero posts no journal at all and still takes a number.
    That is a legitimate outcome rather than a missing journal, which is why this
    check reads the variance before it decides what to expect — the same
    distinction `verify_production` makes for a batch that rightly posts nothing.
    """
    findings: list[Finding] = []
    for shift in CashierShift.objects.filter(
        organization=organization,
        status__in=[CashierShiftStatus.APPROVED, CashierShiftStatus.REVERSED],
    ).order_by("business_date", "pk"):
        label = shift.number or str(shift.pk)
        entry = _entry_for(organization, SHIFT_SOURCE, str(shift.public_id))
        if shift.variance_amount == ZERO:
            if entry is not None:
                findings.append(
                    _error(
                        "cashier_shift_posted_a_journal_for_no_variance",
                        f"{label}: variance is zero and a journal exists.",
                    )
                )
            continue
        if entry is None:
            findings.append(
                _error(
                    "cashier_shift_has_no_journal",
                    f"{label}: variance {shift.variance_amount} with no journal at {SHIFT_SOURCE}.",
                )
            )
            continue

        over_short = _role_account(organization, SALES_CASH_OVER_SHORT, shift.business_date)
        cash = _role_account(organization, SALES_CASH_ON_HAND, shift.business_date)
        expected = {account.pk for account in (over_short, cash) if account is not None}
        touched = set(_nets_by_account(entry))
        if touched != expected:
            names = {
                account.pk: account.code
                for account in Account.objects.filter(pk__in=touched | expected)
            }
            findings.append(
                _error(
                    "cashier_shift_journal_touches_the_wrong_accounts",
                    f"{label}: touched {sorted(names.get(pk, str(pk)) for pk in touched)}, "
                    f"expected {sorted(names.get(pk, str(pk)) for pk in expected)}.",
                )
            )
    return findings


def verify_shift_counts(organization: Organization) -> list[Finding]:
    """
    The stamped variance is still `counted − expected`, and maker is not checker.

    Both are enforced elsewhere — a service check and a database constraint for
    the second — and both are re-checked here for the reason the over-adjustment
    check is: a constraint guarantees rows written after it, and a verifier is
    how anybody learns that something predates it.

    A shift closed but not yet approved is a `COVERAGE_LIMITATION`, not a
    finding. A drawer waiting for a second person is the normal state of a
    drawer at nine in the evening.
    """
    findings: list[Finding] = []
    shifts = CashierShift.objects.filter(organization=organization).select_related(
        "branch", "closed_by", "approved_by"
    )
    for shift in shifts.order_by("business_date", "pk"):
        label = f"{shift.branch.code} {shift.business_date.isoformat()}"
        if shift.status in {CashierShiftStatus.APPROVED, CashierShiftStatus.REVERSED}:
            expected = quantize_money(shift.counted_cash - shift.expected_cash)
            if shift.variance_amount != expected:
                findings.append(
                    _error(
                        "cashier_shift_variance_does_not_reconstruct",
                        f"{label}: counted {shift.counted_cash} − expected "
                        f"{shift.expected_cash} = {expected}, stored {shift.variance_amount}.",
                    )
                )
            if shift.approved_by_id is not None and shift.approved_by_id == shift.closed_by_id:
                findings.append(
                    _error(
                        "cashier_shift_approver_is_the_closer",
                        f"{label}: {shift.approved_by} both closed and approved it.",
                    )
                )
        if shift.status == CashierShiftStatus.CLOSED:
            findings.append(
                _limitation(
                    "cashier_shift_is_not_approved_yet",
                    f"{label}: counted and closed, awaiting a second person. Its "
                    "variance has not reached the ledger.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 8. Source identity and idempotency
# ---------------------------------------------------------------------------


def verify_source_identity(organization: Organization) -> list[Finding]:
    """
    Every sales journal carries a complete source identity, and no key repeats.

    ADR-017's rule is all-or-nothing: an upstream-generated journal carries
    organization, type, id and `SourceEvent`, or none of it. A journal carrying
    three of the four cannot be found by the reversal that needs it, and the
    failure surfaces as "the journal for this document is missing" months later.

    The type is also checked for **case**, which is the trap this module has
    already fallen into once: `canonical_source_identity` upper-cases the type
    before persisting it, so a constant spelled `sales.SalesDay` writes
    `SALES.SALESDAY` and then fails to find itself.
    """
    findings: list[Finding] = []
    entries = JournalEntry.objects.filter(
        organization=organization, source_document_type__in=list(SALES_SOURCES)
    )
    for entry in entries.order_by("accounting_date", "pk"):
        if not entry.source_document_id or not entry.source_event:
            findings.append(
                _error(
                    "sales_journal_has_a_partial_source_identity",
                    f"{entry.entry_number}: type {entry.source_document_type!r}, id "
                    f"{entry.source_document_id!r}, event {entry.source_event!r}.",
                )
            )
        if entry.source_document_type != entry.source_document_type.upper():
            findings.append(
                _error(
                    "sales_journal_source_type_is_not_upper_case",
                    f"{entry.entry_number}: {entry.source_document_type!r} is stored "
                    "case-folded, so a lookup for it cannot find it.",
                )
            )

    keys = (
        JournalEntry.objects.filter(
            organization=organization, source_document_type__in=list(SALES_SOURCES)
        )
        .exclude(idempotency_key="")
        .values("idempotency_key")
        .annotate(seen=Count("pk"))
    )
    for row in keys:
        if row["seen"] > 1:
            findings.append(
                _error(
                    "sales_idempotency_key_is_not_unique",
                    f"{row['idempotency_key']} appears on {row['seen']} journals.",
                )
            )

    #: A stray journal at a sales source identity with no document behind it.
    findings += _verify_no_orphan_journals(organization)
    return findings


def _verify_no_orphan_journals(organization: Organization) -> list[Finding]:
    known: dict[str, set[str]] = {
        DAY_SOURCE: {
            str(value)
            for value in SalesDay.objects.filter(organization=organization).values_list(
                "public_id", flat=True
            )
        },
        ADJUSTMENT_SOURCE: {
            str(value)
            for value in SalesAdjustment.objects.filter(organization=organization).values_list(
                "public_id", flat=True
            )
        },
        SETTLEMENT_SOURCE: {
            str(value)
            for value in DeliveryApplicationSettlement.objects.filter(
                organization=organization
            ).values_list("public_id", flat=True)
        },
        SHIFT_SOURCE: {
            str(value)
            for value in CashierShift.objects.filter(organization=organization).values_list(
                "public_id", flat=True
            )
        },
    }
    findings: list[Finding] = []
    for source, identities in known.items():
        strays = (
            JournalEntry.objects.filter(organization=organization, source_document_type=source)
            .exclude(source_document_id__in=identities)
            .values_list("entry_number", "source_document_id")
        )
        for number, document_id in strays:
            findings.append(
                _error(
                    "sales_journal_names_no_document",
                    f"{number}: {source} {document_id} matches no document in this organization.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 9. Permissions
# ---------------------------------------------------------------------------


#: The seventeen the module promised. Asserted as a number as well as a set,
#: because a table that grew a row and a name that was never granted are two
#: different mistakes and the count catches the first.
EXPECTED_PERMISSION_COUNT = 17


def verify_permission_scope() -> list[Finding]:
    """
    Every declared name is granted, every grant has a scope, and there are 17.

    `apps/sales/permissions.py` says in its own docstring that a name is not a
    grant and that a name whose checkpoint has not landed appears in neither
    `ALL_PERMISSIONS` nor any role. By checkpoint 7 every checkpoint has landed,
    so the two sets must finally coincide — and this is where that promise is
    kept rather than assumed.

    The migrated codenames are checked too. A permission in `_TABLE` with no
    migration behind it makes `sync_role_groups` fail at deploy time, which is a
    worse place to learn it than here.
    """
    from django.contrib.auth.models import Permission

    from apps.sales import permissions as sales_permissions
    from apps.sales.permissions import ALL_PERMISSIONS, PERMISSION_SCOPE, ROLE_PERMISSIONS

    findings: list[Finding] = []
    if len(ALL_PERMISSIONS) != EXPECTED_PERMISSION_COUNT:
        findings.append(
            _error(
                "sales_permission_count_changed",
                f"_TABLE holds {len(ALL_PERMISSIONS)} rows; the module declares "
                f"{EXPECTED_PERMISSION_COUNT}.",
            )
        )

    declared = {
        value
        for name, value in vars(sales_permissions).items()
        if name.isupper() and isinstance(value, str) and value.startswith("sales.")
    }
    granted = set(ALL_PERMISSIONS)
    for name in sorted(declared - granted):
        findings.append(
            _error(
                "sales_permission_is_named_but_never_granted",
                f"{name} appears in the Names block and in no row of _TABLE.",
            )
        )
    for name in sorted(granted - declared):  # pragma: no cover - a typo, not a state
        findings.append(
            _error(
                "sales_permission_is_granted_but_never_named",
                f"{name} is in _TABLE and in no constant.",
            )
        )
    for name in sorted(granted):
        if name not in PERMISSION_SCOPE:  # pragma: no cover - derived from the same table
            findings.append(
                _error("sales_permission_has_no_scope", f"{name} carries no PermissionScope.")
            )

    migrated = {
        f"sales.{codename}"
        for codename in Permission.objects.filter(content_type__app_label="sales").values_list(
            "codename", flat=True
        )
    }
    for name in sorted(granted - migrated):
        findings.append(
            _error(
                "sales_permission_is_not_migrated",
                f"{name} is granted by a role and has no migration behind it; "
                "sync_role_groups would fail on it.",
            )
        )

    if not any(ROLE_PERMISSIONS.values()):  # pragma: no cover - the table is non-empty
        findings.append(_error("sales_roles_carry_nothing", "no role carries a sales permission."))
    return findings


# ---------------------------------------------------------------------------
# 10. Daily reconciliation and coverage
# ---------------------------------------------------------------------------


def verify_daily_reconciliation(
    organization: Organization, *, date_from: datetime.date, date_to: datetime.date
) -> list[Finding]:
    """
    المطابقة اليومية, composed rather than repeated.

    `reconcile_day` already owns the leg equations and the cash comparison, and
    re-deriving them here would produce a second opinion that agrees until the
    day it does not. This walks each posted day in the window and forwards what
    the report already found.
    """
    from apps.sales.daily_reconciliation import reconcile_day

    findings: list[Finding] = []
    days = SalesDay.objects.filter(
        organization=organization,
        status=SalesDayStatus.POSTED,
        business_date__gte=date_from,
        business_date__lte=date_to,
    ).select_related("branch")
    for day in days.order_by("business_date", "branch__code"):
        row = reconcile_day(sales_day=day)
        label = f"{day.branch.code} {day.business_date.isoformat()}"
        findings += [
            Finding(
                severity=finding.severity, code=finding.code, message=f"{label}: {finding.message}"
            )
            for finding in row.findings
        ]
    return findings


def verify_coverage() -> list[Finding]:
    """
    The `SALES` adapter is registered, so the kitchen no longer reports it absent.

    Phase 3 shipped `TheoreticalSourceType.SALES` with **no** adapter, and every
    theoretical figure carried `SALES_NOT_INCLUDED_PHASE_4` beside it as a
    result. Checkpoint 3 registered the adapter; this asserts that the coverage
    code has actually changed, because a registration that silently failed at
    app-ready would leave every kitchen report honestly saying the wrong thing.
    """
    from apps.kitchen.consumption_sources import (
        SALES_NOT_INCLUDED,
        coverage_code,
        sales_source_is_registered,
    )

    findings: list[Finding] = []
    if not sales_source_is_registered():
        findings.append(
            _error(
                "sales_quantity_source_is_not_registered",
                "apps.sales is installed and the SALES theoretical source is not "
                "registered; every kitchen theoretical figure is still partial.",
            )
        )
        return findings
    if coverage_code() == SALES_NOT_INCLUDED:
        findings.append(
            _error(
                "coverage_still_reports_sales_as_absent",
                f"the SALES adapter is registered and coverage_code() still returns "
                f"{SALES_NOT_INCLUDED}.",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# What was looked at
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Counts:
    """How much this organization actually holds, for the `checked:` lines."""

    menu_items: int
    prices: int
    posted_days: int
    posted_lines: int
    adjustments: int
    receivable_entries: int
    settlements: int
    shifts: int


def counts_for(organization: Organization) -> Counts:
    from apps.sales.models import SalesDayLine

    posted = SalesDay.objects.filter(organization=organization, status=SalesDayStatus.POSTED)
    return Counts(
        menu_items=MenuItem.objects.filter(organization=organization).count(),
        prices=MenuPriceVersion.objects.filter(menu_item__organization=organization).count(),
        posted_days=posted.count(),
        posted_lines=SalesDayLine.objects.filter(sales_day__in=posted).count(),
        adjustments=SalesAdjustment.objects.filter(
            organization=organization,
            status__in=[SalesAdjustmentStatus.POSTED, SalesAdjustmentStatus.REVERSED],
        ).count(),
        receivable_entries=ApplicationReceivableEntry.objects.filter(
            organization=organization
        ).count(),
        settlements=DeliveryApplicationSettlement.objects.filter(
            organization=organization,
            status__in=[SettlementStatus.POSTED, SettlementStatus.REVERSED],
        ).count(),
        shifts=CashierShift.objects.filter(organization=organization)
        .filter(Q(status=CashierShiftStatus.APPROVED) | Q(status=CashierShiftStatus.REVERSED))
        .count(),
    )


__all__ = [
    "ADVISORY",
    "COVERAGE_LIMITATION",
    "ERROR",
    "EXPECTED_PERMISSION_COUNT",
    "SALES_SOURCES",
    "Counts",
    "Finding",
    "counts_for",
    "verify_adjustment_journals",
    "verify_adjustments_are_within_their_originals",
    "verify_application_discount_never_posts",
    "verify_coverage",
    "verify_daily_reconciliation",
    "verify_day_journals",
    "verify_discount_funding",
    "verify_line_arithmetic",
    "verify_menu",
    "verify_permission_scope",
    "verify_prices",
    "verify_receivable_ledger",
    "verify_revenue_is_gross",
    "verify_settlement_allocations",
    "verify_settlement_commission",
    "verify_settlement_journals",
    "verify_shift_counts",
    "verify_shift_journals",
    "verify_source_identity",
    "verify_theoretical_quantities",
]
