# ADR-022 — Supplier return valuation and purchase variance treatment

- **Status:** **Accepted** (Task 2.12, 2026-08-14). Decisions 3, 4 and the price
  half of 5 are implemented; 1, 2 and the return half of 5 remain proposed until
  Task 2.13 posts a supplier return.
- **Date:** 2026-08-11
- **Related:** ADR-006 (rounding), ADR-012 (money and allocation), ADR-013
  (periods), ADR-017 (source identity), ADR-018 (moving weighted average, the
  full-depletion rule), ADR-019 (account roles)
- **Detail:** `docs/tasks/task-2-0-procurement-domain-spec.md` §9, §10

ADR-018 settled what stock is worth while it sits in a warehouse. Procurement
raises the two questions that decision does not answer: what a return is worth
when goods go back out to the supplier, and what happens when an invoice
arrives saying the goods cost something other than what the receipt recorded.

Both have an obvious wrong answer that looks right, which is why they need
writing down.

## Context

The moving weighted average has one property that makes everything else in the
ledger work: **an outbound movement always leaves at the standing average**,
and a movement that empties a balance surrenders its whole remaining book value
(ADR-018, the full-depletion rule). There is one average per
`(warehouse, item, lot)` and no cost layers underneath it. That is a deliberate
simplification, and it is what makes the projection rebuildable from the
movement ledger alone.

Procurement now brings two events that want to disagree with it.

A **supplier return** sends back goods that arrived at a known price. The
supplier will credit that price. But the stock leaving the warehouse has been
averaged together with everything else received since, so what leaves the books
is the average, not the receipt price.

A **price variance** arrives when the invoice states a different figure from
the one the receipt posted. The tempting fix is to go back and reprice the
receipt.

## Decision

### 1. A supplier return leaves stock at the standing moving average

No exception, no per-return cost lookup, no "value at the original receipt
price". The return is an ordinary outbound movement through the ordinary
kernel, and a return that empties a position surrenders its whole remaining
value like any other full depletion.

**Why not value it at the receipt price.** Doing so would require knowing which
receipt's goods physically went back — a cost layer the system does not keep
and, under a moving average, a fiction. If 100 kg arrived at 1,000 and 100 kg
at 2,000, the warehouse holds 200 kg at 1,500. There is no kilogram in that
warehouse that "is" the cheap one. Choosing a layer would mean choosing a
costing method, and a warehouse cannot run moving average for issues and FIFO
for returns without two different answers to "what is this stock worth".

### 2. The difference is a purchase return variance, recognised at return

```
Dr  GRNI or supplier payable      what the supplier credits
    Cr  Inventory control          what left the warehouse, at average
    Cr/Dr Purchase return variance the difference, either direction
```

A return that credits more than it removed produces a gain; less, a loss. Both
are real and both belong on the profit and loss where somebody can see them.

**This is the part that will look like a bug**, so it is worth stating the
worked example in full. Rice arrives twice: 100 kg at 1,000/kg, then 100 kg at
2,000/kg. The average is 1,500. Twenty kilograms from the second delivery go
back, and the supplier credits 2,000/kg:

```
Dr  GRNI                          40,000     20 × 2,000, what is credited
    Cr  Inventory control         30,000     20 × 1,500, what left at average
    Cr  Purchase return variance  10,000     the gain
```

Inventory falls by exactly 30,000 and 180 kg remain at 1,500. The books
reconcile; the 10,000 is not an error, it is the arithmetic consequence of
having averaged two prices together and then unwound one of them.

### 3. A price variance never restates a posted movement

An invoice that disagrees with a receipt does **not** go back and reprice it.

This is the same rule ADR-018 already applies to backdated postings, and for
the same reason: the moving average is a function of **posting order**.
Repricing a receipt would change the average that every subsequent issue was
valued at, so it would restate every movement after it — including movements in
periods that are closed, which the accounting kernel refuses outright.

The variance is recognised where it is discovered:

```
Dr  GRNI                       matched receipt value
Dr  Purchase price variance    the difference, when the invoice is dearer
    Cr  Supplier payable       the invoiced value
```

with the variance line's sign reversed when the invoice is cheaper, and the
line **absent entirely** when the figures agree — a zero-value line is refused
by the kernel and would be noise even if it were not.

### 4. Revaluation of stock still on hand is explicit, permissioned, and off by default

Where the received stock has not yet been consumed, an organization may
legitimately want the price correction carried into inventory value rather than
expensed. That is a **value-only adjustment** — quantity unchanged, average
restated forward from today — which the inventory kernel already supports and
tests.

Release 1 does **not** do this automatically. The default is: variance to the
clearing account, inventory value untouched.

**Amendment (Task 2.12): PRC-044 is formally DEFERRED and NOT ELECTED.** Not
merely "off by default" — unbuilt. The elected path needs a permission code, a
source-document identity, an inventory-versus-cost-of-sales allocation policy,
journal shapes for both directions, per-warehouse and per-lot allocation, and
locking, idempotency, reversal and period-close rules. None of those exist in
any approved document, and this ADR asserted a permission and an audit event it
never defined. A partial implementation would move an inventory figure nobody
could derive from a document they were shown, which is the exact failure
decision 4 exists to prevent.

Task 2.12 therefore posts the whole difference to the clearing account, creates
no inventory movement, and never rewrites a moving average — asserted by tests
on the stock balance's quantity, value and average cost either side of a
posting. The balance stays open and reconcilable by invoice, match and
allocation until the revaluation feature is specified and approved.

**Why not automate it.** Stock is usually partly consumed by the time the
invoice arrives, so an automatic rule has to split the variance between "adjust
the remaining stock" and "expense the consumed part". The split is computable —
proportion on hand at the invoice's business date, allocated with
`apps/core/allocation.py` — but the resulting inventory value is one no
document states and no user can derive from anything they were shown. A cook
looking at a cost report should be able to point at the paper it came from.

Revaluation therefore stays an explicit act, with its own permission and its
own audit event, taken by somebody who knows why.

### 5. Two new account roles, resolved the usual way

| Role | Scope | Account | Carries |
|---|---|---|---|
| `PURCHASE_PRICE_VARIANCE` | organization | `8-01-03-001`, class CLEARING | Invoice-versus-receipt differences |
| `PURCHASE_RETURN_VARIANCE` | organization | to be decided at Task 2.13 | Average-versus-credit differences |

**Amendment (Task 2.12): the variance account is a clearing account, and Task
2.0 §15's `5-02-01-001` is superseded.**

Two independent reasons, either sufficient. Mechanically, class 5 sets
`requires_cost_center`, and a supplier invoice has no cost centre to give: the
document belongs to a branch, not to a department, and
`SupplierInvoiceLine.cost_center` is constrained to direct-account lines.
Substantively, this ADR's own rejected alternatives include *"post the variance
to cost of goods sold directly — conflates a purchasing outcome with a
consumption outcome"*, and `5-02-01-001` is a cost-of-sales code.

So the difference is **parked, not classified**. It sits in
`8-01-03-001 تسوية فروقات أسعار المشتريات`, beside the chart's other
bidirectional difference accounts, and its balance is **expected to be non-zero
and is not a fault**. `verify_parked_variance` checks that every fils in it
traces to a live posting and to the allocation rows beneath that posting, which
is the strongest claim available until the balance is split.

**The split is a required future accounting step.** A later, separately
specified period-end process must distribute this balance between inventory
valuation for quantities still on hand and cost of sales for quantities already
consumed or sold, taking its branch and cost centre from inventory ownership
and consumption — **never** from the supplier invoice. Task 2.12 does not build
it and does not guess at it.

Both organization-scoped, both effective-dated `OrganizationAccountMapping`
rows, both resolved at the business date by the posting service, which names
the **role** and never an account, id or code (ADR-019). A posting whose role
has no mapping on that date fails and rolls back the whole operation.

Neither is item-overridable. A variance is a property of a commercial
transaction, not of the thing bought, and per-item variance accounts would
fragment the one figure a buyer actually wants to look at.

## Consequences

- The moving average keeps exactly one definition across the whole system, and
  the projection stays rebuildable from the movement ledger alone.
- No procurement event can restate a closed period.
- Return variance is visible rather than absorbed, so a supplier who credits
  systematically less than they charge shows up in a report instead of quietly
  eroding food cost.
- Inventory value can lag a late invoice. This is accepted: the alternative is
  an inventory figure that changes retroactively, which is worse for every
  report that has already been printed.
- When a layered costing method is introduced behind ADR-018's strategy
  boundary, decision 1 is the one to revisit — and only then.

## Alternatives rejected

**Value the return at the original receipt price.** Requires cost layers the
system does not keep; would put two costing methods in one warehouse.

**Reprice the receipt when the invoice differs.** Restates history, breaks
closed periods, and makes every previously issued cost a moving target.

**Post the variance to cost of goods sold directly.** Conflates a purchasing
outcome with a consumption outcome. Food cost would then move for reasons that
have nothing to do with the kitchen.

**Capitalise all variance into inventory automatically.** Rejected as decision
4 explains: correct only when nothing has been consumed, which is rarely true
by the time an invoice arrives.
