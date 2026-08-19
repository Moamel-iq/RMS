"""
Which discount applies, and who pays for it.

The second question is the one that matters. A discount is money somebody did
not collect, and the whole design turns on recording *which* somebody:

* the **restaurant-funded** share is contra-revenue — the restaurant chose not
  to collect it, so it reduces what the restaurant earns;
* the **application-funded** share is reimbursed — it reduces neither revenue
  nor what the delivery company owes; it is *part* of what the company owes.

Treating the second as a restaurant discount understates revenue and
understates the receivable by the same amount, and both figures look
internally consistent afterwards. The error surfaces months later as a
recurring *favourable* settlement variance, which is the kind of difference
that gets normalised into a rounding adjustment and never investigated
(ADR-028 §3).

Nothing here writes, and nothing reads the clock.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q, QuerySet

from apps.core.money import quantize_money
from apps.sales.models import DiscountProgram

ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


def applicable_programs(
    *,
    organization_id: int,
    branch_id: int,
    on_date: datetime.date,
    menu_item_id: int | None = None,
    channel_id: int | None = None,
    delivery_application_id: int | None = None,
) -> QuerySet[DiscountProgram]:
    """
    Every programme whose applicability covers this line on this date.

    A `NULL` on any axis means "no restriction on this axis", so the filters
    are all `IS NULL OR equals`. That is deliberate rather than a shortcut: the
    common promotion applies to everything, and forcing an operator to
    enumerate every channel to express "everywhere" would guarantee that a
    channel added later silently falls outside it.

    Returned as a queryset, not one row. Two programmes can legitimately be
    live at once — a Ramadan offer and an application launch promotion — and
    deciding that they stack, or that one wins, is a business decision the
    *sale* records explicitly rather than one this read makes silently.
    """
    rows = DiscountProgram.objects.filter(
        organization_id=organization_id,
        is_active=True,
        effective_from__lte=on_date,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))

    rows = rows.filter(Q(branch__isnull=True) | Q(branch_id=branch_id))
    if menu_item_id is not None:
        rows = rows.filter(Q(menu_item__isnull=True) | Q(menu_item_id=menu_item_id))
    else:
        rows = rows.filter(menu_item__isnull=True)

    if channel_id is not None:
        rows = rows.filter(Q(channel__isnull=True) | Q(channel_id=channel_id))
    else:
        rows = rows.filter(channel__isnull=True)

    if delivery_application_id is not None:
        rows = rows.filter(
            Q(delivery_application__isnull=True)
            | Q(delivery_application_id=delivery_application_id)
        )
    else:
        # No application on the line means an application-funded promotion
        # cannot apply: there is nobody to reimburse it.
        rows = rows.filter(delivery_application__isnull=True)

    return rows.select_related("branch", "channel", "delivery_application", "menu_item").order_by(
        "code"
    )


@dataclass(frozen=True)
class DiscountSplit:
    """
    One discount, and the two shares it divides into.

    `application_funded` is computed as the **residual** rather than by
    applying the second percentage. That is the one arithmetic decision in this
    module and it is load-bearing: rating both shares independently and
    rounding each would let a 1,000-dinar discount split into 333.333 and
    666.666, which sums to 999.999 and leaves a fils funded by nobody. Taking
    one share and subtracting makes the split close by construction, at every
    amount, forever.
    """

    gross_discount: Decimal
    restaurant_funded: Decimal
    application_funded: Decimal

    def __post_init__(self) -> None:
        total = self.restaurant_funded + self.application_funded
        if total != self.gross_discount:  # pragma: no cover - construction guarantees it
            raise ValueError(
                f"discount split does not close: {self.restaurant_funded} + "
                f"{self.application_funded} != {self.gross_discount}"
            )

    @property
    def is_shared(self) -> bool:
        return self.restaurant_funded > ZERO and self.application_funded > ZERO


def split_for(
    program: DiscountProgram | None,
    *,
    gross_amount: Decimal,
    manual_amount: Decimal | None = None,
) -> DiscountSplit:
    """
    What this line's discount is, and who funds each part.

    Three cases, in order:

    1. **A manual discount** — no programme, an explicit amount, and (enforced
       by the caller) a reason and an actor. It is entirely restaurant-funded,
       because a delivery company does not reimburse a discount it never agreed
       to. Allowing a manual application-funded discount would let a cashier
       create a receivable out of a keystroke.
    2. **A programme** — percentage or fixed amount, capped by `maximum_amount`
       where one is stated, split by the programme's own shares.
    3. **Neither** — a zero split, which is a real answer and not a missing one.

    Quantized **once**, at the end, and the second share is the residual. See
    `DiscountSplit`.
    """
    if manual_amount is not None:
        amount = quantize_money(manual_amount)
        return DiscountSplit(
            gross_discount=amount, restaurant_funded=amount, application_funded=ZERO
        )

    if program is None:
        return DiscountSplit(gross_discount=ZERO, restaurant_funded=ZERO, application_funded=ZERO)

    if program.discount_amount is not None:
        raw = program.discount_amount
    else:
        assert program.discount_percent is not None  # noqa: S101 - a check constraint guarantees it
        raw = gross_amount * program.discount_percent / ONE_HUNDRED

    if program.maximum_amount is not None and raw > program.maximum_amount:
        raw = program.maximum_amount
    # A discount cannot exceed what is being discounted. Capped rather than
    # refused: a fixed-amount promotion meeting a small order is an ordinary
    # situation, and a negative sale is not.
    if raw > gross_amount:
        raw = gross_amount

    gross_discount = quantize_money(raw)
    restaurant = quantize_money(gross_discount * program.restaurant_funded_share / ONE_HUNDRED)
    application = gross_discount - restaurant
    return DiscountSplit(
        gross_discount=gross_discount,
        restaurant_funded=restaurant,
        application_funded=application,
    )


def funding_is_complete(program: DiscountProgram) -> bool:
    """
    Whether the programme's two shares close.

    Re-checked here as well as by the database constraint, because this is the
    rule a screen has to be able to explain *before* a save fails. The
    constraint is what makes it true; this is what makes it sayable.
    """
    return program.restaurant_funded_share + program.application_funded_share == ONE_HUNDRED


__all__ = [
    "DiscountSplit",
    "applicable_programs",
    "funding_is_complete",
    "split_for",
]
