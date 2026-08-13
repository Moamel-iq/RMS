# Three-way matching

Task 2.11. What a match is, what it refuses to do, and the one sentence an
operator has to believe before using the screens.

## A match decides, it does not pay

**Making a match `READY` posts nothing.** No journal, no stock movement, no
GRNI clearing, no change to the invoice — it is still `APPROVED` afterwards,
and still unpaid. A match is the *evidence* that a supplier's charge is
covered by deliveries the branch actually accepted. Turning that evidence into
an entry is Task 2.12, and until 2.12 ships, a `READY` match is a decision
waiting for one.

The detail screen says this in as many words, in Arabic, above the totals. It
is there because a screen showing a price variance next to a frozen document
looks exactly like a screen that posted it.

## The three documents

| | brings | the question it answers |
|---|---|---|
| Purchase order line | what was agreed | *were we meant to buy this?* |
| Goods receipt line | what was accepted, and what it posted to GRNI | *did it arrive?* |
| Supplier invoice line | what is being charged | *is this the price?* |

The order is optional — a direct market purchase never had one — and where a
receipt names an order line, the allocation copies the **order version** it was
measured against rather than joining to it. A revision agreed next week must
not restate what was matched last week.

## Availability, and why nothing is cached

An allocation may never take more than is left:

- of the delivery line's **accepted** quantity (not its delivered quantity — a
  rejected carton was never received),
- of the invoice line's quantity,
- of the order line's, where there is one.

What is "left" is derived from the allocation rows that still stand, every time
it is asked. There is no `matched_quantity` column anywhere. That is what makes
cancelling a match give the quantity straight back: the released rows stop
counting, and nothing has to be found and corrected. A cached figure would have
to be, and the day somebody cancelled a match inside a failed transaction it
would be wrong with no way to tell.

Two people matching the same remainder at the same moment contend on the same
locked rows, in the documented order — invoice, invoice line, receipt line,
order line, then the allocations. One wins and the other is refused with
`receipt_over_allocation` or `invoice_over_allocation`. Both is impossible.

## Value: a share of what was posted

An allocation's receipt side is a share of the value that **delivery** posted,
taken from the receipt line's own `posted_value` — never from today's moving
average, which has moved since and belongs to different stock. The invoice side
is a share of the invoice line's net, after any freight and discount Task 2.10
allocated.

Partial allocations take a proportional share, and **the last one takes the
exact remainder**. Three allocations against a 1,000.000 delivery come out at
333.333 + 333.334 + 333.333, which sums to 1,000.000 exactly. Rating each share
independently would lose a millifils, and Task 2.12 would then post a GRNI
clearing that did not clear.

## Price variance is a number here, not an entry

    price_variance = invoice_allocated_value - receipt_allocated_value

Positive means the supplier is charging more than the delivery posted. The
database asserts the subtraction, so no path can store a difference its own
components do not support. The header sums it.

That is the whole treatment at this task. The figure is displayed only to
someone holding the cost permission, and the API omits it entirely otherwise —
a null would still tell a warehouse user that a variance exists. Nothing posts
it, `PURCHASE_PRICE_VARIANCE` is deliberately unmapped, and asking an
accounting manager to map an account for a workflow that does not exist is how
a chart of accounts fills up with roles nobody can explain.

## Derived line states

Each invoice line reports one of four states, computed, never stored:

| | |
|---|---|
| `UNMATCHED` | nothing allocated |
| `PARTIALLY_MATCHED` | some quantity covered |
| `MATCHED` | fully covered, and the prices agree |
| `EXCEPTION` | fully covered, but with a variance |

`EXCEPTION` is not an error state. It is a line whose quantities reconcile and
whose money does not, which is precisely the thing three-way matching exists to
find.

## Lifecycle

    DRAFT ──ready──► READY
      │                │
      └──discard       └──cancel (with a reason) ──► CANCELLED

- **`DRAFT`** — allocations may be added and removed freely. Discarding one
  deletes it; it drew no number and burns nothing.
- **`READY`** — frozen. Database triggers refuse every change to the match row
  and to its allocations except the cancellation. It draws a gapless
  `MTC-YYYY-NNNNNN` number at this point, from its own counter rather than the
  procurement document sequence — a match is not a ledger document, and putting
  abandoned drafts through that sequence would leave gaps an auditor reads as
  missing documents.
- **`CANCELLED`** — the correction path, and it needs a reason. The allocation
  rows stay as history but count for nothing: the quantity is released, the
  invoice is free for a replacement match, and the delivery becomes reversible
  again.

There is **no `POSTED`**. Adding one is Task 2.12's decision to make, and the
status enum is asserted not to contain it.

One active match per invoice, enforced by a partial unique index that excludes
cancelled rows. A withdrawn answer is history, not a competing claim.

Retrying `ready` is idempotent — the same allocation set returns the same
frozen match, with the same number and the same timestamp.

## What a match blocks

A posted delivery cited by a **live** match (draft or ready) cannot be
reversed; Task 2.9's guard reports `receipt_has_dependents`. Cancel or discard
the match first and the delivery is reversible again — the reversal guard and
the availability calculation are made to give the same answer on purpose, since
a cancelled match that still held a delivery hostage would make the documented
correction path a dead end.

## Screens

| Screen | Route | For |
|---|---|---|
| Matching queue | `/procurement/matching/` | deliveries nobody has billed yet |
| Matches | `/procurement/matches/` | every match, filterable by status |
| Match detail | `/procurement/matches/<id>/` | allocate, freeze, withdraw |

A match is opened from its invoice — `/procurement/invoices/<id>/match/` —
because opening one is an act on the invoice, not a document created out of
nowhere.

## Permissions

| | |
|---|---|
| `view_purchasematch` | read a match and the queue |
| `match_supplier_invoice` | open, allocate, remove, discard, freeze |
| `cancel_purchase_match` | withdraw an agreed match |

All three are organization-scoped: matching compares documents from more than
one branch, so a branch membership does not reach it. Withdrawing is separate
from matching on purpose — whoever will post from a match is the person who
decides it should stop being the answer. An accountant matches; an accounting
manager withdraws.

## Reconciliation

`verify_matching` (folded into `verify_procurement`) checks four equalities per
source line —

    receipt accepted quantity == active matched + unmatched
    receipt posted value      == active allocated + remaining
    invoice base quantity     == active matched + unmatched
    invoice net amount        == active allocated + remaining

— that no order line is over-allocated, that every stored `price_variance`
equals its own two components, and that **no journal entry or stock movement
cites a purchase match at all**. That last one is the Task 2.12 boundary
asserted as reconciliation rather than only as a unit test: it finds a posting
that crept into the matching workspace whether or not anybody remembered to run
the suite. The verifier reads and reports; it changes nothing.

```bash
python manage.py verify_procurement
```

## Demo

`seed_procurement_demo` adds two matches against the rice invoice: one
allocated and then **cancelled** with a reason, so the release of held quantity
is visible, and its replacement allocated in full and frozen `READY`, carrying
a real positive variance — the invoice bills 1,450 a kilogram against the 1,400
the delivery posted. Both sit on the same invoice, which is exactly what the
one-active-match rule permits.

Nothing in the demo posts. Movement and journal counts are identical before and
after seeding the matches, which is the point.
