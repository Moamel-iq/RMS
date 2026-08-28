r"""
The Sales demo scenario, built through the real services and nothing else.

`docs/development/demo-data-policy.md` is the contract this file keeps. The two
halves of it that bite hardest here:

* **Nothing is inserted directly.** Every posted document goes through the
  service the API and the UI call — `post_sales_day`, `post_sales_adjustment`,
  `post_settlement`, `approve_cashier_shift`, `reverse_sales_day`. There is no
  `JournalEntry.objects.create` anywhere in this module and there must never
  be. A journal written by hand is a journal no posting rule produced, and it
  would show the screens working while proving nothing.
* **A seed that renders empty is a seed that failed** (Task 3.8's lesson). The
  scenario below is sized so every Sales screen has rows on it: twelve
  navigation sections, the dashboard's eight cards, and the two reports.

## Fixed dates, not relative ones

`ANCHOR` is a literal date and every document is offset from it. A relative
anchor would make a second run tomorrow create a *second* set of sales days —
one per branch per calendar date, forever — because `SalesDay` is unique per
branch and business date, and yesterday's document is not this morning's.

The cost is that the dashboard's default fortnight eventually stops covering the
scenario. That is paid for in the command's output, which prints the dashboard
URL **with the dates already in it**, rather than in the data, where the drift
would be silent.

## The three actors, and why there are three

A cashier who counts, a manager who approves, and an accounting manager who
settles. Not one user wearing three hats:
`sales_shift_approver_is_not_the_closer` is a database constraint, so a demo
that closed and approved a shift with the same person would be refused —
correctly — and the whole seed would roll back with it. Each is a data actor
with an unusable password, exactly as the kitchen demo's four reviewers are.

## What the scenario deliberately contains

| Shape | Why it is here |
|---|---|
| four channel categories, three tenders | cash, card and receivable all reach different accounts |
| three fictional applications, three commission bases | 15% of gross, 10% after all discounts, a fixed fee per order |
| a restaurant-funded, an application-funded and a shared discount | the funding split is the module's most consequential arithmetic |
| a posted day, a reversed day, a draft day | every status the list screen can show |
| one adjustment of each reason kind | including the `FINANCIAL_CORRECTION` that moves money and no quantity |
| a settlement posted with a claimed gap on **each** leg | ADR-028 §7's three-way comparison, with nothing netted |
| a settlement reconciled and left unposted, carrying an `UNEXPLAINED_APPROVED` claim | the escape hatch, wearing its explanation and its approver |
| a cashier shift with a small **shortage** | a variance of exactly zero would demonstrate nothing |

The applications are invented. `DEMO-APP-ALPHA`, `-BETA` and `-GAMMA` are not
companies, their rates are not anybody's contract, and nothing here may be
quoted as a commercial term (spec §8.6).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from apps.accounting.models import (
    DELIVERY_APP_RECEIVABLE,
    DELIVERY_COMMISSION_EXPENSE,
    DELIVERY_OTHER_FEE_EXPENSE,
    DELIVERY_SETTLEMENT_VARIANCE,
    SALES_CARD_CLEARING,
    SALES_CASH_ON_HAND,
    SALES_CASH_OVER_SHORT,
    SALES_DISCOUNT,
    SALES_RETURNS,
    SALES_REVENUE,
    SALES_SETTLEMENT_BANK,
    Account,
    AccountRole,
    CostCenter,
    FiscalYear,
    OrganizationAccountMapping,
)
from apps.accounting.services import create_account_mapping, open_fiscal_year
from apps.organizations.models import BranchMembership, OrganizationMembership, Role
from apps.organizations.services import grant_branch_access, grant_organization_access
from apps.sales.adjustment_posting import post_sales_adjustment
from apps.sales.adjustment_services import add_adjustment_line, create_sales_adjustment
from apps.sales.day_services import (
    add_sales_line,
    create_sales_day,
    set_tender_summary,
    submit_sales_day,
    totals_for,
)
from apps.sales.models import (
    CashierShift,
    CommissionBasis,
    DeliveryAgreement,
    DeliveryApplication,
    DeliveryApplicationBranchSetting,
    DeliveryApplicationSettlement,
    DiscountProgram,
    MenuCategory,
    MenuItem,
    MenuItemBranchSetting,
    ReceivableSource,
    SalesAdjustment,
    SalesAdjustmentReasonKind,
    SalesChannel,
    SalesChannelCategory,
    SalesDay,
    SalesDayStatus,
    SettlementAdjustmentReason,
    SettlementRemittance,
    SettlementVarianceLeg,
    TenderDestination,
)
from apps.sales.posting import post_sales_day, reverse_sales_day
from apps.sales.services import (
    create_delivery_agreement,
    create_delivery_application,
    create_discount_program,
    create_menu_category,
    create_menu_item,
    create_menu_price,
    create_sales_channel,
    set_application_branch_setting,
    set_branch_availability,
)
from apps.sales.settlement_posting import post_settlement
from apps.sales.settlement_services import (
    add_settlement_adjustment,
    allocate_entry,
    create_settlement,
    reconcile_settlement,
)
from apps.sales.shift_posting import approve_cashier_shift
from apps.sales.shift_services import close_cashier_shift, open_cashier_shift, set_tender_count
from apps.users.models import User

if TYPE_CHECKING:
    from apps.organizations.models import Branch, Organization

ZERO = Decimal("0")

#: Every record this scenario creates carries it, in a name, a note, a reason or
#: an evidence reference. A demo row that does not say so is a demo row somebody
#: will screenshot.
DEMO_BANNER = "تجريبي — غير معتمد للإنتاج"

#: The namespace, ending in a version. A later scenario that needs materially
#: different postings takes `V2`; it does not mutate what `V1` posted, because a
#: demo posting is ledger history and ledger history is append-only.
DEMO_NAMESPACE = "DEMO-SALES-V1"

DEMO_CODE_PREFIX = "DEMO-"

#: An obviously fictional reference. Nobody could mistake it for a statement
#: number a delivery company actually issued.
DEMO_EVIDENCE = f"{DEMO_NAMESPACE}/NOT-A-REAL-DOCUMENT"

#: Fixed, for the reason in the module docstring. Chosen after the demo recipe
#: versions become effective (2026-06-01) so every line resolves a real version.
ANCHOR = datetime.date(2026, 8, 10)
MASTER_EFFECTIVE = datetime.date(2026, 6, 1)

POSTED_DATE = ANCHOR
REVERSED_DATE = ANCHOR + datetime.timedelta(days=2)
DRAFT_DATE = ANCHOR + datetime.timedelta(days=3)
ADJUSTMENT_DATE = ANCHOR + datetime.timedelta(days=4)
SETTLEMENT_DATE = ANCHOR + datetime.timedelta(days=6)

#: The accounts the eleven Sales roles are mapped to for the demo organization.
#: The codes are the chart's own — `seed_chart_of_accounts` created them — and
#: this only says which role reaches which, which is organization policy and
#: therefore genuinely demo data.
ROLE_ACCOUNTS: tuple[tuple[str, str], ...] = (
    (SALES_REVENUE, "4-01-01-001"),
    (SALES_DISCOUNT, "4-02-01-001"),
    (SALES_RETURNS, "4-03-01-001"),
    (SALES_CASH_ON_HAND, "1-01-01-001"),
    (SALES_CARD_CLEARING, "1-01-03-001"),
    (DELIVERY_APP_RECEIVABLE, "1-02-01-009"),
    (DELIVERY_COMMISSION_EXPENSE, "6-03-01-001"),
    (DELIVERY_OTHER_FEE_EXPENSE, "6-03-01-002"),
    (DELIVERY_SETTLEMENT_VARIANCE, "7-09-05-001"),
    (SALES_SETTLEMENT_BANK, "1-01-02-001"),
    (SALES_CASH_OVER_SHORT, "7-09-06-001"),
)

#: The three data actors. Separate people because the module's controls are
#: enforced on the **actor**: a shift's approver may not be its closer, at the
#: database, for everyone.
DEMO_ACTORS: dict[str, tuple[str, str, str]] = {
    "cashier": ("demo-sales-cashier", "كاشير", "تجريبي"),
    "manager": ("demo-sales-manager", "مدير فرع", "تجريبي"),
    "accounting": ("demo-sales-accounting", "مدير محاسبة", "تجريبي"),
}

#: `(code, name, recipe code, serving code, price)`. The recipes are the
#: kitchen demo's own, and the servings are theirs — a menu item that named a
#: serving no version offers could not be sold, which is the check
#: `verify_sales` makes first.
MENU: tuple[tuple[str, str, str, str, str], ...] = (
    ("DEMO-MENU-MANDI", "مندي كامل", "DEMO-RCP-COST", "FULL", "25000"),
    ("DEMO-MENU-MANDI-HALF", "نصف مندي", "DEMO-RCP-COST", "HALF", "14000"),
    ("DEMO-MENU-DISH", "طبق اليوم", "DEMO-RCP-DISH", "ONE", "18000"),
    ("DEMO-MENU-PLATE", "وجبة جاهزة", "DEMO-RCP-PROD-PLATED", "PORTION", "9000"),
    ("DEMO-MENU-SIDE", "طبق جانبي", "DEMO-RCP-PROD", "PORTION", "4000"),
)

#: `(code, name, category, tender, cost centre)`. Four categories, because
#: the posting behaviour differs by category and a demo with one channel would
#: exercise one third of the journal.
CHANNELS: tuple[tuple[str, str, str, str, str], ...] = (
    ("DEMO-DINE-IN", "الصالة", SalesChannelCategory.DINE_IN, TenderDestination.CASH, "HALL"),
    ("DEMO-TAKEAWAY", "سفري", SalesChannelCategory.TAKEAWAY, TenderDestination.CARD, "HALL"),
    (
        "DEMO-DIRECT",
        "توصيل مباشر",
        SalesChannelCategory.DIRECT_DELIVERY,
        TenderDestination.CASH,
        "DELIVERY",
    ),
    (
        "DEMO-APPS",
        "تطبيقات التوصيل",
        SalesChannelCategory.DELIVERY_APPLICATION,
        TenderDestination.APPLICATION_RECEIVABLE,
        "DELIVERY",
    ),
)

#: `(code, name, settlement cycle days)`. Fictional companies.
APPLICATIONS: tuple[tuple[str, str, int], ...] = (
    ("DEMO-APP-ALPHA", "تطبيق ألفا (تجريبي)", 15),
    ("DEMO-APP-BETA", "تطبيق بيتا (تجريبي)", 30),
    ("DEMO-APP-GAMMA", "تطبيق غاما (تجريبي)", 45),
)

#: `(application code, percent, fixed fee, basis)`. Three shapes, because a
#: demo that showed only a percentage would leave the fixed-fee branch of
#: `commission_for` unexercised on every screen.
AGREEMENTS: tuple[tuple[str, str, str, str], ...] = (
    ("DEMO-APP-ALPHA", "15", "0", CommissionBasis.GROSS_LIST_AMOUNT),
    ("DEMO-APP-BETA", "10", "0", CommissionBasis.AFTER_ALL_DISCOUNTS),
    ("DEMO-APP-GAMMA", "0", "1500", CommissionBasis.GROSS_LIST_AMOUNT),
)


@dataclass
class SalesDemo:
    """What the run built, and what it found already built."""

    organization: Organization
    branch: Branch
    second_branch: Branch
    actors: dict[str, User] = field(default_factory=dict)
    menu_items: list[MenuItem] = field(default_factory=list)
    channels: dict[str, SalesChannel] = field(default_factory=dict)
    applications: dict[str, DeliveryApplication] = field(default_factory=dict)
    programs: dict[str, DiscountProgram] = field(default_factory=dict)
    posted_day: SalesDay | None = None
    reversed_day: SalesDay | None = None
    draft_day: SalesDay | None = None
    adjustments: list[SalesAdjustment] = field(default_factory=list)
    settlements: list[DeliveryApplicationSettlement] = field(default_factory=list)
    shift: CashierShift | None = None
    created: int = 0
    reused: int = 0

    def note(self, *, made: bool) -> None:
        if made:
            self.created += 1
        else:
            self.reused += 1


# ---------------------------------------------------------------------------
# Foundations the scenario needs before it can post anything
# ---------------------------------------------------------------------------


def ensure_demo_actors(*, organization: Organization, branch: Branch) -> dict[str, User]:
    """
    The three signatories, created once and reused.

    Memberships are granted so the demo can be *reviewed* by signing in as each
    of them and seeing what they may and may not do — a cashier who cannot post
    a day, a manager who cannot settle a statement. The services themselves
    check no permission; the screens and the API do, and those are what a
    reviewer opens.
    """
    grants = {
        "cashier": (Role.CASHIER, False),
        "manager": (Role.MANAGER, False),
        "accounting": (Role.ACCOUNTING_MANAGER, True),
    }
    people: dict[str, User] = {}
    for key, (username, first, last) in DEMO_ACTORS.items():
        user, created = User.objects.get_or_create(
            username=username,
            defaults={"first_name": first, "last_name": last, "is_active": True},
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        # Guarded by the membership that would be written rather than by a
        # flag. `grant_*_access` upserts happily, and each upsert records an
        # audit event — so an unguarded second run adds three events and nothing
        # else, which is exactly the kind of drift the idempotency test counts.
        role, organization_wide = grants[key]
        if organization_wide:
            if not OrganizationMembership.objects.filter(
                user=user, organization=organization, role=role, is_active=True
            ).exists():
                grant_organization_access(user=user, organization=organization, role=role)
        elif not BranchMembership.objects.filter(
            user=user, branch=branch, role=role, is_active=True
        ).exists():
            grant_branch_access(user=user, branch=branch, role=role)
        people[key] = User.objects.get(pk=user.pk)
    return people


def ensure_account_mappings(organization: Organization) -> int:
    """
    Map the eleven Sales roles, once. Returns how many were newly mapped.

    A missing mapping is the one failure that takes a whole seed down at the
    *last* step — `post_sales_day` resolves every account before it writes
    anything, so an unmapped `DELIVERY_OTHER_FEE_EXPENSE` fails after the menu,
    the channels, the agreements and the discounts are already built. Doing it
    first turns that into a message.
    """
    created = 0
    for role_code, account_code in ROLE_ACCOUNTS:
        role = AccountRole.objects.filter(code=role_code).first()
        if role is None:  # pragma: no cover - accounting.0015 seeds all eleven
            continue
        if OrganizationAccountMapping.objects.filter(
            organization=organization, account_role=role
        ).exists():
            continue
        account = Account.objects.filter(organization=organization, code=account_code).first()
        if account is None:
            raise DemoPreconditionError(
                f"{organization.code} has no account {account_code} for {role_code}. "
                f"Run: manage.py seed_chart_of_accounts --organization {organization.code}"
            )
        create_account_mapping(
            organization=organization,
            account_role=role,
            account=account,
            effective_from=datetime.date(MASTER_EFFECTIVE.year, 1, 1),
        )
        created += 1
    return created


def ensure_fiscal_years(organization: Organization) -> int:
    """Open the years this scenario posts into, and today's, for the reversal."""
    from django.utils import timezone

    created = 0
    for year in sorted({ANCHOR.year, timezone.localdate().year}):
        if FiscalYear.objects.filter(organization=organization, year=year).exists():
            continue
        open_fiscal_year(organization=organization, year=year)
        created += 1
    return created


class DemoPreconditionError(RuntimeError):
    """Something the scenario needs is absent, and guessing would be worse."""


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


def _cost_center(organization: Organization, code: str) -> CostCenter:
    center = CostCenter.objects.filter(organization=organization, code=code).first()
    if center is None:
        raise DemoPreconditionError(
            f"{organization.code} has no cost centre {code}. "
            f"Run: manage.py seed_chart_of_accounts --organization {organization.code}"
        )
    return center


def _seed_menu(result: SalesDemo) -> None:
    from apps.kitchen.models import Recipe

    organization = result.organization
    category = MenuCategory.objects.filter(organization=organization, code="DEMO-MENU-MAIN").first()
    if category is None:
        category = create_menu_category(
            organization=organization,
            code="DEMO-MENU-MAIN",
            name=f"الأطباق الرئيسية — {DEMO_BANNER}",
            display_order=1,
        )
        result.note(made=True)
    else:
        result.note(made=False)

    for order, (code, name, recipe_code, serving_code, price) in enumerate(MENU, start=1):
        item = MenuItem.objects.filter(organization=organization, code=code).first()
        if item is None:
            recipe = Recipe.objects.filter(organization=organization, code=recipe_code).first()
            if recipe is None:
                raise DemoPreconditionError(
                    f"{organization.code} has no recipe {recipe_code}. "
                    "Run seed_kitchen_demo first — the menu is built on its recipes."
                )
            item = create_menu_item(
                organization=organization,
                code=code,
                name=name,
                recipe=recipe,
                serving_code=serving_code,
                category=category,
                display_order=order,
                notes=DEMO_BANNER,
            )
            result.note(made=True)
        else:
            result.note(made=False)
        result.menu_items.append(item)

        for branch in (result.branch, result.second_branch):
            # Same guard, same reason: `set_branch_availability` is an upsert and
            # records an audit event every time, whether or not it changed
            # anything.
            setting = MenuItemBranchSetting.objects.filter(menu_item=item, branch=branch).first()
            if setting is None or not setting.is_available or setting.notes != DEMO_BANNER:
                set_branch_availability(
                    item=item, branch=branch, is_available=True, notes=DEMO_BANNER
                )
            if not item.prices.filter(branch=branch).exists():
                create_menu_price(
                    menu_item=item,
                    branch=branch,
                    unit_price=Decimal(price),
                    effective_from=MASTER_EFFECTIVE,
                    evidence_reference=DEMO_EVIDENCE,
                    notes=DEMO_BANNER,
                )
                result.note(made=True)
            else:
                result.note(made=False)


def _seed_channels(result: SalesDemo) -> None:
    organization = result.organization
    for order, (code, name, category, tender, center_code) in enumerate(CHANNELS, start=1):
        channel = SalesChannel.objects.filter(organization=organization, code=code).first()
        if channel is None:
            channel = create_sales_channel(
                organization=organization,
                code=code,
                name=name,
                category=category,
                cost_center=_cost_center(organization, center_code),
                default_tender=tender,
                requires_cashier=category != SalesChannelCategory.DELIVERY_APPLICATION,
                display_order=order,
                notes=DEMO_BANNER,
            )
            result.note(made=True)
        else:
            result.note(made=False)
        result.channels[code] = channel


def _seed_applications(result: SalesDemo) -> None:
    organization = result.organization
    for code, name, cycle in APPLICATIONS:
        application = DeliveryApplication.objects.filter(
            organization=organization, code=code
        ).first()
        if application is None:
            application = create_delivery_application(
                organization=organization,
                code=code,
                name=name,
                settlement_cycle_days=cycle,
                notes=DEMO_BANNER,
            )
            result.note(made=True)
        else:
            result.note(made=False)
        result.applications[code] = application
        for branch in (result.branch, result.second_branch):
            external = f"{DEMO_NAMESPACE}/{branch.code}"
            live = DeliveryApplicationBranchSetting.objects.filter(
                delivery_application=application, branch=branch
            ).first()
            if (
                live is None
                or not live.is_active
                or live.external_store_code != external
                or live.notes != DEMO_BANNER
            ):
                set_application_branch_setting(
                    application=application,
                    branch=branch,
                    is_active=True,
                    external_store_code=external,
                    notes=DEMO_BANNER,
                )

    for application_code, percent, fee, basis in AGREEMENTS:
        application = result.applications[application_code]
        for branch in (result.branch, result.second_branch):
            if DeliveryAgreement.objects.filter(
                branch=branch, delivery_application=application
            ).exists():
                result.note(made=False)
                continue
            create_delivery_agreement(
                branch=branch,
                delivery_application=application,
                effective_from=MASTER_EFFECTIVE,
                commission_percent=Decimal(percent),
                fixed_fee_per_order=Decimal(fee),
                commission_basis=basis,
                evidence_reference=DEMO_EVIDENCE,
                notes=DEMO_BANNER,
            )
            result.note(made=True)


def _seed_discounts(result: SalesDemo) -> None:
    """
    Three programmes, and the three funding shapes ADR-028 §3 distinguishes.

    The shared one is the interesting row: half the discount is the restaurant's
    cost and half is a promise by the delivery company, and the two halves reach
    two different places — one debits `SALES_DISCOUNT`, and the other reaches no
    account at all because the company reimburses it.
    """
    organization = result.organization
    wanted: tuple[tuple[str, str, str, str, str, str | None], ...] = (
        ("DEMO-DISC-HOUSE", "خصم المطعم ١٠٪", "10", "100", "0", None),
        ("DEMO-DISC-APP", "عرض ممول من التطبيق ٢٠٪", "20", "0", "100", "DEMO-APP-ALPHA"),
        ("DEMO-DISC-SHARED", "عرض مشترك ١٥٪", "15", "50", "50", "DEMO-APP-BETA"),
    )
    for code, name, percent, restaurant, application_share, application_code in wanted:
        program = DiscountProgram.objects.filter(organization=organization, code=code).first()
        if program is None:
            program = create_discount_program(
                organization=organization,
                code=code,
                name=name,
                effective_from=MASTER_EFFECTIVE,
                discount_percent=Decimal(percent),
                restaurant_funded_share=Decimal(restaurant),
                application_funded_share=Decimal(application_share),
                delivery_application=(
                    result.applications[application_code] if application_code is not None else None
                ),
                evidence_reference=DEMO_EVIDENCE,
                notes=DEMO_BANNER,
            )
            result.note(made=True)
        else:
            result.note(made=False)
        result.programs[code] = program


# ---------------------------------------------------------------------------
# The trading days
# ---------------------------------------------------------------------------


def _declare_tenders(day: SalesDay) -> None:
    """
    Declare each tender at exactly what the lines say.

    A demo that declared a *different* figure would put a permanent advisory on
    المطابقة اليومية, and a reviewer would learn to ignore the one signal the
    screen exists to give. The variance this scenario does show is a real one: a
    counted drawer that came up short.
    """
    totals = totals_for(day)
    for tender, amount in (
        (TenderDestination.CASH, totals.net_cash),
        (TenderDestination.CARD, totals.net_card),
        (TenderDestination.APPLICATION_RECEIVABLE, totals.net_application),
    ):
        if amount != ZERO:
            set_tender_summary(day=day, tender=tender, declared_amount=amount, notes=DEMO_BANNER)


def _seed_posted_day(result: SalesDemo) -> SalesDay:
    """
    The scenario's centrepiece: six lines covering every tender and every basis.

    Built once. A second run finds the day already posted and touches nothing —
    a posted day is frozen by a database trigger, so any attempt would be
    refused anyway, and the guard is here so the refusal never happens.
    """
    existing = SalesDay.objects.filter(branch=result.branch, business_date=POSTED_DATE).first()
    if existing is not None:
        result.note(made=False)
        return existing

    manager = result.actors["manager"]
    accounting = result.actors["accounting"]
    day = create_sales_day(
        organization=result.organization,
        branch=result.branch,
        business_date=POSTED_DATE,
        actor=manager,
        notes=f"{DEMO_BANNER} · {DEMO_NAMESPACE}",
    )
    items = {item.code: item for item in result.menu_items}

    # Hall, cash, no discount.
    add_sales_line(
        day=day,
        menu_item=items["DEMO-MENU-MANDI"],
        channel=result.channels["DEMO-DINE-IN"],
        quantity=Decimal("4.000"),
        order_count=4,
        notes=DEMO_BANNER,
    )
    # Takeaway, card, restaurant-funded discount.
    add_sales_line(
        day=day,
        menu_item=items["DEMO-MENU-MANDI-HALF"],
        channel=result.channels["DEMO-TAKEAWAY"],
        quantity=Decimal("3.000"),
        order_count=3,
        discount_program=result.programs["DEMO-DISC-HOUSE"],
        notes=DEMO_BANNER,
    )
    # The restaurant's own delivery: cash, and a delivery cost centre.
    add_sales_line(
        day=day,
        menu_item=items["DEMO-MENU-DISH"],
        channel=result.channels["DEMO-DIRECT"],
        quantity=Decimal("2.000"),
        order_count=2,
        notes=DEMO_BANNER,
    )
    # ALPHA: 15% of gross, and a discount the application funds in full — so
    # the funded share reaches no account and the receivable is not reduced.
    add_sales_line(
        day=day,
        menu_item=items["DEMO-MENU-MANDI"],
        channel=result.channels["DEMO-APPS"],
        quantity=Decimal("6.000"),
        order_count=6,
        delivery_application=result.applications["DEMO-APP-ALPHA"],
        discount_program=result.programs["DEMO-DISC-APP"],
        notes=DEMO_BANNER,
    )
    # BETA: 10% after all discounts, a shared promotion, and a fee beside the
    # commission so `DELIVERY_OTHER_FEE_EXPENSE` has a line on the screen.
    add_sales_line(
        day=day,
        menu_item=items["DEMO-MENU-PLATE"],
        channel=result.channels["DEMO-APPS"],
        quantity=Decimal("10.000"),
        order_count=10,
        delivery_application=result.applications["DEMO-APP-BETA"],
        discount_program=result.programs["DEMO-DISC-SHARED"],
        other_fee_amount=Decimal("1000"),
        notes=DEMO_BANNER,
    )
    # GAMMA: no percentage at all, a fixed fee per order.
    add_sales_line(
        day=day,
        menu_item=items["DEMO-MENU-SIDE"],
        channel=result.channels["DEMO-APPS"],
        quantity=Decimal("5.000"),
        order_count=5,
        delivery_application=result.applications["DEMO-APP-GAMMA"],
        notes=DEMO_BANNER,
    )

    _declare_tenders(day)
    submit_sales_day(day=day, actor=manager)
    posted = post_sales_day(day=day, actor=accounting)
    result.note(made=True)
    return posted


def _seed_reversed_day(result: SalesDemo) -> SalesDay:
    """A day posted and then reversed, so both entries are on the ledger."""
    existing = SalesDay.objects.filter(
        branch=result.second_branch, business_date=REVERSED_DATE
    ).first()
    if existing is not None:
        result.note(made=False)
        return existing

    manager = result.actors["manager"]
    accounting = result.actors["accounting"]
    day = create_sales_day(
        organization=result.organization,
        branch=result.second_branch,
        business_date=REVERSED_DATE,
        actor=manager,
        notes=f"{DEMO_BANNER} · {DEMO_NAMESPACE}",
    )
    items = {item.code: item for item in result.menu_items}
    add_sales_line(
        day=day,
        menu_item=items["DEMO-MENU-DISH"],
        channel=result.channels["DEMO-DINE-IN"],
        quantity=Decimal("3.000"),
        order_count=3,
        notes=DEMO_BANNER,
    )
    _declare_tenders(day)
    submit_sales_day(day=day, actor=manager)
    post_sales_day(day=day, actor=accounting)
    reversed_day = reverse_sales_day(
        day=day,
        actor=accounting,
        reason=f"{DEMO_BANNER}: أُدخل اليوم على الفرع الخطأ.",
    )
    result.note(made=True)
    return reversed_day


def _seed_draft_day(result: SalesDemo) -> SalesDay:
    """One day left in `DRAFT`, so the list screen shows every status."""
    existing = SalesDay.objects.filter(branch=result.branch, business_date=DRAFT_DATE).first()
    if existing is not None:
        result.note(made=False)
        return existing

    day = create_sales_day(
        organization=result.organization,
        branch=result.branch,
        business_date=DRAFT_DATE,
        actor=result.actors["cashier"],
        notes=f"{DEMO_BANNER} · مسودة قيد الإدخال",
    )
    items = {item.code: item for item in result.menu_items}
    add_sales_line(
        day=day,
        menu_item=items["DEMO-MENU-MANDI"],
        channel=result.channels["DEMO-DINE-IN"],
        quantity=Decimal("2.000"),
        order_count=2,
        notes=DEMO_BANNER,
    )
    result.note(made=True)
    return day


# ---------------------------------------------------------------------------
# The till
# ---------------------------------------------------------------------------


#: How much the drawer is short. Small, deliberate, and **not** zero: a variance
#: of exactly zero posts no journal at all, which is a legitimate outcome and
#: demonstrates nothing about the one thing a closing may post.
DEMO_SHORTAGE = Decimal("750")
DEMO_OPENING_FLOAT = Decimal("50000")


def _seed_shift(result: SalesDemo, day: SalesDay) -> CashierShift:
    """
    A drawer counted by the cashier and approved by the manager.

    Two people, because `sales_shift_approver_is_not_the_closer` is a check
    constraint: one person doing both would be refused by the database and the
    whole seed would roll back with it.
    """
    from apps.sales.shift_services import expected_cash_for

    existing = CashierShift.objects.filter(branch=result.branch, business_date=POSTED_DATE).first()
    if existing is not None:
        result.note(made=False)
        return existing

    cashier = result.actors["cashier"]
    manager = result.actors["manager"]
    shift = open_cashier_shift(
        organization=result.organization,
        branch=result.branch,
        business_date=POSTED_DATE,
        cashier=cashier,
        opening_float=DEMO_OPENING_FLOAT,
        actor=manager,
        notes=DEMO_BANNER,
    )
    # The expectation has to be read **against the day the shift will close on**.
    # An open shift that has not named its day yet answers zero for every tender —
    # honestly, because nothing has posted through it — so counting against that
    # would declare a drawer holding only the float and stamp a variance the size
    # of the day's whole cash takings. Attached in memory only; the service
    # re-reads the row under its own lock and sets this itself.
    shift.sales_day = day
    counted = expected_cash_for(shift) - DEMO_SHORTAGE
    set_tender_count(
        shift=shift,
        tender=TenderDestination.CASH,
        counted_amount=counted,
        actor=cashier,
        notes=DEMO_BANNER,
    )
    close_cashier_shift(shift=shift, sales_day=day, actor=cashier, notes=DEMO_BANNER)
    approved = approve_cashier_shift(shift=shift, actor=manager)
    result.note(made=True)
    return approved


# ---------------------------------------------------------------------------
# Returns, cancellations and corrections
# ---------------------------------------------------------------------------


def _seed_adjustments(result: SalesDemo, day: SalesDay) -> list[SalesAdjustment]:
    """
    One posted adjustment of each reason kind, against the posted day.

    The three exist together on purpose: they post the *same* journal and differ
    only in what they may touch and in what the kitchen does with them. Only the
    cancellation reduces theoretical consumption — a returned plate was cooked,
    and subtracting it would invent a usage variance of exactly that quantity
    (ADR-028 §8). Seeing all three on one screen is the cheapest way to make that
    asymmetry visible.
    """
    lines = {line.sequence: line for line in day.lines.order_by("sequence")}
    accounting = result.actors["accounting"]
    wanted: tuple[tuple[str, str, int, str, str | None, str], ...] = (
        (
            "CANCEL",
            SalesAdjustmentReasonKind.CANCELLED_BEFORE_FULFILLMENT,
            1,
            "1.000",
            None,
            "طلب أُلغي قبل التحضير — لم تُطبخ الوجبة ولم تخرج مكوّناتها.",
        ),
        (
            "RETURN",
            SalesAdjustmentReasonKind.RETURNED_AFTER_FULFILLMENT,
            4,
            "1.000",
            None,
            "طلب أُعيد بعد التسليم — الطعام طُبخ فعلاً، فلا يُخصم من الاستهلاك النظري.",
        ),
        (
            "CORRECTION",
            SalesAdjustmentReasonKind.FINANCIAL_CORRECTION,
            3,
            "0",
            "2000",
            "تصحيح مبلغ فوترة — لا يدّعي أن كمية أقل قد بيعت.",
        ),
    )
    made: list[SalesAdjustment] = []
    for slug, kind, sequence, quantity, gross, reason in wanted:
        evidence = f"{DEMO_NAMESPACE}/ADJ-{slug}"
        existing = SalesAdjustment.objects.filter(
            organization=result.organization, evidence_reference=evidence
        ).first()
        if existing is not None:
            result.note(made=False)
            made.append(existing)
            continue
        adjustment = create_sales_adjustment(
            sales_day=day,
            reason_kind=kind,
            business_date=ADJUSTMENT_DATE,
            reason=f"{DEMO_BANNER}: {reason}",
            evidence_reference=evidence,
            actor=accounting,
            notes=DEMO_BANNER,
        )
        add_adjustment_line(
            adjustment=adjustment,
            original_line=lines[sequence],
            adjusted_quantity=Decimal(quantity),
            adjusted_gross=Decimal(gross) if gross is not None else None,
            line_reason=reason,
            actor=accounting,
        )
        made.append(post_sales_adjustment(adjustment=adjustment, actor=accounting))
        result.note(made=True)
    return made


# ---------------------------------------------------------------------------
# Settlements
# ---------------------------------------------------------------------------


def _sale_entry(result: SalesDemo, application_code: str, day: SalesDay) -> Any:
    from apps.sales.models import ApplicationReceivableEntry
    from apps.sales.posting import SOURCE_DOCUMENT_TYPE as DAY_SOURCE

    return ApplicationReceivableEntry.objects.filter(
        organization=result.organization,
        delivery_application=result.applications[application_code],
        source=ReceivableSource.SALE_POSTED,
        source_document_type=DAY_SOURCE,
        source_document_id=str(day.public_id),
    ).first()


def _adjustment_credit(result: SalesDemo, application_code: str) -> Decimal:
    """What the posted adjustments already took back off this application."""
    from django.db.models import Sum

    from apps.sales.models import ApplicationReceivableEntry

    total = ApplicationReceivableEntry.objects.filter(
        organization=result.organization,
        delivery_application=result.applications[application_code],
        source=ReceivableSource.AUTHORIZED_ADJUSTMENT,
    ).aggregate(total=Sum("credit"))["total"]
    return total or ZERO


def _seed_settlements(result: SalesDemo, day: SalesDay) -> list[DeliveryApplicationSettlement]:
    """
    Two settlements: one posted with a claimed gap on each leg, one reconciled.

    The posted one is the whole of ADR-028 §7 on a screen. `expected` is Σ
    allocations, the statement says something smaller, the bank transferred
    something smaller again, and **every dinar of each gap carries its own
    claim** — a rate difference on the statement leg and a withholding on the
    remittance leg. Nothing is netted into a single "variance", because which
    two of the three figures agree is the diagnosis.

    The reconciled one carries an `UNEXPLAINED_APPROVED` claim, which is the
    escape hatch and is deliberately not free: two check constraints make it
    cost an explanation and a named approver.
    """
    accounting = result.actors["accounting"]
    made: list[DeliveryApplicationSettlement] = []

    # --- ALPHA: posted, with a gap claimed on each leg --------------------
    reference = f"{DEMO_NAMESPACE}/ALPHA-STMT-01"
    existing = DeliveryApplicationSettlement.objects.filter(
        delivery_application=result.applications["DEMO-APP-ALPHA"],
        statement_reference=reference,
    ).first()
    if existing is not None:
        result.note(made=False)
        made.append(existing)
    else:
        entry = _sale_entry(result, "DEMO-APP-ALPHA", day)
        if entry is not None:
            # The application does not pay for the order that came back, so the
            # claim is the sale less what the posted return already credited.
            claimable = entry.debit - _adjustment_credit(result, "DEMO-APP-ALPHA")
            statement_gap = Decimal("2000")
            remittance_gap = Decimal("500")
            statement_amount = claimable - statement_gap
            remitted_amount = statement_amount - remittance_gap
            settlement = create_settlement(
                organization=result.organization,
                branch=result.branch,
                delivery_application=result.applications["DEMO-APP-ALPHA"],
                period_start=POSTED_DATE,
                period_end=ADJUSTMENT_DATE,
                business_date=SETTLEMENT_DATE,
                statement_reference=reference,
                statement_date=SETTLEMENT_DATE - datetime.timedelta(days=1),
                statement_amount=statement_amount,
                remitted_amount=remitted_amount,
                # Deliberately unequal to the accrued figure, so the commission
                # gap shows up as the ADVISORY it is. It reaches no account:
                # commission was recognised once, at the sale.
                statement_commission_amount=Decimal("20000"),
                remittance_destination=SettlementRemittance.BANK,
                evidence_reference=DEMO_EVIDENCE,
                actor=accounting,
                notes=DEMO_BANNER,
            )
            allocate_entry(
                settlement=settlement,
                receivable_entry=entry,
                allocated_amount=claimable,
                actor=accounting,
            )
            add_settlement_adjustment(
                settlement=settlement,
                leg=SettlementVarianceLeg.STATEMENT,
                reason=SettlementAdjustmentReason.COMMISSION_RATE_DIFFERENCE,
                amount=statement_gap,
                explanation="كشف التطبيق يطبّق نسبة عمولة أعلى من الاتفاقية.",
                actor=accounting,
            )
            add_settlement_adjustment(
                settlement=settlement,
                leg=SettlementVarianceLeg.REMITTANCE,
                reason=SettlementAdjustmentReason.WITHHOLDING_OR_OFFSET,
                amount=remittance_gap,
                explanation="حجز مقابل رسوم إدارية، مذكور في الكشف.",
                actor=accounting,
            )
            reconcile_settlement(settlement=settlement, actor=accounting)
            made.append(post_settlement(settlement=settlement, actor=accounting))
            result.note(made=True)

    # --- BETA: reconciled and left unposted -------------------------------
    reference = f"{DEMO_NAMESPACE}/BETA-STMT-01"
    existing = DeliveryApplicationSettlement.objects.filter(
        delivery_application=result.applications["DEMO-APP-BETA"],
        statement_reference=reference,
    ).first()
    if existing is not None:
        result.note(made=False)
        made.append(existing)
        return made

    entry = _sale_entry(result, "DEMO-APP-BETA", day)
    if entry is None:  # pragma: no cover - the posted day always writes one
        return made
    claimable = entry.debit
    remittance_gap = Decimal("500")
    settlement = create_settlement(
        organization=result.organization,
        branch=result.branch,
        delivery_application=result.applications["DEMO-APP-BETA"],
        period_start=POSTED_DATE,
        period_end=ADJUSTMENT_DATE,
        business_date=SETTLEMENT_DATE,
        statement_reference=reference,
        statement_date=SETTLEMENT_DATE - datetime.timedelta(days=1),
        statement_amount=claimable,
        remitted_amount=claimable - remittance_gap,
        statement_commission_amount=Decimal("7650"),
        remittance_destination=SettlementRemittance.CASH,
        evidence_reference=DEMO_EVIDENCE,
        actor=accounting,
        notes=DEMO_BANNER,
    )
    allocate_entry(
        settlement=settlement,
        receivable_entry=entry,
        allocated_amount=claimable,
        actor=accounting,
    )
    add_settlement_adjustment(
        settlement=settlement,
        leg=SettlementVarianceLeg.REMITTANCE,
        reason=SettlementAdjustmentReason.UNEXPLAINED_APPROVED,
        amount=remittance_gap,
        explanation=(
            "فرق لم يفسّره التطبيق بعد. اعتُمد لإقفال الكشف، ومطالبة مفتوحة مع الطرف الآخر."
        ),
        actor=accounting,
        approver=accounting,
    )
    made.append(reconcile_settlement(settlement=settlement, actor=accounting))
    result.note(made=True)
    return made


# ---------------------------------------------------------------------------
# The whole scenario
# ---------------------------------------------------------------------------


def seed_sales_demo(
    *,
    organization: Organization,
    branch: Branch,
    second_branch: Branch,
    post_documents: bool,
) -> SalesDemo:
    """
    Build the scenario. Master data always; posted documents only when asked.

    `post_documents` mirrors the inventory seed's `--confirm-demo`: master data
    can be recreated and a posted journal cannot, so the irreversible half is
    gated and the rest is not.

    Not `@transaction.atomic` here — the **command** owns the transaction, so a
    refusal anywhere leaves nothing behind rather than half a menu and no days.
    """
    result = SalesDemo(organization=organization, branch=branch, second_branch=second_branch)
    result.actors = ensure_demo_actors(organization=organization, branch=branch)
    ensure_account_mappings(organization)
    ensure_fiscal_years(organization)

    _seed_channels(result)
    _seed_applications(result)
    _seed_menu(result)
    _seed_discounts(result)

    if not post_documents:
        return result

    result.posted_day = _seed_posted_day(result)
    result.reversed_day = _seed_reversed_day(result)
    result.draft_day = _seed_draft_day(result)

    if result.posted_day.status == SalesDayStatus.POSTED:
        # The drawer is counted before the corrections are recorded, which is
        # both the chronology and the rule: `expected_cash` is stamped at close,
        # so an adjustment decided four days later cannot move a count that was
        # already declared.
        result.shift = _seed_shift(result, result.posted_day)
        result.adjustments = _seed_adjustments(result, result.posted_day)
        result.settlements = _seed_settlements(result, result.posted_day)
    return result


__all__ = [
    "ANCHOR",
    "APPLICATIONS",
    "CHANNELS",
    "DEMO_BANNER",
    "DEMO_CODE_PREFIX",
    "DEMO_EVIDENCE",
    "DEMO_NAMESPACE",
    "DEMO_OPENING_FLOAT",
    "DEMO_SHORTAGE",
    "MENU",
    "DemoPreconditionError",
    "SalesDemo",
    "ensure_account_mappings",
    "ensure_demo_actors",
    "ensure_fiscal_years",
    "seed_sales_demo",
]
