# ADR-018 — Inventory valuation and the stock ledger

- **Status:** **Proposed.** Awaiting approval of the Task 1.0 decision table.
  Nothing is implemented.
- **Date:** 2026-08-09
- **Related:** ADR-006 (quantity precision), ADR-012 (monetary precision),
  ADR-007/016 (scope), ADR-013 (periods), ADR-017 (source identity)
- **Detail:** `docs/tasks/task-1-0-inventory-domain-spec.md`,
  `docs/invariants/inventory-invariants.md`

This ADR records only what is **durable and not already decided elsewhere**.
The item master, conversions, permissions, API shape, and task order live in
the Task 1.0 specification; they are task output. What follows will still be
true, and still be referenced, in Phase 5.

## Decision

### 1. The valuation key

```
(warehouse, item, lot)
```

`lot` is null for items that do not track lots. Organization and branch are
**derivable** — a warehouse belongs to one branch, which belongs to one
organization — and are stored denormalised on the balance row for tenancy
filtering only, never as part of the identity.

The architecture plan's "Organization + Branch + Warehouse + Item" names the
same key with its derivable parts spelled out. Stating the minimal form
matters: it guarantees exactly one balance row per physical stock position, so
two rows can never disagree about one shelf.

### 2. The warehouse owns value; a location does not

A `StockLocation` (bin) refines *where a thing is*, and carries quantity only.
Moving a box between bins inside one store must not revalue anything, and a
warehouse-level cost must not have to be recomputed by weighted aggregation on
every read.

### 3. Moving weighted average, behind a strategy boundary

`MOVING_WEIGHTED_AVERAGE` is the Release 1 method. `ValuationLayer` and
`ValuationAllocation` are nevertheless recorded from the first posting, even
though the average does not need them.

That is the whole point: with layers captured, introducing FIFO later is a new
consumption strategy over data that already exists. Without them it is a
migration of history that cannot be reconstructed, because the information was
never written down.

### 4. Quantity zero implies value zero, by construction

When an outbound movement brings a balance to exactly zero, it is valued at
the **entire remaining book value**, not at `quantity × average`.

```
if resulting_quantity == 0:
    value_out = remaining_value
else:
    value_out = quantize_money(quantity × average)
```

So `Q == 0 ⟹ V == 0` always holds. There is no residual to allocate, no
adjusting entry, and — decisively — **no mutation of any historical
movement**. The difference is absorbed into the cost of the goods actually
issued, which is where it economically belongs.

### 5. Backdated postings do not restate history

An effective date may be backdated within an `OPEN` period. Valuation follows
**posting order**, never effective-date order: a receipt backdated to the 3rd
and posted on the 10th affects the average from the 10th forward and does not
re-price the issues of the 5th and 7th.

**Quantity as-of a past date is exact. Value as-of a past date is the value
that was known then.** Where a genuine restatement is required, the answer is
an explicit revaluation adjustment with its own audit trail — visible, not
implicit.

### 6. The ledger is the source of truth; the balance is a projection

`StockMovement` is immutable and append-only. `StockBalance` is a cache
carrying `last_movement_id`, rebuildable by replaying the ledger in posting
order. A divergence between them is a defect that fails a test; it is never
repaired by overwriting the projection, because a projection that can be
quietly corrected proves nothing.

### 7. Negative stock is refused

Checked inside the row lock that guards the write, with a database trigger
behind it. Overriding needs a dedicated organization-scoped permission, a
reason, a recorded actor, an audit event, and a standing exception report.
Consumption before a valid receipt makes a moving average unreliable and can
produce values that are not merely wrong but impossible.

## Alternatives considered

- **Value per location.** Correct-looking and wrong: a bin move would create
  gains and losses out of moving a box across a room.
- **Include branch or organization in the valuation key.** Redundant — they
  are functionally determined by the warehouse — and redundancy in an identity
  is an opportunity for two rows to disagree.
- **FIFO from the start.** More precise, materially more machinery, and the
  architecture plan judged moving average appropriate for a restaurant. The
  layer tables keep the door open at low cost.
- **Retro-recalculating on a backdated posting.** What a spreadsheet does, and
  it rewrites movements that are already posted, reported, and reconciled to
  the general ledger. Immutability is a Phase 0 invariant that Inventory does
  not get to relax.
- **Absorbing the zero-quantity residual into a separate adjustment entry.**
  Defensible, and it puts a rounding artefact into the accounts as though it
  were an economic event. Charging it to the goods that actually left is both
  simpler and truer.
- **Allowing negative stock with a later correction.** Every ledger that has
  tried this has discovered that "later" does not arrive.

## Consequences

- One balance row per physical stock position; no aggregation needed to answer
  "what is this worth".
- Introducing FIFO later requires no ledger migration.
- A backdated receipt will surprise anyone expecting spreadsheet recalculation.
  This must be stated in operator training, not discovered.
- Every outbound posting serialises on its balance rows. Throughput is bounded
  by lock contention per `(warehouse, item, lot)`, which is the correct
  granularity — two warehouses never block each other.
- Lot-tracked items hold a separate average per lot, so enabling lot tracking
  on an existing item is a data-migration decision, not a checkbox. Task 1.2
  must refuse to flip `tracks_lots` once movements exist, for the same reason
  the base unit is frozen.

## Open

- Approval of §5 (backdated valuation). It is the one decision here that a
  reasonable accountant might want the other way, and it is durable.
- Whether a periodic revaluation process is needed in Release 1, or whether
  `MANUAL_ADJUSTMENT` suffices until a real case appears.
