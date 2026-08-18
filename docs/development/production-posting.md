# Production posting — the public interface between Kitchen and Inventory

Task 3.5. Companion to `production-drafting.md`, which covers everything up to
the moment a batch commits.

Read ADR-025 first for *why*. This is *how*, and specifically what one module
may call in the other.

## The one door

`apps.kitchen` may not import `apps.inventory.ledger`, `operations`,
`commands`, `transfers`, `adjustments`, `counts`, `opening` or `locations`. A
boundary test asserts it, and asserts that the **only** inventory posting
module Kitchen imports is:

```
apps/inventory/production.py
```

That module knows nothing about recipes, batches or multipliers. It takes
warehouses, quantities, one output and a source identity, and it does what
inventory does with movements. `PRODUCTION_IN` and `PRODUCTION_OUT` have been
inventory's own movement types since Phase 1; this is what finally writes them.

### What it exposes

| Callable | Answers |
|---|---|
| `ProductionConsumption` | one quantity of one item, out of one lot and optionally one bin |
| `ProductionYield` | the one thing the event produces, and the account it enters |
| `resolve_output_control_account` | which `INVENTORY_CONTROL` account, on that date |
| `production_period` | the open period the business date falls in, or a named refusal |
| `project_consumed_value` | what the consumptions will be worth, under the caller's locks |
| `post_production_entry` | one stock entry, inputs and output together, plus the journal if one is needed |
| `reverse_production_entry` | the exact mirror, plus the journal reversal if there was a journal |
| `assert_projection_matched` | the projection against what the kernel actually wrote |

Everything else in `apps.inventory` stays private to inventory.

## The hard part: the output's value

Value conservation says the produced goods are worth exactly what was consumed.
The consumed value is the kernel's moving average and is only known **after**
the outbound movements are valued — but the whole event has to be one stock
ledger entry, because a batch is one economic event with one source identity,
and the entry is where that identity lives when there is no journal.

So the value is projected before the call and posted in it:

1. take the kernel's own locks, in the kernel's own order — warehouse freezes,
   the organization's mapping lock, then every stock key the event touches,
   canonically sorted;
2. read each input position and replay `ledger.apply_outbound` over it, in the
   same canonical order, accumulating positions locally so two effects against
   one position see the second at the average the first left;
3. sum the value deltas — that is the output's `inbound_value`;
4. hand the whole set, inputs and output, to `post_stock_entry`.

Nothing can move between steps 1 and 4 because the locks are already held, and
they are re-entrant, so the kernel's own acquisitions inside `post_stock_entry`
are no-ops.

`assert_projection_matched` then compares the projection against the movements
that were written. It is cheap, it runs on every posting, and it means a future
change to either side is a **failed posting** rather than a silent value leak.

## Lock order

Outwards in, always:

```
kitchen:    batch → requirements → actual rows → allocations
inventory:  warehouse freezes → account mappings → stock keys → posted counter
```

Nothing takes a stock key before a kitchen row, which is why a posting racing
an ordinary issue from the same store cannot deadlock with it. The race suite
exercises both directions.

## What posting writes

| Row | Written |
|---|---|
| `StockLedgerEntry` | one, carrying `KITCHEN_PRODUCTION_BATCH` / `public_id` / `POSTED` |
| `StockMovement` | one `PRODUCTION_OUT` per positive consumption or allocation, one `PRODUCTION_IN` |
| `InventoryLot` | one, only when the output item tracks lots, naming the batch that produced it |
| `StockLocationMovement` | one `PICK` per named bin |
| `JournalEntry` | one, **only** when the per-account nets are non-zero |
| `ProductionBatchAllocation.movement` / `.consumed_value` | written back from what the kernel charged |
| `ProductionBatch` | number, status, posting evidence, values, keys |
| `AuditEvent` | one `POSTED`, and one `REVERSED` on reversal |

## Numbering

`PRD-YYYY-NNNNNN`, gapless per organization and fiscal year, from
`KitchenDocumentSequence` — a third sequence table beside inventory's and
procurement's, because `PRODUCTION_BATCH` is not an inventory document type and
keying it into that enum would make the enum a lie.

The number is drawn **after** every domain reason to refuse has been checked,
including the period, so a refused posting consumes nothing. A gapless sequence
with gaps in it is worse than an honest one.

## Cost visibility

The posting **command** responses carry no money at all. What a posting was
worth lives on one endpoint:

```
GET /api/v1/kitchen/production-batches/{id}/posting
```

behind `view_recipe_cost` **and** `view_production`. Redaction is structural
rather than conditional: a reader without the permission does not receive a
`null`, they receive nothing, which is a different statement.

The Arabic screens follow the same rule — the value column is not rendered at
all, and a test reads the raw bytes of both the permitted and the refused
rendering.

## Verification

`verify_production` composes the draft verifier with the posting verifier and
reports; there is no repair mode.

The check that earns the module is the **no-journal recomputation**: a journal
that is rightly absent and one that is wrongly missing are both
`journal_entry_id IS NULL`, and only recomputing the per-account nets from the
movements tells them apart.

```bash
./.venv/Scripts/python.exe manage.py verify_production --organization KM
```

Exit 1 on defects; observations are reported and never counted against it,
because a red list nobody can clear stops being read within a week.

## What Task 3.5 does not do

- No WIP account, no clearing account, no yield-variance account, no new
  account role.
- No `PRODUCTION_WIP` movement.
- No backflush.
- No partial completion and no multi-day batch — refused by name.
- No consumption or variance reporting; that is Tasks 3.6 and 3.8.
