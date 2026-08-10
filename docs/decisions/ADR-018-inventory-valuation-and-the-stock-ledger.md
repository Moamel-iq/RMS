# ADR-018 — Inventory valuation and the stock ledger

- **Status:** **Accepted** (2026-08-09, with amendments). The stock ledger
  itself is delivered by Task 1.2; Task 1.1 delivers only the master data it
  will reference.
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

**Null lots must not be left to SQL's uniqueness semantics.** In standard SQL
`NULL` is distinct from every `NULL`, so a plain
`UNIQUE (warehouse, item, lot)` would permit unlimited rows for a
non-lot-tracked item — precisely the case that must have exactly one. The
guarantee is therefore stated explicitly, one of:

```python
UniqueConstraint(
    fields=("warehouse", "item", "lot"),
    nulls_distinct=False,  # Django 5.0+, PostgreSQL 15+
    name="stock_balance_key_unique",
)
```

or, where that is unavailable, two partial constraints that together cover
every row:

```sql
UNIQUE (warehouse, item)       WHERE lot_id IS NULL
UNIQUE (warehouse, item, lot)  WHERE lot_id IS NOT NULL
```

Django 5.2 and PostgreSQL 18 are both in use here, so `nulls_distinct=False`
is available and is the form Task 1.2 should take. The partial pair is
recorded as the fallback, not as a preference.

### 2. The warehouse owns value; a location does not

A `StockLocation` (bin) refines *where a thing is*, and carries quantity only.
Moving a box between bins inside one store must not revalue anything, and a
warehouse-level cost must not have to be recomputed by weighted aggregation on
every read.

### 3. Moving weighted average, behind a strategy boundary
*(amended at Task 1.3 to match the implemented behaviour)*

`MOVING_WEIGHTED_AVERAGE` is the Release 1 method. **`ValuationLayer` records
every inbound cost fact from the first posting; `ValuationAllocation` records
nothing and stays empty.** A moving average does not consume a layer — it
charges the blended cost of everything on hand — so an allocation row under
this method would fabricate FIFO-layer consumption that never happened.
`ValuationAllocation` remains empty until a valuation strategy that actually
allocates layers is enabled, and an invariant test holds the line: posting an
outbound movement under moving average creates no allocation rows.

The layers are still the point: with inbound cost facts captured, and with
the immutable movement history alongside them, a future FIFO cutover is
**possible** — the consumption for past periods is derivable from data that
already exists. Stated honestly, though: switching to FIFO is a **controlled
cutover with an explicit rebuild policy** — recomputing allocations, agreeing
a cutover date, and reconciling the restated values — not a configuration
toggle with zero migration work. What the layers buy is that the cutover is
*computable*; nothing makes it free.

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

### 7. Negative stock is refused — including on reversal

Checked inside the row lock that guards the write, with a database trigger
behind it. Overriding needs a dedicated organization-scoped permission, a
reason, a recorded actor, an audit event, and a standing exception report.
Consumption before a valid receipt makes a moving average unreliable and can
produce values that are not merely wrong but impossible.

**A reversal mirrors its original's quantity and value, but a reversal that
*decreases* current stock still passes the availability check.** Reversing an
untouched receipt is fine. Reversing a receipt whose goods have already been
issued is refused, because the stock is no longer there to take back — the
dependent effects must be corrected first, or an explicitly authorised
exception used.

Exempting reversals from the check would make "reverse the receipt" the
standard way to drive a balance negative, which is the one thing the check
exists to prevent.

There is **no permanent per-item flag that allows negative stock.** An
override is a per-posting exception with a named actor and a reason, never a
property of the item that quietly applies forever.

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

## Amendments applied at acceptance (2026-08-09)

**1. No periodic revaluation engine in Release 1.** An explicit, approved
manual revaluation through `MANUAL_ADJUSTMENT` is sufficient. A scheduled
revaluation process is not built until a real case demands one; building it
speculatively would add a second thing that changes valuation without a
business event behind it.

**2. Every movement retains three distinct times**, and reports must say which
they use:

| Field | Meaning |
|---|---|
| `effective_at` | when it happened in the business |
| `posted_at` | when it entered the ledger |
| `posted_sequence` | the total order valuation was computed in |

This gives two legitimate historical views, and they answer different
questions:

| Mode | Ordered by | Answers |
|---|---|---|
| **Effective-date view** | `effective_at`, as currently known | "What did we hold on the 5th, given everything we now know?" |
| **Posted-as-of view** | `posted_sequence` up to a cutoff | "What did the books say on the 5th, as they stood then?" |

A report must name which it uses. **"As of" alone is forbidden** — the two
diverge exactly when a backdated movement exists, which is exactly when
someone is looking, and a report that does not say which one it means is
worse than no report.

**3. A reversal is not exempt from the negative-stock check.** See §7.

**4. Positive count adjustments never create quantity at zero value.** See §8.

## §8 — Valuing a positive count adjustment

A count gain adds quantity that the ledger did not know about. It must arrive
with a real cost:

- If the current balance has **positive quantity and a valid positive
  average**, the approved policy may value the gain at that average.
- If quantity is **zero**, or the average is zero or undefined, an
  **explicit approved unit cost is required**.

Creating positive quantity at zero value because the previous balance happened
to be zero would put free stock on the books and understate cost of sales for
as long as it lasted.

## Open

Nothing blocking. The two questions raised at proposal are resolved above.
## Amendments applied at implementation (Task 1.2, 2026-08-09)

**5. The posted-order counter is an organization-scoped row, and it costs
throughput.** Valuation follows posting order, so the ledger needs a total
order two concurrent postings cannot both claim. `StockPostingSequence` is one
row per organization, taken under `SELECT ... FOR UPDATE`: gapless,
deterministic, and scoped to the organization it orders.

Stated plainly rather than buried: taking that lock **serialises postings
within one organization** for the rest of their transaction. That is a
stronger bound than the Consequences section above anticipated, where
contention was per `(warehouse, item, lot)`. It is accepted at restaurant
volumes — a branch posts tens of movements a day, not thousands a second — and
it buys a sequence with no gaps and no dependence on commit order. If it ever
binds, the replacement is a PostgreSQL sequence per organization, trading
gaplessness for concurrency; nothing above the model depends on the numbers
being contiguous.

The lock order is fixed and must stay fixed: **stock keys first, in canonical
`(warehouse, item, lot)` order, then the counter.** The reverse would deadlock
a transaction holding a key and waiting for the counter against one holding
the counter and waiting for that key.

**6. Absent balance rows are locked with a PostgreSQL advisory lock.**
`SELECT ... FOR UPDATE` on a row that does not exist locks nothing, and the
first receipt into a new warehouse is exactly that case — two concurrent ones
would both find no balance and both insert. `pg_advisory_xact_lock` over a
canonical key string exists whether or not the row does, and is released by
commit or rollback with no cleanup path to forget. A hash collision between
two different keys costs needless serialisation and never costs correctness.

**7. Negative stock is refused outright in Task 1.2, for everyone.**
`inventory.override_negative_stock` is reserved vocabulary and is **not
operational**: the kernel consults no permission at all. The reason is not
caution but definition — the approved moving-average contract does not say how
a later receipt settles the valuation variance a negative position creates,
and a permission cannot make an undefined accounting state valid.

A database `CHECK (quantity >= 0)` on `StockBalance` backs this up. **A later
task that activates the override must relax that constraint in the same
migration**, or the override would be refused by the very database it is meant
to be an exception to.

**8. `ValuationAllocation` exists and is deliberately empty.** A moving
average does not consume a layer — it charges the blended cost of everything
on hand — so recording that an issue "took 30 kg from the layer received on
the 3rd" would be a fabrication that looks like evidence. The outbound cost
authority is, and stays, the moving-average snapshot on `StockMovement`.

The table exists because the allocation for a past period is **derivable**:
layers and outbound movements both carry `posted_sequence`, so a future FIFO
migration can compute the consumption it needs from the ledger it already has.
Every row it ever holds must name the strategy that produced it.

**9. Source identity is canonicalised centrally, and asymmetrically.**
`apps/core/source_identity.py` is the single normaliser for accounting,
inventory, and the audit trail:

| Field | Rule | Why |
|---|---|---|
| `source_document_type` | `strip().upper()` | our vocabulary |
| `source_document_id` | `strip()` only | **theirs** — `AB-1042` and `ab-1042` can be two real supplier invoices |
| `source_event` | `strip().upper()` | our vocabulary |

A whitespace-only value is refused rather than read as "absent": swallowing it
would move the posting outside the uniqueness guarantee while leaving it
looking like a manual entry.

**10. A request fingerprint never contains a server-generated timestamp.** The
fingerprint hashes the *caller's* `effective_at`, which is `None` when they
sent none. Hashing the resolved `timezone.now()` instead would give every
retry a different fingerprint and turn idempotency into a permanent
`idempotency_key_conflict` — the exact opposite of what it is for.
