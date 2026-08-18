# ADR-026 — Kitchen custody, consumption, and the boundary of usage variance

- **Status**: Accepted
- **Date**: 2026-08-18
- **Task**: 3.8 — Kitchen flow, actual consumption, theoretical-consumption
  infrastructure and variance analysis
- **Supersedes in part**: Task 3.0 §11.2's batch-consumption formula, and the
  `MATERIAL_RETURN` / `LINKED_WASTE` link vocabulary
- **Related**: ADR-018 (valuation and the stock ledger), ADR-024 (recipe
  versioning and the effective-dated cost basis), ADR-025 (production posting,
  value conservation and reversal)

---

## 1. Context

Task 3.5 made production post: a batch consumes exact quantities out of one
kitchen warehouse, produces one output, and its input value equals its output
value to the fils (RCP-034). Task 3.6 read what production and Inventory had
already done. Task 3.7 recorded staff and complimentary meals, which move no
stock and write no journal.

Task 3.8 has to answer the question those three set up: **what did this kitchen
actually consume, and how does that compare with what it should have?**

The second half of that question cannot be answered in Phase 3, and most of
this ADR is about being precise rather than approximate about which half is
which.

---

## 2. The partition, not a formula

**Decision.** Every posted `StockMovement` at a selected kitchen warehouse is
classified into **exactly one** bucket by one authoritative classifier,
`apps/kitchen/consumption.classify_kitchen_movement`. Consumption is then a
*reading* of that partition, never a separate sum over a hand-picked list of
movement types.

**Why.** A formula is wrong quietly. `PRODUCTION_OUT + ISSUE + WASTE` is correct
until somebody adds a movement type, after which the report is short by exactly
the movements nobody thought about and nothing anywhere says so.

A partition carries its own proof. For each `(warehouse, item, lot)` key over
the window:

```
closing quantity − opening quantity  =  Σ (every bucket's signed total)
```

The left side comes from the kernel's own `quantity_before` / `quantity_after`
columns. The right side is built by adding up the buckets. If a movement reached
the ledger and reached no bucket, the right side is short and the identity fails
visibly (RCP-104). That is the difference between a table of good intentions and
a checkable claim.

The classifier **raises** on an unknown `MovementType` rather than falling
through to an `OTHER` bucket. A default would silently absorb a new movement
type into a bucket nobody chose.

### 2.1 `RETURN_OUT` and `TRANSFER_SHORTAGE`: subcategories, not buckets

`RETURN_OUT` (a supplier return) and `TRANSFER_SHORTAGE` (a loss in transit) are
real movement types a kitchen warehouse can carry, and at first reading neither
had a home in the approved vocabulary.

**A first implementation added two public buckets for them.** That was the wrong
answer, and the reasoning is worth keeping because the mistake is an easy one:
what those movements needed was **drill-down detail**, not a seat in the public
vocabulary that ADR-026, the CSV headers, the API contract and every future
consumer would then have to understand. Widening a public enum looks free and is
not.

Both have homes:

| Movement type | Public bucket | Internal subcategory | Nets against |
|---|---|---|---|
| `RETURN_IN` | `ECONOMIC_RETURN_OR_REVERSAL` | `ISSUE_RETURN_IN` | direct economic consumption |
| `RETURN_OUT` | `ECONOMIC_RETURN_OR_REVERSAL` | `SUPPLIER_RETURN_OUT` | **supply** |
| `TRANSFER_SHORTAGE` | `CUSTODY_TRANSFER_OUT` | `TRANSIT_SHORTAGE_LOSS` | custody out |

A supplier return is a genuine reversal — of a **receipt**, not of a use. A
transfer shortage is stock that left this store's custody and never arrived,
which is custody exactly like the dispatch it closes.

**The subcategory is what makes the arithmetic safe.** `RETURN_IN` and
`RETURN_OUT` share a public bucket, so netting the whole bucket against
consumption would make goods sent back to a supplier look like the kitchen
having cooked less. `direct_economic_consumption` therefore nets only the
`ISSUE_RETURN_IN` share, and `supply_receipt` nets the `SUPPLIER_RETURN_OUT`
share. Verified on the development database: a warehouse with 115 received and
20 returned to the supplier reports net supply 95 and direct consumption 0.

The public vocabulary is the approved **fifteen**. Subcategories are internal,
exist only where a bucket genuinely holds two kinds of event, and are never a
reporting dimension of their own.

---

## 3. Custody movement is not consumption

**Decision.** `TRANSFER_IN` and `TRANSFER_OUT` at a kitchen warehouse are
**custody** buckets. They are reported beside consumption and are never added
to it or subtracted from it, in either direction.

**Why.** Moving rice from the store to the kitchen changes who holds it. Nothing
has been used: the rice is still rice, still on the books, still countable. It
is consumed when a batch cooks it (`PRODUCTION_OUT`) or when somebody issues it
out for use (`ISSUE`).

The charter's original formula added the transfer *and* the production usage,
which counts the same kilogram twice — once when it changed hands and once when
it was cooked. A variance report built on that shows a permanent structural
overage no kitchen can ever explain, and the natural response to an
unexplainable variance is to stop reading the report. Spec §11.1 records the
correction.

**The mirror error is equally wrong and less obvious.** A custody transfer
carrying material *back* to the store is **not** negative production
consumption. Subtracting it from a posted batch's `PRODUCTION_OUT` would credit
the same kilogram twice — once through the transfer's own ledger effect, once
again in the report.

---

## 4. Post-production correction is reversal and repost

**Decision.** A posted `ProductionBatch` is never rewritten. If its actual
inputs were materially wrong, the correction is: **reverse the batch → correct
or replace the draft → repost**.

A later custody transfer from the kitchen back to the store:
- changes custody;
- does **not** reduce the immutable batch's posted `PRODUCTION_OUT`;
- does **not** rewrite the output value;
- does **not** change batch actual consumption.

A later Waste document:
- records abnormal stock loss, with its own reason code, value and journal;
- does **not** rewrite the posted batch;
- does **not** increase the batch's input value a second time.

**Why.** ADR-025 locked `input_value = output_value` on every posted batch. Any
later document that adjusted one side of that equation would either break the
invariant or require restating a journal that has already reached the general
ledger. Reversal and repost keeps both ledgers append-only and leaves the
original mistake visible, which is the standing rule for every posted document
in this system.

This distinction is visible in the services (`batch_actual_consumption` does not
read the link table), in the reports (the attribution panel sits below the
arithmetic and says so), and here.

---

## 5. `BatchDocumentLink` — explanatory attribution only

**Decision.** A kitchen-owned link model attributes one posted Inventory line to
one posted batch, for **explanation only**. Two closed link types:

| Link type | Source family | What it claims |
|---|---|---|
| `CUSTODY_RETURN_CONTEXT` | `inventory.StockTransferLine` | A transfer moved material out of this kitchen store, near this batch |
| `ABNORMAL_WASTE_CONTEXT` | `inventory.InventoryMovementDocumentLine` | A posted Waste document at this store is about this batch |

**The names end in `_CONTEXT` deliberately.** Task 3.0 §11.2 called the first
`MATERIAL_RETURN` and defined batch consumption as
`consumed − linked returns + linked waste`. **That arithmetic is superseded**
(§4 above). Calling a custody transfer a "production return" would tell every
future reader that it reverses `PRODUCTION_OUT`, and the first person to act on
that reading would build a report that double-counts.

### 5.1 Typed foreign keys, not a document-type string

Task 3.0 sketched `document_type` + `document_id` as a UUID pair. **Rejected.**
That is a caller-controlled table name in disguise: nothing in the database can
then check that the id exists, belongs to the right organization, or names a
matching item, and a link pointing at a deleted or foreign row would sit in a
variance report looking exactly like a valid one.

Instead: two nullable `PROTECT` foreign keys, one per source family, with a check
constraint saying exactly one is set **and** that it agrees with `link_type`.
Referential integrity becomes the database's job. `GenericForeignKey` is not
used, for the same reason.

### 5.2 The attribution cap

For any source line, the sum of `attributed_quantity` across every `ACTIVE` link
may not exceed that line's own `base_quantity` (RCP-102), and no two `ACTIVE`
links may point the same batch at the same source line.

Enforced **twice**: in `document_links.py` under `SELECT ... FOR UPDATE`, and by
a `DEFERRABLE INITIALLY DEFERRED` constraint trigger (migration 0023). The
service check produces a readable Arabic refusal; the trigger is what survives
two concurrent writers who each read a total that was fine.

Without the cap, one waste document could be charged in full to three batches
and every batch's variance report would balance against a quantity only one of
them can honestly claim.

### 5.3 One-directional ownership

The model lives in `apps.kitchen`, holds keys **into** `apps.inventory`, and
`apps.inventory` neither imports it nor changes behaviour because of it
(RCP-101). Deleting every row would leave both ledgers byte-identical and only
the kitchen's attribution reports poorer.

A link is cancelled with a reason, never edited and never deleted. An `ACTIVE`
link is immutable at the database except for its cancellation columns.

---

## 6. Two authoritative actual-consumption reads

### 6.1 Batch actual consumption

For one posted batch: its positive `ProductionBatchActualLine` rows and the
`PRODUCTION_OUT` movements the posting made, preserving primary/substitute
identity, source `RecipeLine`, component path, item, unit, quantity, lot and
location allocations, posted value, business date and reversal state.

Two equalities are **reported, not asserted** — this is a report, and
`verify_kitchen` decides whether a mismatch is an error:

```
Σ positive actual quantities (per item)  =  Σ |PRODUCTION_OUT| (per item)
Σ input movement values  =  batch.input_value  =  batch.output_value
```

Per-row posted value comes from `ProductionBatchAllocation.consumed_value` where
the row was allocated, and from the movement keyed
`production-actual:<uuid>` where it was not. **Historical movements are never
repriced**: a purchase made last week must not restate what a batch cost last
month.

A `REVERSED` batch still reports what it consumed, because that happened. Its
net contribution to *current-period* consumption becomes zero once its reversal
movements are in the window, which the period read handles by classification.

### 6.2 Kitchen period actual consumption

Per `(warehouse, item)` over a date range, with every stream reported
separately:

```
NET PRODUCTION CONSUMPTION  = PRODUCTION_OUT − exact reversal of PRODUCTION_OUT
DIRECT ECONOMIC CONSUMPTION = ordinary ISSUE − genuine RETURN_IN − its reversal
```

Custody in, custody out, production output, raw-material waste, produced-output
waste, count corrections and value-only corrections are reported **beside**
those two figures, never inside them. An `economic_outflow` subtotal adds
consumption and abnormal loss; both remain separately readable above it.

### 6.3 Waste is classified by what was lost

Wasting 3 kg of raw onions is ingredient loss. Wasting 3 kg of *cooked mandi
rice* is the loss of a produced item whose ingredients already left stock through
that batch's `PRODUCTION_OUT`; adding it to ingredient consumption would charge
the rice, spice and oil a second time (RCP-105). Output waste is **never
expanded back into raw ingredients**.

The classifier tells them apart by asking whether the item is any recipe's
`output_item` in that organization — a data question with a closed answer, never
the document's translated display text.

### 6.4 Corrections stay corrections

`COUNT_GAIN`, `COUNT_LOSS` and `MANUAL_ADJUSTMENT` get their own buckets and are
excluded from consumption (RCP-106). A count difference is *the unexplained thing
a variance report exists to surface*, not an explanation of it. Folding count
losses into consumption would make actual consumption move to meet theoretical
consumption and drive the variance towards zero — arithmetically self-fulfilling
and operationally worthless.

A `MANUAL_ADJUSTMENT` that moved value and no quantity is a **revaluation** and
gets its own bucket: it cannot be consumption because nothing left.

---

## 7. Normal yield loss is not abnormal waste

Unchanged from Task 3.6 and restated because this is the ADR a future reader
will search. The gap between expected and actual output is **normal production
loss**: it is absorbed into the produced item's unit cost (RCP-035), it raises no
document, it writes no journal, and it lives on الإنتاجية والفاقد.

الهالك is an Inventory Waste document with a reason code, a quantity, a value and
a journal. The two are never added. A report that added them would let a kitchen
hide spoilage inside a yield figure, which is the one thing the separation exists
to prevent.

---

## 8. MealRecord explained usage

Staff and complimentary meals are expanded to leaf ingredients through the
**shared expansion engine**, at the recipe version **stored on each record** —
never a re-resolved one. Cancelled meals contribute nothing: no row rather than
a zero row, because the correction said the meal never happened.

The results are labelled `STAFF_MEAL_EQUIVALENT` and
`COMPLIMENTARY_MEAL_EQUIVALENT` and are presented as **separate explanatory
source buckets**.

### 8.1 Production plans and meal equivalents are not added

**Decision.** No combined "theoretical" total is offered anywhere in Task 3.8.

**Why.** A `ProductionBatch`'s planned lines and a `MealRecord`'s expansion
**overlap physically**. The batch already contains the ingredients that produced
the output; the meal record explains where some of those produced portions went.
Expanding both to raw materials and adding them counts the same rice twice.

A combined figure would need a deduplication key linking each meal portion to
the batch that produced it, and no such key exists — a meal is recorded against
a recipe and a date, not against a batch. Until one exists, separate buckets are
the honest presentation.

The same reasoning governs the variance diagnostic's residual: meal equivalents
sit **beside** it and are not subtracted from it. A staff meal does not consume
store stock, and subtracting it would remove a quantity already counted once,
driving the residual negative for any kitchen that feeds its staff.

Meal accounting reclassification remains deferred and recorded
(`MEAL_ACCOUNTING_RECLASSIFICATION_DEFERRED`, RCP-044).

---

## 9. Sales-based theoretical consumption: interface ready, data deferred

**Decision.** A stable quantity-source interface ships now
(`TheoreticalConsumptionSource`, `TheoreticalConsumptionContribution`). Two
adapters are registered: `STAFF_MEAL` and `COMPLIMENTARY_MEAL`. The `SALES`
adapter is **absent** — not stubbed, not returning an empty list, not present
behind a flag.

`SALES` is nevertheless a declared member of `TheoreticalSourceType`, and that
distinction is the mechanism: `theoretical_consumption_coverage` iterates the
**enum**, not the registry, so a declared type with no adapter is reported as
`DEFERRED_TO_PHASE_4` with a zero count. The report says *sales are missing*
rather than quietly summing the two sources it happens to have.

Every theoretical and variance response carries, without exception and
regardless of filters:

```
coverage_code = SALES_NOT_INCLUDED_PHASE_4
```

and the Arabic notice:

> الاستهلاك النظري المعتمد على المبيعات غير مكتمل حالياً؛ سيتم ربط كميات
> المبيعات المعتمدة في المرحلة الرابعة.

`is_final` is a **constant `False`**, not a computed flag. A derived boolean
would eventually return `True` for a period with no sales in it, and an empty
period is exactly where a false claim of finality does the most damage.

No Sales models are created. No fake `SALES` contribution exists. No sold
quantity is fabricated. There is no backflush.

---

## 10. Two variance outputs, labelled apart

### 10.1 Production standard variance — AVAILABLE and COMPLETE

```
production variance = actual posted production quantity − planned production quantity
```

Both sides are posted facts about the **same batch**: what the frozen recipe
version required, and what the kitchen actually put in. No sold quantity is
involved and none is implied. Nothing about this figure is provisional.

Where a substitution crossed dimensions, the cell returns
`NOT_QUANTITATIVELY_COMPARABLE` rather than zero. Zero means "no deviation";
this means "the question has no numeric answer". A physical variance is never
calculated across incompatible dimensions.

### 10.2 Final sales-based usage variance — NOT AVAILABLE

```
final usage variance = actual consumption − theoretical sales consumption
```

Approved sold quantities do not exist in Phase 3, so this number cannot be
computed — and the decision is that it is **not approximated**.

**Why.** A variance report is read by people making staffing and purchasing
decisions. A number that silently omits sales is not a rough version of the real
number; it is a different number with the same name, and it will be wrong in the
direction that looks like theft.

What the screen shows instead is a **partial diagnostic**: actual economic
consumption, production standard requirements, staff-meal equivalents,
complimentary-meal equivalents, custody flows, waste, count corrections, and a
residual named for what it actually is —
`unexplained_by_production_plan` — carrying on every row and in every export:

```
PARTIAL_COVERAGE
NOT_FINAL_USAGE_VARIANCE
```

There is no field, control, endpoint or export anywhere that produces a
definitive final consumption variance.

---

## 11. Value visibility

Quantity reports require `view_kitchen_report`. Money additionally requires
`view_recipe_cost`, resolved through `cost_readable_organization_ids` — the
organization-aware helper — and never through a bare global `has_perm`.

Redaction is **structural**: without cost permission the `columns` list has no
money entries at all, so the cells are absent rather than blank or null. A null
tells the reader a number exists and that they are not trusted with it, which is
a different statement from the one intended.

---

## 12. What Task 3.8 created

```
0 StockMovement            0 StockLedgerEntry
0 StockBalance mutation    0 StockLocationBalance mutation
0 JournalEntry             0 JournalLine
0 production reposting     0 Sales record
0 backflush movement
```

The only new business records are `BatchDocumentLink` rows: kitchen-owned,
explanatory, and provably without ledger effect. The Task 3.8 smoke measures the
stock and journal census before and after every read and every link command and
asserts it unchanged, rather than asserting the claim in prose.

There is **no repair mode**. Verification reports and refuses to repair
(RCP-050).

---

## 13. Rejected alternatives

| Rejected | Why |
|---|---|
| Adding custody transfers to consumption | Counts the same kilogram twice: once when it changed hands, once when it was cooked. Produces a permanent structural overage nobody can explain |
| Subtracting an unrelated custody return from `PRODUCTION_OUT` | The mirror of the same double count, and it would restate a batch whose input value is already locked to its output value |
| Editing a posted batch to correct its inputs | Breaks `input_value = output_value` or requires restating a journal already in the general ledger. Reversal and repost keeps both ledgers append-only |
| Task 3.0 §11.2's `consumed − linked returns + linked waste` | Both adjustments credit or charge quantities that already moved through their own documents' ledger effects |
| `MATERIAL_RETURN` / `LINKED_WASTE` link names | They imply the link reverses `PRODUCTION_OUT`. The first reader to act on that implication builds a double-counting report |
| `document_type` + UUID as the link's source | A caller-controlled table name the database cannot check. A link to a deleted or foreign row would look valid |
| `GenericForeignKey` | Same objection, plus it defeats `PROTECT` |
| Adding `ProductionBatch` plans and `MealRecord` expansions blindly | They overlap physically. Without a portion-to-batch deduplication key, the sum counts the same rice twice |
| Subtracting meal equivalents from the consumption residual | A staff meal does not consume store stock; its ingredients already left through the batch. Subtracting drives the residual negative for any kitchen that feeds staff |
| Calling the Phase 3 partial figure a final usage variance | It omits sales, so it is a different number with the same name — wrong in the direction that looks like theft |
| A computed `is_final` flag | Would return `True` for an empty period, which is where a false claim of finality does the most damage |
| A stubbed `SALES` adapter returning nothing | Indistinguishable from sales having contributed nothing. Absence plus an enum member is the only honest shape |
| Repricing historical movements | Lets a purchase made later restate what a batch cost earlier |
| Creating fake Sales records or a backflush | Fabricates the one input the whole final calculation depends on |
| An `else: OTHER` fallback in the classifier | Silently absorbs a new `MovementType` into a bucket nobody chose, and breaks the stock identity that is the partition's only proof |
| Folding count losses into consumption | Drives the variance towards zero: arithmetically self-fulfilling, operationally worthless |
| Expanding produced-output waste back into raw ingredients | Charges the ingredients a second time; they already left through the batch |

---

## 14. Consequences

- Consumption has one authoritative classifier and one proof, and both are
  exercised by `verify_kitchen`.
- A future `MovementType` **must** be classified explicitly. The classifier
  raises otherwise, which is intended: a loud failure at the point of change
  beats a quiet one in a report six months later.
- Phase 4 adds sales by writing **one adapter**. No arithmetic in
  `consumption.py`, `consumption_sources.py` or `consumption_reconciliation.py`
  changes, and the coverage machinery starts reporting `SALES` as `AVAILABLE`
  the moment the adapter is registered.
- Until then, every theoretical and variance surface — HTML, API and CSV —
  carries its coverage code, and no surface offers a final figure.
- Task 3.11 must certify the full matrix listed in
  `docs/runbooks/phase-3-deferred-verification.md`.
