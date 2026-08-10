# ADR-020 — Transfer ownership, in-transit valuation and cross-branch accounting

- **Status:** **Accepted** (2026-08-10, Task 1.5)
- **Date:** 2026-08-10
- **Related:** ADR-007 (organization and branch boundaries), ADR-008 (business
  date), ADR-015 (cost centres and the branch dimension), ADR-017 (source
  identity), ADR-018 (stock ledger and valuation), ADR-019 (account roles,
  control-account continuity, lock order)
- **Detail:** `docs/tasks/phase-1-task-breakdown.md` Task 1.5

ADR-018 settled how stock is valued at one place. This settles what happens
when goods are between two of them: who owns them, what they are worth on
arrival, and how two branches' books both stay balanced. It is durable
architecture, not Task 1.5 mechanics — Production (Phase 3) and any later
inter-branch flow inherit §8–§10 unchanged.

## Decision

### 1. Goods belong to the source branch until they are received

Stock that has left one warehouse and not arrived at another sits in the
**source branch's** in-transit warehouse, on the source branch's books, from
dispatch until each receipt takes its share out.

That is both the accounting truth and the operational one. If the lorry never
arrives, the loss is the dispatching branch's, and the books should already
say so rather than having to decide afterwards. A destination that owned goods
it had never seen would carry a balance it could not count, could not sell
from, and could not be held to.

### 2. Every branch has one protected in-transit warehouse

`WarehouseType.IN_TRANSIT`, `is_system=True`, one per branch by database
constraint, created on demand by `ensure_in_transit_warehouse`.

**A user never chooses it.** It is absent from every selector, refused by the
transfer service as an endpoint, and refused again by a database trigger for
anything that bypasses the service. Offering it would let somebody dispatch
goods *out of* the place goods in flight are already sitting, or land a
transfer in a warehouse with no physical existence — and the ledger would
have no way to tell either apart from a real movement.

### 3. Dispatch and receipt are separate economic events

A transfer is not one posting. It is dispatched once, received any number of
times, possibly closed short, and each of those individual events can later be
reversed on its own without undoing the others.

So the aggregate's status is **computed** from its posted children and never
written by a caller:

```
DRAFT → DISPATCHED → PARTIALLY_RECEIVED → COMPLETED
                                        → CLOSED_WITH_SHORTAGE
      → REVERSED
```

A status somebody can set is a status that can disagree with the events
underneath it, and "how much of this transfer has arrived" is a question only
those events can answer.

Dispatch itself posts **both halves in one transaction**: the outbound from
the source warehouse, then the inbound into in-transit carrying the *exact*
outbound value. A position emptied to zero surrenders its entire remaining
book value (ADR-018 §4), which is not `quantity × average` and cannot be
predicted before the fact — so the value is read back from the posted movement
and fed in, rather than recomputed. That is what makes a dispatch value-neutral
in every case rather than in the common one.

### 4. A receipt is valued from its own dispatch, never from the in-transit average

One in-transit position pools **every** transfer of that item currently on the
road. Its moving average is therefore a blend of all of them, and valuing a
receipt at that average would take one transfer's quantity out at another
transfer's cost. The difference never comes back: the branches' figures both
look plausible and neither is right.

So each transfer line retains its own `remaining_quantity` and
`remaining_value`, and a receipt consumes a share of *that*. The stock kernel
gained an exact-outbound-value input (`MovementInput.outbound_value`) for
precisely this, as the mirror of the exact-inbound-value input Task 1.4 added.

The kernel refuses an exact value the position cannot support rather than
falling back to the average — `allocated_value_exceeds_position_value`,
`allocated_value_leaves_residual_at_zero_quantity`. That fallback is exactly
what would let a receipt quietly take another transfer's money, so it is
reported as the disagreement it is.

### 5. The allocation rule, and why the last one is special

```
received == remaining  ->  allocated = remaining_value          (exactly)
received <  remaining  ->  allocated = remaining_value × received / remaining
```

then both remaining figures are reduced by what was taken.

The equality branch is what makes the arithmetic close. Computing the final
event's share by the ratio would leave a dinar or two standing against no
quantity, and nothing downstream could ever clear it — the same reasoning as
the kernel's full-depletion rule, applied to an allocation basis instead of a
balance. It guarantees:

```
active received value + active shortage value + remaining value
    == the dispatched value, to the dinar
```

Posting order decides the intermediate rounding; each transfer line allocates
independently; the dispatch movement is never mutated to absorb a residual.

### 6. Partial receipts, and one shortage that closes the rest

Receipts may be partial and there may be many. **A shortage closure resolves
the entire remainder** — a partial write-off leaving an unexplained open
residual is exactly the state the closure exists to end, and offering it as an
option would make it reachable by accident.

Closure is the most sensitive act in the inventory module: it turns stock that
has gone missing into an expense. It requires its own permission
(`inventory.close_transfer_shortage`, answered at the **source branch**), a
non-empty reason, an explicit cost centre, evidence, and an audit event. A
storekeeper does not hold it. An unexplained inventory loss posted to an
expense account with nobody's department carrying it is indistinguishable from
concealing a theft.

### 7. Each posted event snapshots its own business date

Not one date for the whole transfer. Dispatch and shortage take the **source**
branch's operating day; a receipt takes the **destination's** for the arrival
and the **source's** for the in-transit release, and the two may differ —
they are two branches with their own timezones and cutoffs (ADR-008).

Each side validates its own accounting period, and if either is closed the
whole receipt rolls back: stock effects, journals, numbering and all. Nothing
resolves the two branches to a single date, and nothing silently takes the
first one.

### 8. Same-branch accounting

```
dispatch    Dr Inventory In-Transit          Cr Source Inventory Control
receipt     Dr Destination Inventory Control Cr Inventory In-Transit
shortage    Dr Inventory Shortage Loss       Cr Inventory In-Transit
```

One branch-local journal each. The credit side of a receipt or a shortage is
read from the in-transit position's own control account, never resolved
afresh — ADR-019 §7 applied to in-transit stock exactly as to any other.

### 9. Cross-branch accounting uses two coordinated journals

**Not** one journal spanning both branches:

```
    Dr Destination Inventory    Cr Source In-Transit      ← refused
```

That single entry balances for the organization and leaves *each branch's*
standalone trial balance out by the value of the goods — so neither branch can
close its own books, and the error is invisible at the level where anybody
would look for it.

Instead, two journals meeting at inter-branch clearing:

```
source branch, source business date:
    Dr Inter-Branch Clearing         Cr Inventory In-Transit

destination branch, destination business date:
    Dr Destination Inventory Control Cr Inter-Branch Clearing
```

Both carry the same value, are written in one transaction, link to the same
receipt, use separate source identities, and are reversed together. Each branch
is balanced on its own books; the organization's clearing account nets to zero
for the complete event.

`INTER_BRANCH_CLEARING` is an `AccountRole` with **organization** mapping scope
only — it is the account that makes each branch's trial balance sum to zero,
and per-item answers would leave one branch's clearing entry facing a different
account from the other's, netting to nothing at all. An unmapped role fails
with `account_role_unmapped` and rolls back every stock and document effect.

### 10. Corrections are event reversals, never edits

Each event reverses on its own terms:

| Reversing | Allowed when | Effect |
|---|---|---|
| a receipt | the received goods are still at the destination | exact original value back into transit; the transfer reopens for that quantity |
| a shortage | always, while it is the active closure | exact written-off value back into transit; the transfer reopens |
| a dispatch | **no** active receipt and **no** active shortage exist, and the full quantity and value are still in transit | everything back on the source shelf; the transfer becomes REVERSED |

Every reversal mirrors the original's quantity, value, control accounts,
clearing accounts and cost centres exactly, and is dated by the affected
branch's *current* business day — a reversal is a new event now, not a
backdated edit of a closed month.

Availability applies to a reversal exactly as to an issue: a receipt whose
goods have since been consumed cannot be un-received. Exempting reversals
would make "reverse the receipt" the standard way to drive a balance negative,
which is the one thing the check exists to prevent.

Posted rows are immutable by whole-row allowlist trigger; double reversal,
reversal of a reversal, and reversing a dispatch under live children are all
refused.

### 11. Lock order, extended

ADR-019 §6's order, with the aggregate above it:

```
1. the parent transfer row                   select_for_update
2. the child receipt or shortage row         select_for_update
3. the organization's account-mapping lock   shared for postings,
                                             exclusive for mutations
4. the transfer lines being resolved         select_for_update, by primary key
5. every stock key the event touches         advisory, canonical order
6. the inventory posted-sequence counter
7. the domain document-number sequence
8. the journal-number sequence
```

Step 5 is taken **up front, across both sides of the event**, and that is not
a detail. A receipt releases from in-transit and lands at the destination
through two separate kernel calls; letting each sort its own single key orders
them by the order the calls are written rather than canonically. A dispatch of
`W_A → W_B` locks `(W_A, item)` then `(IN_TRANSIT, item)`; a same-branch
receipt of `W_B → W_A` would lock `(IN_TRANSIT, item)` then `(W_A, item)` —
opposite order, same two keys, and the two deadlock. One canonical acquisition
covering the whole event removes the cycle.

When two journals are required, both numbers are allocated inside the same
transaction under the same deterministic acquisition.

### 12. An accounted posting names an account for every dinar

`StockLedgerEntry` now records the journal it produced. If that link is set,
every value-bearing movement under the entry must carry a non-null
`control_account` — enforced by a **deferred** constraint trigger, because
posting legitimately writes the entry, then its movements, then the journal,
and an immediate check would fail on the correct sequence rather than on the
incorrect one.

The column stays nullable. ADR-019 §7 records why: a posting that never
reached the general ledger — the bare kernel, a focused test, a tool with no
accounting in play — genuinely has no account, and inventing one would be
worse than recording that it had none. What this makes impossible is reaching
the GL *without* one.

## Consequences

- A branch's own trial balance stays meaningful across inter-branch movement,
  which is what makes per-branch reporting trustworthy at all.
- Reconciliation gains two independent comparisons: each transfer line against
  its posted children, and the in-transit ledger against the sum of what every
  open transfer claims from it. The first catches a retained balance that has
  drifted from its own events; the second catches a transfer whose children
  are self-consistent but whose stock is not there.
- The retained `remaining_quantity`/`remaining_value` on a transfer line are a
  cache, and are treated as one: maintained under the transfer's row lock,
  bounded by check constraints, and checked against the derived figure by
  reconciliation. Deriving them on every read would make the §5 allocation a
  race.
- Cross-organization transfer stays prohibited and unmodelled. Two
  organizations are two sets of books; goods crossing between them is a sale
  and a purchase with an invoice and a price, not an internal movement at cost.

## Alternatives rejected

**Destination owns the goods from dispatch.** Puts a balance on a branch that
has never seen the stock, cannot count it, and cannot be answerable for it —
and makes a shortage the receiving branch's loss, which nobody would accept.

**One in-transit warehouse for the organization.** Loses the answer to "whose
goods are these", which is the whole question a transfer raises.

**Value receipts at the in-transit moving average.** Simpler, and wrong
whenever two transfers of one item overlap — which is the normal case for a
restaurant group moving the same staples between branches every week.

**One journal for a cross-branch receipt.** Balanced for the organization,
unbalanced for both branches. Rejected in §9.

**Derive the transfer line's remaining balance on every read.** Correct in a
single thread and a race in every other: two concurrent receipts would each
compute the same basis and each allocate against it.
