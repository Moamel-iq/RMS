# ADR-025 — Production posting, value conservation and reversal

- **Status:** **Accepted** (Task 3.5, 2026-08-18). Proposed by Task 3.0 and
  extended by Task 3.0A; written by the task that first implements its subject,
  as the decisions index requires.
- **Date:** 2026-08-18
- **Related:** ADR-006 (decimal and rounding), ADR-008 (business date),
  ADR-013 (periods), ADR-016 (permission and scope), ADR-017 (source identity
  and idempotency), ADR-018 (inventory valuation and the stock ledger),
  ADR-019 (account roles and domain-owned posting mappings), ADR-024 (recipe
  versioning — where the requirements come from)
- **Detail:** `docs/tasks/task-3-0-recipes-production-domain-spec.md` §8, §8A,
  §15; `docs/development/production-posting.md`

Cooking turns fifty kilos of ingredients into forty-two kilos of rice. This
decision settles what that is worth, where the value goes, and what happens to
the eight kilos that are not there any more.

## Context

A production batch is the first Kitchen event that moves stock. Everything
before it — recipes, versions, costing, drafting — is a claim about money
nobody has spent. Posting spends it, and the spending has to reconcile against
two ledgers that were built before this module existed.

Three questions had to be answered before any of it could be written, and the
first two have obvious wrong answers that would have reconciled.

## Decision

### 1. Value is conserved through the batch, exactly

Each consumed input leaves at the stock kernel's moving average — an ordinary
outbound, under the kernel's own exact-depletion and negative-stock rules. The
output enters at **exactly the sum of what left**, through
`MovementInput.inbound_value`, the exact-figure channel the kernel already
built for returns and transfer receipts.

No value is created, destroyed, or re-derived through `quantity × unit_cost`.
`production_batch_conserves_value` refuses the row where the two disagree, so
this is a property of the schema rather than of one code path.

**The obvious wrong answer** was to value the output at the recipe's standard
cost and journal the difference. It reconciles, it looks like management
accounting, and it is wrong here: this is a moving-average system with **no
approved standard cost**. There is no figure to hold a variance against, so the
"variance" would have been the distance between reality and an unapproved
number somebody typed into a recipe.

### 2. Yield loss is absorbed into the output's unit cost

Fifty kilos of inputs worth 70,000 becoming forty-two kilos of rice makes the
rice worth 70,000, and the unit cost — 70,000 ÷ 42 — says so.

There is **no yield-variance account and no yield-variance journal**. Yield
problems surface where a kitchen manager can act on them: on the productivity
report, and as unit-cost drift. Not in a GL account nobody reconciles.

A future standard-costing election would supersede this section explicitly.

### 3. The journal is the per-account net, and is usually silence

Ingredients leave through the control accounts their **balances** carry
(ADR-019 §7 — an outbound leaves through the account it entered). The output
enters through its own item's resolved `INVENTORY_CONTROL`. Lines are netted
per account and zero-value lines are omitted.

**When every account nets to zero — the common case, one shared inventory
control account — no `JournalEntry` exists at all.** That is a correct posting,
not a failed one. Two entries that always net to zero are motion without
information, and the batch's source identity lives on the **stock ledger entry**
either way, which is where a production event's truth is.

The alternative — washing every batch through a production clearing account so
a journal always exists — was considered and rejected for the same reason.

**The cost of this decision** is that a journal which is rightly absent and one
which is wrongly missing look identical from outside. So `verify_kitchen`'s
proof does not read the column: it **recomputes the per-account nets from the
movements** and asserts each is exactly zero. Only that distinguishes them, and
it is the check that makes the silence safe to allow.

### 4. No WIP, under seven conditions

There is no work-in-progress account, no `PRODUCTION_WIP` movement in Release 1
and no `IN_PROGRESS` status. That is true **only because** a Release 1 batch
satisfies all seven conditions of RCP-094: one business date, one warehouse, no
partial completion, no multi-day state, no period crossing, atomic posting, and
an inert draft that reserves nothing.

The system refuses what it cannot represent rather than approximating it. It
does not post the consumption today and the output tomorrow, does not post a
nil output, and does not silently move the business date — a two-day cook
quietly recorded as a one-day cook produces a number wrong in a way no report
can detect, which is worse than a refusal somebody can escalate.

If those conditions ever stop holding, WIP custody, WIP accounting, separate
issue and completion events, partial-completion arithmetic and a period-boundary
policy must all be specified and approved first.

### 5. One output, and each input its own economic fact

A batch produces exactly one inventory item. Every positive actual row is
posted as its **own** `PRODUCTION_OUT` movement — including two rows against
one requirement when part of it was substituted, and including rows whose base
units are in different dimensions. Nothing invents a KG-to-L ratio and nothing
adds two rows whose dimensions disagree; each is valued separately and the
variance report says "not quantitatively comparable" where that is the honest
answer.

Rows are not aggregated by item either: the same item reached by two component
paths stays two consumptions, because "was the overspend in the dish or in the
blend?" is the batch variance report's whole subject.

### 6. Lots and locations are named, never guessed

Lot-tracked inputs require exact allocation before a batch may post — you
cannot produce from a lot you did not name, and "roughly which batch" is not an
answer a recall can use. Allocation rows must sum to their consumption exactly;
summing to less would post part of what the kitchen recorded using, which is a
partial completion by another name.

An untracked item in a warehouse with no bins needs no allocation row at all.
Requiring an empty formality there would be a form to fill in for the schema's
sake.

When the **output** item tracks lots, posting creates the lot through the
approved Inventory service and writes `produced_by_document_type` and
`produced_by_document_id` — the fields Phase 1 reserved with the comment that
nothing wrote them yet. Expiry follows the item's `shelf_life_days` from the
batch's **business date**, never from today.

### 7. Reversal mirrors, once, and may be refused

Every ingredient returns at the value it left at and the output leaves at the
value it entered at, whatever the averages have since become. The kernel
already refuses when the produced goods are no longer there to take back, and
that refusal is kept: "reverse the batch" must not become the standard way to
drive a position negative.

A batch that correctly wrote no journal writes no reversal journal. Creating one
for symmetry would put a pair of entries into the ledger for an event the ledger
never recorded.

Reversal is once-only, requires a reason that is kept forever, and is an
**elevated** permission separate from posting.

### 8. Identity and idempotency

`source_document_type = KITCHEN_PRODUCTION_BATCH`, `source_document_id =
str(batch.public_id)`, `source_event = POSTED` — and `REVERSED` for the
reversal. `SourceEvent` is **not** extended; two values have sufficed for every
module so far.

Posting carries its own idempotency key, separate from the drafting key,
matched against a request fingerprint and never against the key alone. Drafting
and posting are two commands; one key matched against two fingerprints would
make a retry of either look like a conflict.

### 9. Where the code lives

`apps.kitchen` may not import the stock ledger. Posting therefore goes through
**one** narrow public interface, `apps.inventory.production`, which knows
nothing about recipes: it takes quantities, an output and a source identity.
A boundary test asserts that this is the only inventory posting module Kitchen
imports.

That module solves the one hard problem: the output's value is the sum of the
consumed values, but the consumed values are only known once the kernel has
valued the outbounds — and the whole event must be **one** stock ledger entry,
because a production batch is one economic event with one identity. So it takes
the kernel's locks first, replays `ledger.apply_outbound` in the kernel's own
canonical order to project the value exactly, posts inputs and output together,
and then asserts the projection against what was actually written. It *calls*
the kernel's arithmetic rather than restating it; a second implementation of
the exact-depletion rule would have diverged the first time a batch emptied a
position.

## Consequences

- Value conservation is checkable at the row, and is checked.
- The common batch writes no journal, which surprises readers until they see
  the recomputation that proves the silence is correct.
- Production adds **no** account role, no WIP account, no yield-variance
  account and no clearing account — spec §15, unchanged.
- Multi-day or partially completed production is a specification task, not a
  code change.
- A posted batch is frozen by trigger against every writer including a
  superuser at a psql prompt; correction is reversal plus a fresh draft.

## Rejected alternatives

| Alternative | Why not |
|---|---|
| Value the output at standard cost, journal the variance | No approved standard cost exists; the "variance" would measure distance from an unapproved number |
| Always write a journal, through a production clearing account | Two entries that always net to zero are motion without information |
| Two stock entries, one for inputs and one for the output | One economic event, one source identity; the entry is where the identity lives when there is no journal |
| Recompute `quantity × average` in the Kitchen layer | A second implementation of ADR-018 §4's exact-depletion rule, guaranteed to diverge |
| Aggregate consumptions by item | Destroys the component path, which is the batch variance report's subject |
| Let Kitchen import `apps.inventory.ledger` directly | The dependency rule exists to keep posting arithmetic in one place; one narrow named door is the exception, and it is asserted |
