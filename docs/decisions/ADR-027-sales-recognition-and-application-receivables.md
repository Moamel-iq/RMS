# ADR-027 — Sales recognition, application receivables, and the cashier boundary

- **Status**: Accepted
- **Date**: 2026-08-19
- **Task**: 4.0 — Sales domain specification, and Phase 4 checkpoints 1–7
- **Related**: ADR-014 (chart of accounts), ADR-016 (permission plus scope),
  ADR-019 (account roles and the posting indirection), ADR-024 (recipe
  versioning and the effective-dated basis), ADR-026 (kitchen consumption and
  the boundary of usage variance)
- **Companion**: ADR-028 (discounts, commissions and settlements)

---

## 1. Context

Phase 3 finished the kitchen and left one hole in it on purpose.
`TheoreticalSourceType.SALES` was **declared and not implemented**, and every
theoretical-consumption figure the system produced carried
`SALES_NOT_INCLUDED_PHASE_4` beside it. That was not a stub: the coverage
report iterated the enum rather than the registry precisely so it could name a
source it did not have.

Phase 4 supplies that source. It also has to supply everything a restaurant
means by "sales" — a menu, prices, channels, three delivery companies with
three different contracts, discounts that somebody has to fund, a till that
somebody has to count, and a monthly argument with each application about what
it actually owes.

This ADR settles **recognition**: when a sale becomes revenue, what the journal
says, and what a receivable from a delivery application is. ADR-028 settles the
three things that make the amounts non-obvious — discounts, commissions and
settlements.

---

## 2. Revenue is gross, and deductions sit beside it

**Decision.** `SalesDayLine.gross_amount = quantity × the snapshotted unit
price`, and `SALES_REVENUE` is credited with that figure. Every deduction —
restaurant-funded discount, commission, application fee — is a **separate
line** in the same journal, never a subtraction inside the credit.

**Why.** A revenue figure that already has discounts inside it cannot answer
"what did we give away this month", and that is the question a restaurant asks
when its margin moves. Netting also destroys the arithmetic that makes the
journal checkable: with gross revenue on one side, every other line is an
identifiable claim about where the difference went, and the entry balancing is
a real proof rather than a tautology.

Decimal throughout, quantized once at the storage boundary (ADR-006). No float
appears anywhere in this module, including in the dashboard aggregates.

---

## 3. The sale is one document per branch per business date

**Decision.** The sales aggregate is `SalesDay` — one organization, one branch,
one explicit business date — carrying `SalesDayLine` rows and
`SalesTenderSummary` rows. Lifecycle `DRAFT → SUBMITTED → POSTED → REVERSED`.

**Why an aggregate rather than an order.** Release 1 has no point-of-sale
integration; takings arrive as a day's totals from a till report and three
application dashboards. Modelling an individual order would mean inventing
order identity the restaurant does not have, and every report would then be
built on fabricated granularity. A day is what the business can actually
evidence, and `order_count` on the line records how many orders produced that
quantity without pretending to identify them.

**Why `SUBMITTED` exists here when the supplier invoice has no such state.**
The comparison is worth stating because it looks inconsistent. A supplier
invoice arrives as a document somebody else authored, so approving it *is* the
second pair of eyes. A sales day is authored in-house by the person whose till
it describes, so the second pair of eyes has to come after the authoring is
finished — `SUBMITTED` is the cashier saying "this is what I counted", and
`POSTED` is somebody else agreeing it may reach the ledger.

**Posted lines are never edited.** Correction is reversal plus a replacement
day, or a `SalesAdjustment` where the correction is about a specific line. A
posted `SalesDay` is frozen by a database trigger, not by application code.

---

## 4. The version is resolved once, at the business date, and stored forever

**Decision.** When a line is entered, the service resolves and **stores** the
exact `Recipe`, `RecipeVersion`, `RecipeServing` and `MenuPriceVersion` in
force at that line's business date. Nothing re-resolves them afterwards — not
a report, not the theoretical-consumption adapter, not the dashboard.

**Why.** This is the charter's absolute rule (ADR-024) and Phase 4 is where it
would be easiest to break. A recipe changed in September must not restate what
August consumed, and a price changed on Monday must not restate Sunday's
revenue. Storing the identity rather than re-deriving it is what makes a
year-old sales day still explainable, and it is why `SalesDayLine` carries five
snapshot fields that look redundant until somebody edits a recipe.

The corollary is that a menu item whose recipe has **no version effective at
the business date** cannot be sold on that date. The service refuses the line
rather than falling back to the newest version, because a fallback is a silent
answer to a question the operator did not know they were asking.

---

## 5. An application receivable is a ledger, never a balance

**Decision.** `ApplicationReceivableEntry` is **append-only**: one row per
economic event, with `debit`, `credit`, a source identity and a business date.
The balance is `SUM(debit) - SUM(credit)` over the entries. No
`current_balance` field exists on `DeliveryApplication`, and adding one is
refused.

**Why.** A stored balance is a number that can disagree with the entries that
produced it, and the disagreement is discovered during a settlement argument —
the worst possible moment, because the counterparty has their own figure and
the restaurant can no longer explain its own. An append-only ledger cannot
drift: there is exactly one representation, and every screen derives from it.

The performance objection is real and answered rather than dismissed: the
entries are indexed on `(organization, delivery_application, business_date)`
and a period balance is one aggregate query. If that ever stops being enough,
the answer is a materialised period summary that is *derived and rebuildable*,
never an incrementally-maintained field.

Entry sources are a closed set: `SALE_POSTED`, `SALE_REVERSED`, `SETTLEMENT`,
`SETTLEMENT_REVERSED`, `AUTHORIZED_ADJUSTMENT`.

---

## 6. The application sales journal

For a posted application sale:

```
Dr  DELIVERY_APP_RECEIVABLE        expected_application_receivable
Dr  SALES_DISCOUNT                 restaurant-funded discount
Dr  DELIVERY_COMMISSION_EXPENSE    commission
Dr  DELIVERY_OTHER_FEE_EXPENSE     other accrued fees, where configured
    Cr  SALES_REVENUE                                     gross amount
```

with

```
expected_application_receivable
  = gross_amount
  − restaurant_discount
  − commission_amount
  − other_accrued_fees
```

The balance is exact by construction: the four debits sum to gross by
definition of the receivable. That is not a happy accident — it is why the
receivable is defined as a residual rather than computed independently and
checked afterwards.

**The application-funded discount is deliberately absent from this journal.**
It is money the *application* gave the customer and will reimburse, so it
reduces neither revenue nor the amount owed. Working it through: the customer
paid `gross − restaurant_discount − application_discount`, and the application
owes the restaurant that plus the discount it funded, which is
`gross − restaurant_discount` before its own commission. Posting the
application-funded portion as a restaurant discount would understate revenue
and understate the receivable by the same amount, and both figures would look
internally consistent. ADR-028 §3 carries the full argument.

## 7. The cash and card journal

```
Dr  SALES_CASH_ON_HAND / SALES_CARD_CLEARING   gross − restaurant_discount
Dr  SALES_DISCOUNT                             restaurant-funded discount
    Cr  SALES_REVENUE                                        gross amount
```

The tender destination is resolved through the channel's effective account
mapping, never hard-coded in a view. A channel may override the organization
default; the role is what the posting service names.

Card takings debit a **clearing asset** rather than cash. The money is real and
the restaurant does not have it, and treating it as cash on hand would make
every cashier closing count short by that day's card volume.

---

## 8. A cashier closing may post exactly one thing

**Decision.** `CashierShift` posts **only** the approved cash over/short
variance:

```
shortage:  Dr SALES_CASH_OVER_SHORT   Cr SALES_CASH_ON_HAND
overage:   Dr SALES_CASH_ON_HAND      Cr SALES_CASH_OVER_SHORT
```

It never posts sales revenue, and the opening float is not revenue either.

**Why this needs saying.** The intuitive design — the closing records the day's
takings — is wrong and wrong in a way that looks right on the screen. The sale
already recognised the revenue and already debited cash; a closing that posted
takings again would double every cash sales figure in the system, and the
duplication would be invisible because both entries would be individually
defensible.

Maker-checker is enforced on the actor: `closed_by` and `approved_by` must
differ, checked in the service **and** by a database constraint, because a
control that lives only in application code is a control that a management
command can walk around.

---

## 9. The kitchen learns about sales without knowing what a sale is

**Decision.** `apps.sales` implements the `TheoreticalConsumptionSource`
protocol and **registers** itself with the kitchen at app-ready.
`apps.kitchen` gains a `register_theoretical_source()` function and imports
nothing from `apps.sales`.

**Why.** The dependency direction is the whole point of the Phase 3 interface.
Sales depends on the kitchen's public quantity-source interface; the kitchen
must never depend on sales models, and Inventory and Accounting must never
import either. Registration inverts the dependency without inverting the
control: the kitchen still decides what a contribution means, what expansion
runs, and what coverage is reported.

**What the adapter subtracts, and what it does not.** Only
`CANCELLED_BEFORE_FULFILLMENT` reduces the quantity fed to theoretical
consumption. `RETURNED_AFTER_FULFILLMENT` does **not**: the food was cooked,
the ingredients left stock, and pretending otherwise would create an
unexplained actual-consumption variance of exactly the returned amount. Where
returned food is physically discarded, that is a Waste document in the
kitchen's own ledger — counting it here as well would be the double count
ADR-026 §4 refuses.

**Coverage stops being a limitation once the adapter is registered.** The
`SALES_NOT_INCLUDED_PHASE_4` code, the `DEFERRED_TO_PHASE_4` status and the
`PARTIAL_COVERAGE` / `NOT_FINAL_USAGE_VARIANCE` labels are now **computed from
the registry** rather than being constants. A deployment where sales is not
installed still reports the Phase 3 limitation honestly; a deployment where it
is reports a final figure.

---

## 10. What Release 1 does not do

Stated rather than left to be discovered:

- **No direct-stock sale.** Release 1 sells `RECIPE_SERVING` menu items only.
  Selling a stocked item straight out of a warehouse needs a COGS-and-issue
  path that no certified service provides, and inventing one here would be a
  second stock-consumption route beside production. `MenuItem.fulfillment_source`
  carries the vocabulary so the addition is data rather than a redesign.
- **No cost of goods sold posting from a sale.** Food cost reaches the ledger
  through production and consumption, which is where the stock actually moves.
  A sale posting COGS as well would count the same rice twice.
- **No tax, and no VAT.** None is configured, none is assumed, and no field
  quietly defaults to zero percent.
- **No point-of-sale integration** and therefore no order-level identity.
- **No customer master and no customer receivable.** The only receivable here
  is from a delivery application.
