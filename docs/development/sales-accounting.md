# Sales accounting — every journal Phase 4 writes, and the ones it refuses to

**Status:** current as of checkpoint 7, 2026-08-19.
**Decisions:** ADR-027 (recognition and application receivables), ADR-028
(discounts, commissions and settlements). Where this document and an ADR
disagree, the ADR wins and this document is wrong.

This is the reference for anybody reading a Sales journal and asking why it has
the lines it has. Four documents post, and a fifth — the daily reconciliation —
posts nothing and never will.

---

## The eleven roles

Seeded by `accounting.0015_sales_account_roles`, all organization-scoped. Per
application and per channel overrides live on the sales master data that owns
the concept; accounting never learns what a delivery application is (ADR-019).

| Role | Default account | Reached by |
|---|---|---|
| `SALES_REVENUE` | `4-01-01-001` | the sale, and **nothing else, ever** |
| `SALES_DISCOUNT` | `4-02-01-001` | the sale, and credited back by an adjustment |
| `SALES_RETURNS` | `4-03-01-001` | the adjustment |
| `SALES_CASH_ON_HAND` | `1-01-01-001` | the sale, the adjustment, the closing |
| `SALES_CARD_CLEARING` | `1-01-03-001` | the sale and the adjustment |
| `DELIVERY_APP_RECEIVABLE` | `1-02-01-009` | the sale, the adjustment, the settlement |
| `DELIVERY_COMMISSION_EXPENSE` | `6-03-01-001` | the sale and the adjustment — **never the settlement** |
| `DELIVERY_OTHER_FEE_EXPENSE` | `6-03-01-002` | the sale and the adjustment |
| `DELIVERY_SETTLEMENT_VARIANCE` | `7-09-05-001` | the settlement |
| `SALES_SETTLEMENT_BANK` | `1-01-02-001` | the settlement |
| `SALES_CASH_OVER_SHORT` | `7-09-06-001` | the closing |

Classes 4, 5 and 6 require a cost centre, and it comes from the **channel** —
dine-in earns in the hall, application orders earn in delivery. A sale never
invents one and neither does its reversal. Where a role is ever mapped to an
account that requires a cost centre the settlement and the closing have no
principled source for, the service **refuses** with `cost_center_required`
rather than inventing a dimension.

---

## 1. The sale — `SALES.SALESDAY`

`accounting_date = document_date = business_date`, `source_event = POSTED`.
Amounts accumulate **per account** across lines and net once, so a day with two
hundred lines produces a journal a human can read.

For a cash or card line (ADR-027 §7):

```
Dr  SALES_CASH_ON_HAND / SALES_CARD_CLEARING   gross − restaurant discount
Dr  SALES_DISCOUNT                             restaurant-funded discount
    Cr  SALES_REVENUE                                             gross
```

For an application line (§6):

```
Dr  DELIVERY_APP_RECEIVABLE      gross − restaurant discount − commission − fees
Dr  SALES_DISCOUNT               restaurant-funded discount
Dr  DELIVERY_COMMISSION_EXPENSE  commission
Dr  DELIVERY_OTHER_FEE_EXPENSE   other fees, where any
    Cr  SALES_REVENUE                                                    gross
```

**Revenue is credited gross, always.** A deduction is a separate line beside it,
never a subtraction inside the credit: a revenue figure with discounts already
inside it cannot answer "what did we give away", and every deduction would then
be double counted.

**The application-funded discount appears in neither journal**, and that absence
is the single most consequential line in the module. The application reimburses
it, so it reduces neither revenue nor the amount owed. Posting it as a
restaurant discount would understate both revenue and the receivable by the same
amount, and both figures would look internally consistent afterwards
(ADR-028 §3).

Card takings go to a **clearing asset**, not to cash. The money is real and the
restaurant does not have it; treating it as cash on hand would make every
cashier closing count short by that day's card volume.

### The receivable it writes

One `ApplicationReceivableEntry` per application, `source = SALE_POSTED`,
`debit = Σ net_amount`. Reversal writes the mirror with `source =
SALE_REVERSED`, reading the amount **from the ledger** rather than recomputing
it from the lines — recomputing would silently pick up any master-data change
since, which is exactly the drift a reversal must not introduce.

---

## 2. The adjustment — `SALES.SALESADJUSTMENT`

`accounting_date = document_date = the adjustment's own business date`. All
three reason kinds post this same journal.

Against a cash or card line:

```
Dr  SALES_RETURNS                                 adjusted gross
    Cr  SALES_DISCOUNT                            adjusted restaurant discount
    Cr  SALES_CASH_ON_HAND | SALES_CARD_CLEARING  adjusted net
```

Against an application line:

```
Dr  SALES_RETURNS                                 adjusted gross
    Cr  SALES_DISCOUNT                            adjusted restaurant discount
    Cr  DELIVERY_COMMISSION_EXPENSE               adjusted commission
    Cr  DELIVERY_OTHER_FEE_EXPENSE                adjusted other fees
    Cr  DELIVERY_APP_RECEIVABLE                   adjusted net
```

Balanced by construction, because `net = gross − restaurant discount −
commission − other fees` on the line it corrects.

**`SALES_REVENUE` is never touched.** Debiting revenue would restate a posted
gross figure and destroy ADR-027 §2's whole point — that revenue is gross and
every deduction sits beside it as an identifiable claim. `SALES_RETURNS` exists
for exactly this, and is kept apart from `SALES_DISCOUNT` because a discount is
a pricing decision made **before** the sale and a return is a sale that stopped
being one **after** it. `verify_sales` check 7 is that assertion.

### The receivable, and the one awkward corner

Posting writes `source = AUTHORIZED_ADJUSTMENT`, `credit = Σ adjusted net`.
Reversing writes the mirror with the **same** source value and
`source_document_id = f"{public_id}:REVERSED"`.

The suffix is necessary and is not a hack. The uniqueness key is
`(organization, application, source, type, id)`; sale and settlement reversals
get a distinct `source` value, but ADR-027 §5 fixes the vocabulary at five and
there is no `ADJUSTMENT_REVERSED`. The only free component left is the document
id, so the reversal names itself there — in the one field the canonicaliser
deliberately does **not** case-fold. Every reader matches on the `str(public_id)`
prefix.

---

## 3. The settlement — `SALES.DELIVERYAPPLICATIONSETTLEMENT`

`accounting_date = document_date = business_date`.

```
Dr  SALES_SETTLEMENT_BANK | SALES_CASH_ON_HAND    remitted amount
Dr  DELIVERY_SETTLEMENT_VARIANCE                  total variance   (when > 0)
    Cr  DELIVERY_SETTLEMENT_VARIANCE              −total variance  (when < 0)
    Cr  DELIVERY_APP_RECEIVABLE                   expected amount
```

with `total variance = expected − remitted`. The debit side is the asset
actually received plus the shortfall recognised; the credit side is the
receivable cleared. A `remitted amount` of zero is legal — a fully offset
statement — and simply omits that line.

**Commission is never recognised twice.** `statement_commission_amount` is
stored, compared against `accrued_commission_for(settlement)`, shown as
`commission_gap` and reported by `verify_sales` as an **ADVISORY**. It reaches
`DELIVERY_COMMISSION_EXPENSE` **never**. ADR-028 §6 calls this the single most
likely error in the module: commission was accrued at the sale, and expensing it
again at settlement overstates selling expense and understates gross margin by
the same amount — both individually defensible, which is why nobody finds it by
reading. `verify_sales` check 10 exists for exactly this.

Every claimed adjustment, whatever its leg and whatever its reason, posts to
`DELIVERY_SETTLEMENT_VARIANCE` (bidirectional). The leg and the reason are
analytic dimensions on the document, not different accounts; there is no second
variance role and none is invented.

### The receivable

`source = SETTLEMENT`, `credit = expected amount`. Reversal writes
`source = SETTLEMENT_REVERSED`, `debit =` the same amount, the **same** document
id — the paired source value exists here, so no suffix is used or needed.

Reversal also returns the allocations to open. The allocation rows stay, because
they are evidence of what was claimed; `unallocated_debit` excludes allocations
belonging to non-posted settlements, which is why the trigger's sum is over
posted settlements only.

---

## 4. The closing — `SALES.CASHIERSHIFT`

Written at **approval** and nowhere else.

```
shortage  (variance < 0):
    Dr  SALES_CASH_OVER_SHORT        |variance|
        Cr  SALES_CASH_ON_HAND       |variance|

overage   (variance > 0):
    Dr  SALES_CASH_ON_HAND            variance
        Cr  SALES_CASH_OVER_SHORT     variance
```

A variance of exactly zero posts **no journal at all** and is a legitimate
outcome, not an error: the shift still reaches `APPROVED` and still takes a
number, because the document exists whether or not it moved money.

It does **not** post sales revenue. It does **not** post the day's takings. It
does **not** post the opening float, which is neither revenue nor an economic
event. It does **not** post card takings, which sit in `SALES_CARD_CLEARING`
until the acquirer remits and are not in the drawer to be counted. It does
**not** write an `ApplicationReceivableEntry`.

The reason this needs stating, from ADR-027 §8: the intuitive design — the
closing records the day's takings — is wrong in a way that looks right on
screen. The sale already recognised the revenue and already debited
`SALES_CASH_ON_HAND` when the day posted. A closing that posted takings again
would double every cash sales figure in the system, and the duplication would be
invisible, because both entries would be individually defensible and both would
name a real document. `verify_sales` check 11 asserts the journal touches
exactly two accounts.

---

## 5. Reversals

Every reversal in this module goes through `reverse_entry`, which appends the
mirror image and leaves the original standing. Nothing is re-decided: the
question "what did this document do" has one answer and it was settled the day
it posted. Only the reversal's **date** is current, because undoing something is
an event that happens now.

Idempotency keys are derived from the document's own `public_id`, never accepted
from a caller:

| Document | Posting key | Reversal key |
|---|---|---|
| Sales day | `sales-day:{public_id}` | `sales-day-reversal:{public_id}` |
| Adjustment | `sales-adjustment:{public_id}` | `sales-adjustment-reversal:{public_id}` |
| Settlement | `application-settlement:{public_id}` | `application-settlement-reversal:{public_id}` |
| Cashier shift | `cashier-shift:{public_id}` | `cashier-shift-reversal:{public_id}` |

Posting *this document* is the command, the document is the payload, and a
posted document is frozen by a database trigger — so a retry cannot present the
same key with a different payload, and there is no second key vocabulary for
anybody to get wrong.

---

## 6. What a source document type is called

Stored **upper-case with the dot retained**, because
`canonical_source_identity` does `strip().upper()` on the type. A constant
spelled `sales.SalesDay` writes `SALES.SALESDAY` and then fails to find itself —
a reversal that cannot locate its own journal. The four constants are spelled
the way they are stored:

```
SALES.SALESDAY
SALES.SALESADJUSTMENT
SALES.DELIVERYAPPLICATIONSETTLEMENT
SALES.CASHIERSHIFT
```

`verify_sales` check 12 asserts the case, because this has already bitten once.

---

## 7. Lock order

Extends the documented global order rather than reinterpreting it. For every
posting service in this module:

1. the document row — `select_for_update`
2. mapping resolution
3. the sales document-number counter — `select_for_update`
4. the journal-number counter — inside `post_entry`

Nothing takes a row lock between steps 2 and 4.

---

## 8. Reading the accounts back

`manage.py verify_sales` composes every equation above and reports three
severities. It exits non-zero only for `ERROR`; a commission gap with a delivery
company and a drawer counted but not yet approved are an `ADVISORY` and a
`COVERAGE_LIMITATION` respectively, and neither is a defect.

There is no `--fix`, no `--repair` and no `--rebuild` (RCP-050). A verifier that
could change the thing it verifies is a verifier nobody can trust.
