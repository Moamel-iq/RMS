# Task 1.7B — Stock locations and the location quantity ledger

**Status:** implemented · **Blocks:** Task 1.8 · **Depends on:** 1.7A

Mandatory for Phase 1 exit. Invariant 22 is live in
`docs/invariants/inventory-invariants.md`, is cited by `INV-038`, and Task 1.8's
gate requires every invariant enforced and tested.

## The finding that shapes this task

**The valuation key does not gain a dimension.**

ADR-018 §2 already decided it: *"The warehouse owns value; a location does not.
A `StockLocation` (bin) refines where a thing is, and carries quantity only."*

So `StockBalance`, `_StockKey`, `_lock_stock_key` and the whole moving-average
kernel stay keyed on `(warehouse, item, lot)` and are **not modified**. What
1.7B adds is a second, quantity-only projection beside the valued one.

That is a much smaller blast radius than "locations touch every posting path".
It also means the task cannot be done by widening the stock key — doing so
would revalue stock every time a box moved between bins, which is the exact
outcome ADR-018 forbids.

## What gets built

### Models

    StockLocation          warehouse, code, name_ar, name_en, is_active
    StockLocationBalance   location, item, lot  ->  quantity        (no value)
    StockLocationMovement  the quantity-only effect that changed it

`StockLocationBalance` carries **no value, no average cost and no control
account**. A location holding 5 kg of rice holds no money; the warehouse does.

### The invariant

> Invariant 22 — the sum of a warehouse's location quantities equals its
> warehouse quantity, per `(item, lot)`.

Two halves, and both are needed:

- **Enforced** in the posting path, inside the existing stock-key lock, so a
  concurrent location move cannot break it between check and write.
- **Verified** by extending `verify_stock_projection` with a third comparison
  — the same shape as the ledger↔projection one already there.

Positions with no location are legitimate and are the migration path: a
warehouse that has never used bins has all its quantity "unlocated". The
invariant is therefore `sum(located) + unlocated == warehouse quantity`, not
`sum(located) == warehouse quantity`, and the unlocated bucket is explicit
rather than implied.

### Posting integration

Every path that names a warehouse gains an **optional** location:
opening, receipt, issue, return, transfer dispatch/receipt/shortage, waste,
count, adjustment. Optional is the whole design — a deployment that does not
use bins must be unaffected, and Release 1's existing data has no locations.

### Location transfers

Move quantity between two locations **inside one warehouse**. Posts a
`StockLocationMovement` pair and **no** `StockMovement`, because nothing
entered or left the warehouse and nothing was revalued. This is the case that
proves the split is real.

### Permissions

`manage_locations` (branch scope, MANAGER + OWNER) and `move_location_stock`
(warehouse scope, + STOREKEEPER). Reading locations rides on `view_stock`.

### Demo and visibility

Extends `seed_inventory_demo` — no second command, same five items. Two or
three locations in `DEMO-MAIN`, one located position, one deliberately
unlocated so the reconciliation shows both buckets, and one location-to-location
move. Screens: a location list under the warehouse and a location-balance
report, on the existing shared list infrastructure with htmx filtering.

## Decisions taken during implementation

1. **An issue does not pick a location; the ledger releases one.** Requiring
   every caller to name a bin would have made locations mandatory in all but
   name and would have changed every posting service. Instead
   `release_for_outbound` takes the shortfall from the unlocated pool first and
   then from bins in ascending code order — a deterministic tie-break,
   explicitly **not** FEFO or FIFO, which remain strategies behind ADR-018's
   boundary. A caller that does name a bin gets exactly that bin debited.
2. **An unlocated position does not block anything.** Moving from unlocated
   into a bin is how an existing warehouse adopts them, and staying unlocated
   forever is a supported permanent state.
3. **Locations do not nest.** One level under a warehouse. Nesting is a tree,
   and a tree needs the depth and cycle rules `ItemCategory` carries.
4. **The lock is per `(warehouse, item, lot)`, not per bin.** Two concurrent
   put-aways into different bins compete for the same unlocated remainder; a
   per-bin lock would let both take it. Covered by a real COMMIT-boundary
   test.

## What this task must not do

- Must not widen the valuation key or touch the moving-average kernel.
- Must not add per-key count freezing — that is a different mechanism, still
  deferred, and bundling it would repeat the mistake the 1.7A/1.7B split
  exists to correct. Release 1 counts stay `FULL_WAREHOUSE + HARD_FREEZE`.
- Must not add a repair mode to the projection verifier.
