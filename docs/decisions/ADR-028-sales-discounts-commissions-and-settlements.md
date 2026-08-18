# ADR-028 — Sales discounts, delivery commissions, and application settlements

- **Status**: Accepted
- **Date**: 2026-08-19
- **Task**: 4.0 — Sales domain specification, and Phase 4 checkpoints 2, 3 and 5
- **Related**: ADR-019 (account roles), ADR-022 (variance recognised where it
  is decided, not where it is convenient), ADR-023 (three-way matching)
- **Companion**: ADR-027 (sales recognition and application receivables)

---

## 1. Context

ADR-027 settles what a sale is worth and where it lands. Three things make the
amount non-obvious, and each one has a wrong answer that looks correct:

1. a discount that two parties fund between them;
2. a commission whose rate depends on which of four bases the contract names;
3. a monthly settlement where the application's own statement disagrees with
   both.

This ADR settles all three.

---

## 2. A discount programme is master data with an effective range

**Decision.** `DiscountProgram` carries a percentage **or** a fixed amount, an
effective range, a funding split, and its applicability (channel, application,
menu item). A sales line **snapshots** the programme, the gross discount, the
restaurant-funded share and the application-funded share.

Manual discounts are permitted and are not free-form: a manual discount
requires a reason, an actor, an explicit amount, and the
`manage_sales_discounts` authority. There is no silent override.

**Why the snapshot.** A programme that ran in July and was edited in August
must not restate July's margin. This is the same rule as the price version and
the recipe version, and it is not weaker here because a discount feels like a
smaller number.

---

## 3. Funding is a share of the discount, and the shares must sum to it

**Decision.** For a shared discount,

```
restaurant_funded_share + application_funded_share = 100% of the discount
```

enforced by a check constraint on the programme and re-checked on every line.
A programme whose split does not close is refused at creation.

**The two shares are economically different and neither may impersonate the
other.**

- The **restaurant-funded** portion is contra-revenue. The restaurant chose not
  to collect it, so it reduces what the restaurant earns and debits
  `SALES_DISCOUNT`, a class-4 account.
- The **application-funded** portion is reimbursed. It reduces neither revenue
  nor the amount the application owes; it is part of what the application owes.

**Why this matters more than it looks.** The tempting simplification is to
treat every discount as a restaurant discount and let the settlement sort it
out. Work through a 5,000-dinar order with a 1,000-dinar promotion the
application funds entirely. Under the simplification: revenue 5,000, discount
1,000, receivable 4,000 less commission. Under the correct treatment: revenue
5,000, restaurant discount 0, receivable 5,000 less commission. The customer
paid 4,000 either way, and the restaurant is owed 1,000 more than the
simplification says. That 1,000 shows up at settlement as an unexplained
*favourable* variance every single month — which is exactly the kind of
recurring difference that gets normalised into a rounding adjustment and never
investigated.

Calling the application-funded portion a restaurant expense would be the same
error wearing a different coat: it would leave revenue right and make marketing
spend wrong.

---

## 4. Commission is accrued at the sale, from the exact agreement

**Decision.** `DeliveryAgreement` is effective-dated per organization, branch
and application, and carries a percentage, an optional fixed fee per order, a
**closed** commission basis, a settlement lag and its evidence. The sales line
snapshots the agreement identity **and** every calculation field, and accrues
the commission at posting.

**Why accrue rather than wait.** The rate is known the day the order is taken.
Waiting for the statement would leave a month's margin unknown until the
following month, and — worse — would make every settlement difference look like
news. Accruing means a settlement difference is genuinely a difference, which
is the only way the variance figure carries information.

**Why the basis is a closed vocabulary and not a free string.**

```
GROSS_LIST_AMOUNT           commission on the list value
AFTER_RESTAURANT_DISCOUNT   list less what the restaurant funded
AFTER_ALL_DISCOUNTS         list less both funded shares
CUSTOMER_PAID_AMOUNT        what the customer actually handed over
```

The last two are numerically identical in Release 1 and are **still separate
values**, because they are different contractual claims that will diverge the
moment a delivery fee or a tip enters the model. Collapsing them now would mean
a later divergence silently restated every historical agreement. A free-text
basis would let a typo — `AFTER_ALL_DISCOUNT` — silently fall through to a
default, which is a pricing error nobody would see for a quarter.

The basis is stored on the agreement, snapshotted on the line, and named in the
verifier. Nothing derives it from the application's name.

---

## 5. What the application owes

```
customer_charge
  = gross_amount − restaurant_discount − application_discount

expected_application_receivable
  = gross_amount − restaurant_discount − commission − other_accrued_fees
```

The application-funded discount cancels out of the second expression, which is
the arithmetic statement of §3: the application pays back what it discounted.

---

## 6. Settlement binds to entries, not to totals

**Decision.** `DeliveryApplicationSettlement` has lifecycle
`DRAFT → RECONCILED → POSTED → REVERSED` and allocates to **posted receivable
entries** through `DeliveryApplicationSettlementAllocation`. The required
equality is

```
Σ allocations = receivable amount cleared
```

checked in the service and by a constraint.

**Why allocations rather than a period total.** A settlement that merely
credited "the balance as at the 31st" could not answer which sales it paid for,
and the first disputed order would be unanswerable. Allocations make the
question mechanical.

The settlement journal:

```
Dr  SALES_SETTLEMENT_BANK / SALES_CASH_ON_HAND   actual remittance
Dr/Cr DELIVERY_SETTLEMENT_VARIANCE               explained difference, if any
    Cr  DELIVERY_APP_RECEIVABLE                  receivable cleared
```

**Commission is never recognised twice.** It was accrued at the sale, so the
settlement does not expense it again; the statement's commission column is
compared against the accrual and any difference is a *variance*, not a second
expense. This is the single most likely error in the whole module and the
verifier checks for it explicitly.

---

## 7. Unexplained variance blocks posting

**Decision.** A settlement whose expected receivable, statement figure and
actual remittance do not reconcile **cannot post** until the difference is
either categorised against an approved adjustment reason or explicitly posted
to `DELIVERY_SETTLEMENT_VARIANCE` with a stated reason and an actor.

**Why blocking rather than absorbing.** An account that silently absorbs
differences is an account nobody reads, and a mis-configured commission rate
sitting inside it is invisible for a year. ADR-022 made the same call for
purchase variances: recognise a difference where it is decided, by somebody who
decided it.

The three-way comparison is kept explicit on the document —
**expected**, **statement**, **remitted** — rather than reduced to one net
number, for the same reason three-way matching keeps order, receipt and invoice
separate (ADR-023): the pattern of which two agree is the diagnosis.

---

## 8. Returns and cancellations are three different facts

**Decision.** `SalesAdjustment` carries a closed reason kind:

| Kind | Financial | Theoretical consumption |
|---|---|---|
| `CANCELLED_BEFORE_FULFILLMENT` | reverses the sale | **reduces** the quantity |
| `RETURNED_AFTER_FULFILLMENT` | reverses the approved amount | **no change** |
| `FINANCIAL_CORRECTION` | corrects money only | **no change** |

**Why a cancellation reduces consumption and a return does not.** A cancelled
order was never cooked, so the ingredients never left. A returned order was
cooked, and the ingredients did leave — subtracting it from theoretical
consumption would manufacture an unexplained actual-versus-theoretical variance
of exactly the returned quantity, every time. Where the returned food is
physically thrown away, that is a Waste document in the kitchen's own ledger;
recording it here as well would double-count it (ADR-026 §4).

**Why `FINANCIAL_CORRECTION` may not touch quantity.** A correction to money is
not a claim that less food was sold. Letting it silently rewrite sold quantity
would let a pricing fix change what the kitchen is measured against.

Every adjustment requires a reason code, evidence, an actor, a business date,
the original posted line, and its own posting lifecycle. A posted sales line is
never edited.

---

## 9. What this ADR does not decide

- **No landed-cost-style allocation of commission into food cost.** Commission
  is a selling expense; pushing it into COGS would make food cost move for
  reasons that have nothing to do with the kitchen — the same argument ADR-022
  makes about purchase price variance.
- **No automatic write-off of aged application receivables.** Aging is
  reported; deciding a debt is bad is a decision with an owner.
- **No tips, delivery fees charged to the customer, or driver payments.** None
  is in the Release 1 contracts, and modelling them speculatively would put
  fields in the ledger that nobody can populate correctly.
