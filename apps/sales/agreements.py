"""
Which delivery agreement applies, and what it costs.

Two questions, kept apart on purpose:

* **resolution** — given a branch, an application and a business date, which
  agreement was in force? One answer, or none.
* **computation** — given that agreement and a line's amounts, what commission
  does it accrue?

Splitting them is what makes a posted commission re-derivable. The sales line
stores the agreement identity *and* every field the computation used, so
`commission_for` can be replayed against stored values years later without
this table being consulted at all (ADR-028 §4).

Nothing here writes. Nothing here reads the clock: every function takes the
business date as an argument, because "which agreement applies" is a claim
about a named day and a function that read `today` would answer differently at
23:59 and 00:01 with nothing having changed.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.money import quantize_money
from apps.sales.models import CommissionBasis, DeliveryAgreement

ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


def resolve_agreement(
    *,
    branch_id: int,
    delivery_application_id: int,
    on_date: datetime.date,
) -> DeliveryAgreement | None:
    """
    The agreement in force for this branch and application on this date.

    `None` rather than an exception, and rather than a default rate. A default
    would be a commission nobody agreed to, quietly accrued against a real
    company; the *sale* refuses to post a line it cannot price, which puts the
    failure in front of whoever can fix it.

    At most one row can match: the exclusion constraint in migration `0003`
    refuses overlapping active periods for the same branch and application, so
    the ordering below breaks a tie that cannot exist. It is there to make the
    result deterministic rather than dependent on insertion order.
    """
    return (
        DeliveryAgreement.objects.filter(
            branch_id=branch_id,
            delivery_application_id=delivery_application_id,
            is_active=True,
            effective_from__lte=on_date,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
        .select_related("delivery_application", "branch")
        .order_by("-effective_from", "-pk")
        .first()
    )


@dataclass(frozen=True)
class CommissionInputs:
    """
    Everything the commission arithmetic needs, as values rather than rows.

    Values on purpose. A sales line snapshots each of these, so replaying a
    posted commission needs no agreement, no application and no database — the
    figures on the line are sufficient, which is what makes a three-year-old
    accrual arguable.
    """

    gross_amount: Decimal
    restaurant_discount: Decimal
    application_discount: Decimal
    order_count: int
    commission_percent: Decimal
    fixed_fee_per_order: Decimal
    commission_basis: str


@dataclass(frozen=True)
class CommissionResult:
    """The accrued commission, and the base it was charged on."""

    basis_amount: Decimal
    percentage_component: Decimal
    fixed_component: Decimal

    @property
    def total(self) -> Decimal:
        """
        Quantized **once**, at the end.

        Rounding each component and adding would be the classic error ADR-006
        names: two 0.4995 halves round to 1.000 together and 0.500 + 0.500 =
        1.000 apart only by luck, and on a month of orders the luck runs out.
        """
        return quantize_money(self.percentage_component + self.fixed_component)


def basis_amount_for(inputs: CommissionInputs) -> Decimal:
    """
    What the percentage is charged on, per the contract's stated basis.

    `AFTER_ALL_DISCOUNTS` and `CUSTOMER_PAID_AMOUNT` compute identically in
    Release 1 and are deliberately separate branches rather than one shared
    one. They are different contractual claims and they diverge the moment a
    delivery fee or a tip enters the model; collapsing them into a single
    branch now would make that later divergence a silent restatement of every
    historical agreement (ADR-028 §4).
    """
    gross = inputs.gross_amount
    if inputs.commission_basis == CommissionBasis.GROSS_LIST_AMOUNT:
        return gross
    if inputs.commission_basis == CommissionBasis.AFTER_RESTAURANT_DISCOUNT:
        return gross - inputs.restaurant_discount
    if inputs.commission_basis == CommissionBasis.AFTER_ALL_DISCOUNTS:
        return gross - inputs.restaurant_discount - inputs.application_discount
    if inputs.commission_basis == CommissionBasis.CUSTOMER_PAID_AMOUNT:
        return gross - inputs.restaurant_discount - inputs.application_discount
    # No fallback, and no default rate. An unknown basis is a vocabulary that
    # grew without this function growing with it, and answering anyway would
    # accrue a commission nobody agreed to.
    raise ValueError(f"unknown commission basis {inputs.commission_basis!r}")


def commission_for(inputs: CommissionInputs) -> CommissionResult:
    """
    Accrue the commission for one line, at the sale.

    Accrued rather than discovered at settlement, because the rate is known the
    day the order is taken. Waiting for the statement would leave a month's
    margin unknown until the following month and — worse — make every
    settlement difference look like news, which is precisely what stops a
    variance figure carrying information (ADR-028 §4).

    Full precision throughout; `CommissionResult.total` quantizes once.
    """
    basis = basis_amount_for(inputs)
    percentage = basis * inputs.commission_percent / ONE_HUNDRED
    fixed = inputs.fixed_fee_per_order * Decimal(inputs.order_count)
    return CommissionResult(
        basis_amount=basis,
        percentage_component=percentage,
        fixed_component=fixed,
    )


def inputs_from(
    agreement: DeliveryAgreement,
    *,
    gross_amount: Decimal,
    restaurant_discount: Decimal,
    application_discount: Decimal,
    order_count: int,
) -> CommissionInputs:
    """Snapshot an agreement's terms beside a line's amounts."""
    return CommissionInputs(
        gross_amount=gross_amount,
        restaurant_discount=restaurant_discount,
        application_discount=application_discount,
        order_count=order_count,
        commission_percent=agreement.commission_percent,
        fixed_fee_per_order=agreement.fixed_fee_per_order,
        commission_basis=agreement.commission_basis,
    )


@dataclass(frozen=True)
class AgreementPreview:
    """
    What one agreement would charge on a worked example.

    The agreements screen renders this so somebody approving a contract can see
    the number rather than the rate. A percentage and a fixed fee read as
    harmless separately; 15% of 25,000 plus 500 per order does not.
    """

    agreement: DeliveryAgreement
    gross_amount: Decimal
    order_count: int
    result: CommissionResult

    @property
    def basis_label(self) -> str:
        return str(CommissionBasis(self.agreement.commission_basis).label)

    @property
    def net_to_restaurant(self) -> Decimal:
        return quantize_money(self.gross_amount - self.result.total)


def preview(
    agreement: DeliveryAgreement,
    *,
    gross_amount: Decimal,
    order_count: int = 1,
) -> AgreementPreview:
    """One agreement against a worked example, for the approval screen."""
    return AgreementPreview(
        agreement=agreement,
        gross_amount=gross_amount,
        order_count=order_count,
        result=commission_for(
            inputs_from(
                agreement,
                gross_amount=gross_amount,
                restaurant_discount=ZERO,
                application_discount=ZERO,
                order_count=order_count,
            )
        ),
    )


#: The message a sale shows when no agreement covers the date. Named here
#: rather than written inline at three call sites, so the three cannot drift.
NO_AGREEMENT_NOTICE = _(
    "لا توجد اتفاقية عمولة سارية لهذا التطبيق في هذا الفرع بتاريخ العملية. "
    "لا يمكن ترحيل مبيعات التطبيق بدون اتفاقية — العمولة تُستحق عند البيع، لا عند التسوية."
)


__all__ = [
    "NO_AGREEMENT_NOTICE",
    "AgreementPreview",
    "CommissionInputs",
    "CommissionResult",
    "basis_amount_for",
    "commission_for",
    "inputs_from",
    "preview",
    "resolve_agreement",
]
