# Recipe costing — how a number gets onto a cost card

Task 3.3. The companion to `docs/tasks/task-3-0-recipes-production-domain-spec.md`
§6 and §27, written for whoever has to change this code or explain a figure to
an accountant.

Read this before touching `apps/kitchen/costing.py`,
`apps/kitchen/snapshots.py`, or `apps/inventory/valuation.py`.

---

## 1. What a cost is a function of

Four inputs, all named by the caller, **none of them defaulted**:

```
the exact RecipeVersion  ·  the warehouse  ·  the as-of date  ·  POSTED_AS_OF
```

Miss any one and the answer is a different number that looks like the same
number. There is no `today`, no "the current version", no organization-wide
average, no other branch's warehouse, and no second valuation mode.

`cost_recipe_on_date` is the **only** entry point that resolves a version, and
it resolves it the one certified way — `resolve_recipe_version` — then costs
that exact row at the same date. Both halves are date-driven; neither may
silently use today (RCP-026).

## 2. POSTED_AS_OF is the only authoritative basis

Inventory offers two historical modes and they legitimately disagree:

| Mode | Question | Movement set |
|---|---|---|
| `POSTED_AS_OF` | *what did the books say at that moment?* | a **prefix** of the posting order |
| `EFFECTIVE_DATE` | *which movements belong to that period, knowing what we know now?* | not a prefix |

Costing uses `POSTED_AS_OF` and nothing else. The reason is reproducibility: a
prefix of the posting order can be named by a single integer — the
**posted-sequence high-water mark** — and re-read years later. An
`EFFECTIVE_DATE` cost could not be reproduced from a sequence and would disagree
with itself the next time somebody keyed in a late delivery.

`StockMovement.posted_sequence` is unique per organization and allocated under a
row lock held to commit, so numbers are handed out serially: there is no
committed sequence *N* with an uncommitted *N−1* beside it. The maximum
committed sequence at or before a date is therefore a genuine mark with no
holes beneath it.

## 3. One cutoff, captured once

```
cutoff = posted_cutoff(organization=<org>, as_of_date=<date>)   # once
valuations = valuation_at_cutoff(warehouse=<wh>, item_ids=<ids>, cutoff=cutoff)
```

Every position the calculation reads is constrained to that one integer, and the
whole read runs in one transaction. A receipt racing a cost card therefore takes
a sequence **above** the mark and is wholly excluded, or commits before it and
is wholly included. There is no arrangement in which one line of the card sees
it and another does not.

**Costing takes no inventory row locks, deliberately.** Locking stock so a
read-only query "looks safe" would let a reporting screen block a delivery,
which is a worse failure than any it prevents. The cutoff is what buys
consistency; a lock would buy less and cost more.

## 4. The valuation read

`apps/inventory/valuation.py` — read-only, bulk, and caller-agnostic. It exists
beside `reports.stock_valuation` rather than inside it for four reasons, each
load-bearing:

1. **Scope.** `stock_valuation` narrows by inventory's own custody scope.
   Costing is authorized by `kitchen.view_recipe_cost` over the recipe's
   organization, and the caller may hold that with no inventory membership at
   all. This module performs **no authorization** and must never be reached from
   a view without one above it.
2. **Grain.** One figure per item, not one row per lot.
3. **Reproducibility.** A sequence cutoff, not a date predicate.
4. **Availability.** "Valued at zero" and "not valued" are different facts.

### Multiple lots

```
warehouse item quantity = Σ lot quantities
warehouse item value    = Σ lot values
warehouse item average  = total value ÷ total quantity
```

**Never the average of the lots' own averages.** The two agree only when the
lots hold equal quantities. 90 KG at 1,000 plus 10 KG at 2,000 is 1,100 by the
correct method and 1,500 by the wrong one — a 36% error (ADR-018 §4).

### Availability

| State | Meaning | Costable |
|---|---|---|
| `AVAILABLE` | a position with positive quantity | yes — **including at zero value** |
| `NO_POSITION` | no movement ever touched this (warehouse, item) | no |
| `ZERO_QUANTITY` | movements exist, the shelf is empty | no |

A positive quantity with no value is a real zero-cost position — free samples, a
fully written-down lot — and is a cost. An empty position is not a cost of zero:
`value ÷ 0` has no answer, and inventing one would understate every recipe that
names the item.

Expired stock stays in book valuation until Inventory removes it through the
approved waste or adjustment process. Location rows carry quantity only and own
no value; the warehouse figure is authoritative.

## 5. Missing valuation

There is **no fallback**. No last purchase price, no supplier quotation, no
purchase-order price, no replacement cost, no manually entered figure, and no
silent zero.

An unvalued leaf produces a structured `MissingValuation` naming the item, the
component path, the source recipe and version, and a stable code
(`recipe_cost_item_not_valued`). The Arabic panel shows all of it. A card with
any gap:

- still renders, so somebody can see **what** to fix;
- carries `is_complete = False`;
- **cannot become a snapshot** (`recipe_cost_snapshot_requires_complete_cost`).

A costing record with a hole in it is worse than no record, because it looks
like a total.

## 6. The arithmetic

```
effective leaf quantity  = leaf.base_quantity × Π(multipliers on the path)
raw extension            = effective quantity × warehouse unit cost
total material cost      = quantize_money( Σ raw extensions )
allocated line extension = allocate(total, weights = raw extensions)
```

Three deliberate choices:

- **The multiplier product never quantizes on the way down** (RCP-073,
  ADR-006). A gram of saffron three levels deep would otherwise round three
  times before anybody multiplied it by a price.
- **The leaf quantity quantizes exactly once**, at `CALCULATION_PLACES` — the
  precision `RecipeLine.base_quantity` itself carries. Doing it here rather
  than after the multiplication means the number on the card is the number the
  extension was computed from, so a snapshot can be re-verified.
- **The document total quantizes once and is then allocated back to the lines**
  (`CLAUDE.md`, ADR-012). Rating each line, rounding it, and summing is the
  forbidden shape: forty lines each rounded down is a recipe that cost less than
  it cost. `apps/core/allocation.allocate` distributes the residue remainder
  DESC then sequence ASC.

### Required equalities

```
Σ snapshot line values        = total material cost
food + packaging + accompaniment = total material cost
```

The second is a **database check constraint** on `RecipeCostSnapshot`, not a
service assertion. The first is the verifier's job, because no single row can
see it.

### Direct lines

Use the stored `base_quantity`. Never reconvert from the current package
conversion; the line's conversion snapshot is what it meant. Cost the **gross**
issued quantity, include optional lines by default (RCP-021), ignore
`RecipeLineSubstitute` entirely (RCP-022), and preserve `cost_class`, line order
and provenance.

**Never apply `loss_rate` or `cooking_yield`.** They are informational, and the
gross approved quantity already expresses the loss (RCP-018, RCP-060).
Multiplying by them would count it twice.

## 7. Nested components

Follow `RecipeComponent.component_version` **directly**. Never
`resolve_recipe_version` for a child, never the newest child, never the
currently active one. The reference stays valid after the child is superseded,
which is the whole point of freezing it (RCP-072, RCP-081, spec §26.4).

A **stocked** sub-recipe is not expanded at all. Its `output_item` is an
inventory item with a book value that already contains its ingredients;
expanding them too would charge the parent for the ingredients *and* for the
blend they became (RCP-071). The mutual exclusion that makes this unrepresentable
is a database constraint, not a rule the walk has to remember.

The same item on two paths is **two rows**. Its unit cost is fetched once for the
whole card, so the rows are priced identically and the class totals still sum
exactly — but the paths stay separate, because a card exists to be traced.

### Ordering

```
component line_order path  →  leaf RecipeLine line_order  →  item code
```

Never primary-key order. Two databases restored differently must produce the
same card, or a diff between two snapshots is unreadable.

### A corrupt graph

The walk **refuses**; it does not truncate and it does not recurse. A walk that
silently stopped at the depth limit would return a total that is too small and
still look like an answer (`recipe_cost_graph_cycle`,
`recipe_cost_graph_too_deep`).

## 8. Output-unit, plate and serving cost

```
cost per output unit = total ÷ expected_output_quantity      (6 dp, a rate)
cost_per_serving(s)  = total × factor_of_batch(s)            (6 dp, a rate)
portions_per_batch   = expected_output ÷ q(primary)
plate cost           = total × factor_of_batch(primary)      (6 dp, a rate)
```

All **rates**, quantized once to `UNIT_PRICE_PLACES` because a unit cost is not
a posted amount (RCP-086).

### The plate basis

The **primary `RecipeServing`** is the plate divisor. No model in this
repository carries a `portions_per_batch` column — Task 3.0 §3 sketched one on
`Recipe` and Task 3.1 did not build it — and adding one now would be a second,
*mutable* statement of a fact the serving already holds exactly. RCP-084
guarantees exactly one primary per version with a partial unique index, so the
divisor is unambiguous and frozen with the version.

`plate_cost` is computed as `total × factor_of_batch`, not `total ÷
portions_per_batch`. Algebraically identical, and deliberately the first: it uses
the version's own frozen twelve-place factor, so plate cost equals the primary
serving's `cost_per_serving` **exactly** rather than usually, and a snapshot
reproduces it from stored columns.

An authoritative version always has a primary serving — submission refuses one
without — and a test verifies that rather than assuming it. A **preview** of a
draft can lack one, and gets `recipe_cost_no_primary_serving` with the rest of
the card intact. No snapshot may be written without a plate basis
(`recipe_cost_snapshot_requires_plate_basis`).

### The allocation, and why it is compact rather than capped

For each serving definition independently, the exact total is divided across the
whole servings the output makes, plus one weight for whatever output is left
over. The parts sum to the recipe total to the fils (RCP-087). The leftover
carries cost because it is output the batch paid for: dropping it would make the
scenario sum to less than the recipe, and inflating the whole servings to absorb
it would overstate what one serving cost.

**No count is too large.** Every whole serving carries equal weight, so the
certified largest-remainder allocator can only produce **two** amounts: a floor,
and that floor plus one fils for however many servings the residue reaches.
Recording both amounts, both counts and the leftover *is* the distribution —
the per-serving list is reconstructible and adds nothing:

```
normal_count × normal_amount
+ elevated_count × elevated_amount
+ leftover_output_cost
= the recipe total, exactly
```

`_compact_allocation` computes that analytically, in constant work and constant
storage, so 50,000 portions cost what 10 cost and are stored in one row rather
than fifty thousand. It is a **derivation** of the certified allocator, not a
second opinion: a parametrised test holds the two against each other for every
small case.

`MAX_ENUMERATED_SERVINGS` survives only as the limit on how many example rows a
**screen** may list. It decides no business calculation.

**Serving definitions are alternatives, never simultaneous.** Each scenario
allocates the *whole* total; adding two together would double the recipe.

`rounding_increment` and `rounding_policy` govern **planning counts only** and
never touch money (RCP-085).

A physical factor of 0.500 does not imply half the selling price, half the
packaging, or half a commercial recipe's cost (RCP-124). No dish, cut, serving
name or gram figure appears in any Phase 3 service, model, constant, migration
or template; a convention test holds that line (RCP-082).

## 9. Preview versus authoritative

| Version status | Preview | Authoritative | Snapshot |
|---|---|---|---|
| `DRAFT`, `SUBMITTED` | yes, `is_authoritative = False` | no | **no** |
| `APPROVED`, `ACTIVE`, `SUPERSEDED` | no | yes | yes |
| `REJECTED` | no | no | no |

The preview exists because the accountant's signature on `KM-RCP-004` is a
signature on the *costing evidence*, and asking for it while refusing to show
the figures would be asking for a signature on nothing. It does not weaken
RCP-015: only an approved structure is authoritative, and a preview is never a
historical answer.

Which path a card takes is decided by the **stored status**, never by a
caller-supplied flag.

## 10. Snapshots

Three tables, all **append-only** at the database (migration 0009). A trigger
refuses UPDATE and DELETE for everyone including a superuser at a psql prompt.
Admin is read-only. There is no edit route, no delete route, and no archive flag
that hides one. **A correction is a new snapshot.**

`RecipeCostSnapshot` keeps the exact version, branch, warehouse, as-of date,
valuation mode, `ledger_cutoff_sequence`, calculation version, the version's
status *at that moment*, the three class totals, the total, the output-unit
rate, the **plate basis** (`portions_per_batch`, `plate_cost` and the primary
serving's code), who and when, the purpose fields, and the idempotency evidence.
The primary serving's own quantity, unit and frozen factor sit on its
`RecipeCostSnapshotServing` row, so nothing about the historical plate cost
depends on a serving somebody may since have renamed.
`RecipeCostSnapshotLine` keeps every identity twice — a `PROTECT` foreign key so
it stays joinable and denormalised text so it stays readable after a rename.
`RecipeCostSnapshotServing` keeps the rate, the compact allocation (both
amounts, both counts and the leftover), and the identity of the serving it
describes.

### Idempotency

Organization-scoped, on a key **and** a fingerprint of the request, never the
key alone. The fingerprint covers the version's public id, the warehouse, the
date, the calculation version and the purpose inputs — and deliberately **not**
the resulting figures, because two identical requests a week apart legitimately
produce different totals and hashing the answer would turn every honest re-run
into a permanent conflict.

Two intentional snapshots of the same version, warehouse and date are allowed
and expected: a menu is repriced more than once. There is deliberately **no**
uniqueness constraint on `(version, warehouse, as_of_date)` — one would forbid
the second decision in the name of preventing a duplicate the key already
prevents.

### The verifier

`manage.py verify_recipe_cost_snapshots` — report-only, no repair, and the
tables would refuse a repair anyway.

It checks **internal coherence**: the header total against its lines, the class
totals against the lines of each class, line numbering and duplicate paths, every
stored extension against `quantity × unit cost`, each unit cost against the
valuation evidence beside it, the organization/branch/warehouse/version
identities, the calculation version, the valuation mode, each serving
allocation against the total, and the idempotency evidence.

**It never compares a snapshot against today's inventory.** Stock moved; that is
what stock does, and a March snapshot whose items cost more in September is
correct in every particular. A red list full of those would stop being read.

`--recompute` is the explicit second mode: re-read the ledger at each snapshot's
own recorded cutoff and re-derive its unit costs. Off by default because it is a
query per item and only meaningful while the movements behind that cutoff still
exist.

## 11. Permissions

`kitchen.view_recipe_cost`, organization-scoped, held by **OWNER, MANAGER,
ACCOUNTING_MANAGER and ACCOUNTANT** — the map Task 3.1 decided and Task 3.3
activates without widening. A storekeeper reads the card and not the cost, which
is the same boundary that keeps stock valuation away from the person counting
the shelves.

- Foreign recipe, version, snapshot or warehouse → **404**.
- In scope without the permission → **403**.
- `view_recipe` alone, or `review_recipe_version` alone, exposes no money.
- A global Django group with no organizational reach authorizes nothing.
- HTMX and full-page paths run identical checks.

**Cost keys are omitted, never blanked.** A null tells the reader a number
exists and that they are not trusted with it, which is a different statement
from the one intended. Tests read raw response bytes for exactly that reason.

## 12. Routes

```
GET  /api/v1/kitchen/recipe-versions/{id}/cost-preview
GET  /api/v1/kitchen/recipe-versions/{id}/cost
GET  /api/v1/kitchen/recipes/{id}/cost-on-date
POST /api/v1/kitchen/recipe-versions/{id}/cost-snapshots
GET  /api/v1/kitchen/recipe-cost-snapshots
GET  /api/v1/kitchen/recipe-cost-snapshots/{id}
```

No `PATCH`, no `DELETE`, and no generic writable CRUD for snapshots. Every
Decimal crosses as a **quoted string** in both directions: JSON's only numeric
type is binary floating point, and a costing figure that has been through a
float is no longer the figure that was approved.

Screens: `kitchen:cost_card`, `kitchen:cost_on_date`,
`kitchen:cost_snapshot_create`, `kitchen:cost_snapshot_list`,
`kitchen:cost_snapshot_detail`. Every HTMX interaction has a full-page fallback.

## 13. What Task 3.3 must never grow

No selling price, cost percentage, profit margin, contribution margin,
commission, franchise fee, labour, gas, utilities, overhead, or net profit. No
`ProductionBatch`, production planning, material issue, waste, inventory
movement, `StockBalance` mutation, journal entry, menu item, or recipe import.

No cost field on `Recipe` or `RecipeVersion` — a stored "current cost" would be
a copy of the ledger's moving average that starts drifting the moment the next
receipt posts (RCP-009), and it is exactly the field that arrives one day as a
small optimisation. A test holds that line.

The label on every screen is **`كلفة المواد المباشرة`** — direct material cost —
because that is what the number is.

## 14. What the later tasks own

**Task 3.4** — production-batch drafting, expanding a version into batch lines,
planned and actual quantities. Flattening must follow
`RecipeComponent.component_version` directly for the same reason costing does:
re-resolving "the currently effective child" by date would be the silent
re-pointing RCP-072 forbids, arriving through the back door.

**Task 3.5** — production posting, actual input valuation, output inventory
value, and stock and GL effects. It also owns the *other* half of RCP-087: the
exact allocation of a **posted batch's actual** cost across the servings it
really produced. That is a different figure from the standard plate cost built
here, and it needs a posted batch to exist.

Neither owns standard plate cost. That is this task's, and it is built.

---

## Amended by Task 3.4 — costing now shares its expansion

The walk described above is no longer costing's own. Task 3.4 needed the
identical traversal to flatten a production draft, so it moved to
`apps/kitchen/expansion.py` and **costing was moved onto it in the same task**
rather than left with a copy.

What changed for a reader of `costing.py`:

- `_collect_leaves` is gone. `expand_recipe_version(version)` returns
  `ExpandedLeaf` rows and `_KIND_FROM_LEAF` maps `LeafKind` to `CostLineKind`.
- The two graph refusals are now `recipe_expansion_graph_cycle` and
  `recipe_expansion_graph_too_deep`. They were `recipe_cost_*`; a production
  draft refused for a cycle is not a costing failure, and the code a caller sees
  should say what actually happened. The two costing tests that named the old
  codes were **rewritten, not deleted** — the behaviour they guard is unchanged
  and only the name moved with the walk.
- `MAX_COMPONENT_DEPTH` is read from `expansion`, so a test that monkeypatches
  the depth limit patches it there.

What did **not** change: the cost card, the plate cost, the serving allocation,
the snapshot columns, the valuation cutoff, and every figure any of them
produces. The full costing regression — 88 tests — passes unchanged on the
shared engine, and that was the condition for making the move at all.

Nothing about costing reaches into production, and production reads no money.
The engine carries no cost, resolves no date, touches no warehouse and writes no
row — which is exactly what lets costing multiply its leaves by a warehouse
average and production multiply the same leaves by a batch multiplier without
either inheriting the other's assumptions.
