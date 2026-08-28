"""
Comparing supplier quotations, and awarding one.

The comparison exists because raw quoted prices are not comparable. Two
suppliers quote "rice": one by the 30 kg sack at 42,000, one by the kilogram at
1,450. Put those two numbers side by side and the sack looks catastrophically
expensive. Normalise both to a base unit and the sack is cheaper — 1,400
against 1,450. Add the sack supplier's 15,000 delivery charge and it is dearer
again. Every one of those three readings is arithmetically correct, and only
the last one is the answer to "what will this actually cost us".

So this module computes all three and shows them, and awards nothing. PRC-016:
the system ranks, a human decides, and the reason is recorded — because lowest
landed cost is still not the same question as best value, and a supplier who is
200 dinars dearer and actually delivers on Friday is often the right choice.

Nothing here writes to a ledger. An award creates no stock, no journal, no
payable and no GRNI; it records which offer was chosen so the purchase order
raised later can point at it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.allocation import AllocationItem, allocate
from apps.core.models import AuditAction
from apps.core.money import quantize_money, quantize_unit_price
from apps.core.services import record_audit_event, snapshot
from apps.procurement.models import (
    PurchaseRequest,
    PurchaseRequestStatus,
    SupplierQuotation,
    SupplierQuotationLine,
    SupplierQuotationStatus,
)
from apps.users.models import User


@dataclass(frozen=True)
class ComparisonRow:
    """
    One quotation line, expressed so it can be compared with any other.

    Every money field is a `Decimal` and every one is derived on read. Nothing
    in this dataclass is stored: a persisted landed price would be a second
    figure that disagrees with the quotation the moment freight is corrected.
    """

    quotation_id: int
    quotation_number: str
    line_id: int
    supplier_code: str
    supplier_name: str
    item_code: str
    item_name: str
    base_unit_code: str
    package_code: str | None
    #: What the supplier wrote, in the unit they wrote it in.
    quoted_quantity: Decimal
    quoted_unit_price: Decimal
    line_total: Decimal
    #: The same offer in the item's own unit.
    base_quantity: Decimal
    base_unit_price: Decimal
    #: This line's share of the document's freight and other charges.
    charge_share: Decimal
    landed_total: Decimal
    landed_base_unit_price: Decimal
    #: Context a price alone does not carry.
    valid_until: datetime.date | None
    is_expired: bool
    lead_time_days: int | None
    minimum_order_quantity: Decimal | None
    meets_minimum_order: bool
    supplier_sku: str
    #: Ranking flags. Presentation only — nothing acts on them.
    is_cheapest_base_unit: bool = False
    is_cheapest_landed: bool = False


def _charge_shares(quotation: SupplierQuotation) -> dict[int, Decimal]:
    """
    Spread the document's freight and other charges across its lines.

    Weighted by line total, allocated with `apps/core/allocation.py` so the
    shares sum **exactly** to the charge — never by rating each line and
    rounding, which is how a total stops matching its parts (ADR-012).

    A quotation whose lines are all zero-valued (every item a free sample) has
    no weight to spread by. Its charges stay unallocated rather than being
    split evenly, because "evenly" would be an invented basis: the freight on a
    free sample is a real cost and the right answer is that nobody has said how
    to attribute it.
    """
    charges = quantize_money(quotation.freight_amount + quotation.other_charges)
    lines = list(quotation.lines.all().order_by("sequence"))
    if not lines or charges == 0:
        return {line.pk: Decimal("0.000") for line in lines}

    weights = [AllocationItem(sequence=line.sequence, weight=line.line_total) for line in lines]
    if all(item.weight == 0 for item in weights):
        return {line.pk: Decimal("0.000") for line in lines}

    by_sequence = {result.sequence: result.amount for result in allocate(charges, weights)}
    return {line.pk: by_sequence[line.sequence] for line in lines}


def _row_for(line: SupplierQuotationLine, *, share: Decimal, on: datetime.date) -> ComparisonRow:
    quotation = line.quotation
    landed_total = quantize_money(line.line_total + share)
    catalogue = line.supplier_item

    minimum = catalogue.minimum_order_quantity if catalogue else None
    # The minimum is stated in whatever unit the catalogue row buys in, which
    # is the same unit the quotation line uses when both name a package. Where
    # they differ there is nothing to compare, so the check does not pretend.
    comparable = catalogue is not None and catalogue.package_unit_id == line.package_unit_id
    meets_minimum = not (comparable and minimum is not None) or line.quantity >= minimum

    return ComparisonRow(
        quotation_id=quotation.pk,
        quotation_number=quotation.number,
        line_id=line.pk,
        supplier_code=quotation.supplier.code,
        supplier_name=quotation.supplier.name,
        item_code=line.item.code,
        item_name=line.item.name,
        base_unit_code=line.item.base_unit.code,
        package_code=line.package_unit.code if line.package_unit else None,
        quoted_quantity=line.quantity,
        quoted_unit_price=line.unit_price,
        line_total=line.line_total,
        base_quantity=line.base_quantity,
        base_unit_price=line.base_unit_price,
        charge_share=share,
        landed_total=landed_total,
        landed_base_unit_price=quantize_unit_price(landed_total / line.base_quantity),
        valid_until=quotation.valid_until,
        is_expired=quotation.valid_until is not None and quotation.valid_until < on,
        lead_time_days=catalogue.lead_time_days if catalogue else None,
        minimum_order_quantity=minimum if comparable else None,
        meets_minimum_order=meets_minimum,
        supplier_sku=catalogue.supplier_sku if catalogue else "",
    )


def compare_quotations(
    *, quotations: list[SupplierQuotation], item_id: int | None = None, on: datetime.date
) -> list[ComparisonRow]:
    """
    Every quoted line, normalised, ranked and flagged.

    Sorted by landed base unit price ascending, then by supplier code so the
    order is stable when two suppliers land on the same figure. The sort is
    presentation: `is_cheapest_base_unit` and `is_cheapest_landed` are the two
    facts a buyer needs to see disagree, and they are computed **per item**,
    because "cheapest" across different items is not a sentence that means
    anything.
    """
    rows: list[ComparisonRow] = []
    for quotation in quotations:
        shares = _charge_shares(quotation)
        lines = quotation.lines.select_related(
            "item", "item__base_unit", "package_unit", "supplier_item"
        ).order_by("sequence")
        for line in lines:
            if item_id is not None and line.item_id != item_id:
                continue
            rows.append(_row_for(line, share=shares[line.pk], on=on))

    ranked: list[ComparisonRow] = []
    by_item: dict[str, list[ComparisonRow]] = {}
    for row in rows:
        by_item.setdefault(row.item_code, []).append(row)

    for group in by_item.values():
        # An expired offer is shown but never crowned: flagging it cheapest
        # would recommend something the award service will refuse.
        live = [row for row in group if not row.is_expired]
        pool = live or []
        cheapest_unit = min((row.base_unit_price for row in pool), default=None)
        cheapest_landed = min((row.landed_base_unit_price for row in pool), default=None)
        for row in group:
            ranked.append(
                ComparisonRow(
                    **{
                        **row.__dict__,
                        "is_cheapest_base_unit": (
                            not row.is_expired and row.base_unit_price == cheapest_unit
                        ),
                        "is_cheapest_landed": (
                            not row.is_expired and row.landed_base_unit_price == cheapest_landed
                        ),
                    }
                )
            )

    ranked.sort(key=lambda row: (row.item_code, row.landed_base_unit_price, row.supplier_code))
    return ranked


def comparison_for_request(
    *, request: PurchaseRequest, on: datetime.date | None = None
) -> list[ComparisonRow]:
    """Every offer recorded against one request, compared."""
    quotations = list(
        SupplierQuotation.objects.filter(request=request)
        .exclude(status=SupplierQuotationStatus.DRAFT)
        .select_related("supplier")
        .prefetch_related("lines")
    )
    return compare_quotations(quotations=quotations, on=on or timezone.localdate())


@transaction.atomic
def award_quotation(
    *,
    request: PurchaseRequest,
    quotation: SupplierQuotation,
    actor: User,
    reason: str,
    on: datetime.date | None = None,
) -> PurchaseRequest:
    """
    Record which offer was chosen, and why.

    A reason is required unconditionally, not only when the winner is dearer.
    The specification makes it mandatory in that case; making it mandatory
    always is stricter, and the alternative is a field that is usually empty
    and therefore never read — which defeats the point of recording a
    judgement at all.

    Creates no stock, no journal, no payable and no GRNI. The purchase order
    raised from this award is the commitment; this is the record of a decision.
    """
    if not reason.strip():
        raise ValidationError(
            _("An award must record why this offer was chosen."), code="reason_required"
        )

    locked = PurchaseRequest.objects.select_for_update().get(pk=request.pk)
    previous = snapshot(locked)
    # Re-read the quotation too, and under the same lock. Its status must come
    # from the database, never from the instance the caller happens to be
    # holding: an object loaded while the offer was SUBMITTED would still say
    # SUBMITTED after somebody else declined or awarded it, and this is the
    # direction that matters — the stale read would let an award through.
    quotation = SupplierQuotation.objects.select_for_update().get(pk=quotation.pk)

    if locked.status != PurchaseRequestStatus.APPROVED:
        raise ValidationError(
            _("Only an approved request may be awarded; %(number)s is %(status)s."),
            code="request_not_approved",
            params={
                "number": locked.number or str(locked.public_id),
                "status": locked.status,
            },
        )
    if locked.awarded_quotation_id is not None:
        raise ValidationError(
            _("Request %(number)s is already awarded."),
            code="already_awarded",
            params={"number": locked.number},
        )
    if quotation.organization_id != locked.organization_id:
        raise ValidationError(
            _("That quotation belongs to another organization."),
            code="organization_mismatch",
        )
    if quotation.request_id != locked.pk:
        raise ValidationError(
            _("That quotation was not offered against this request."),
            code="quotation_not_for_request",
        )
    if quotation.status != SupplierQuotationStatus.SUBMITTED:
        raise ValidationError(
            _("Quotation %(number)s is %(status)s and cannot be awarded."),
            code="quotation_not_awardable",
            params={
                "number": quotation.number or str(quotation.public_id),
                "status": quotation.status,
            },
        )

    business_date = on or timezone.localdate()
    if quotation.valid_until is not None and quotation.valid_until < business_date:
        raise ValidationError(
            _("Quotation %(number)s expired on %(date)s."),
            code="quotation_expired",
            params={
                "number": quotation.number,
                "date": quotation.valid_until.isoformat(),
            },
        )

    locked.awarded_quotation = quotation
    locked.awarded_by = actor
    locked.awarded_at = timezone.now()
    locked.award_reason = reason.strip()
    locked.full_clean()
    locked.save()

    awarded_previous = snapshot(quotation)
    quotation.status = SupplierQuotationStatus.AWARDED
    quotation.full_clean()
    quotation.save()
    record_audit_event(
        action=AuditAction.APPROVED,
        target=quotation,
        previous_state=awarded_previous,
        new_state=snapshot(quotation),
        reason=reason.strip(),
    )

    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        branch=locked.branch,
        previous_state=previous,
        new_state=snapshot(locked),
        reason=reason.strip(),
    )
    return locked
