# Task 1.0 — Inventory domain specification

- **Status:** **Accepted with amendments (2026-08-09).** Specification only —
  Task 1.0 created no models, migrations, services, API, or UI. The amendments
  applied at approval are marked *(amended)* where they changed a
  recommendation.
- **Date:** 2026-08-09
- **Branch:** `phase/1-inventory`, from `phase-0-complete` (`7073f95`)
- **Related:** ADR-006, ADR-007, ADR-008, ADR-012 – ADR-018,
  `docs/invariants/inventory-invariants.md`,
  `docs/tasks/phase-1-task-breakdown.md`

Everything operational in this system eventually writes to inventory. The
ledger, the unit conversions, and the costing behaviour have to be dependable
before purchasing, recipes, or sales consume them — so this document fixes the
contract first, and the implementation tasks follow it.

## 0. Source material, and two gaps in it

Read for this specification:

| Source | Where | Used for |
|---|---|---|
| Revised architecture plan | `System/files/Khan_Mandi_..._Revised_Architecture_and_Claude_Code_Plan.txt` | Inventory rules §3, §4, §5, §9; Phase 1 scope |
| Installation-to-coding plan | `docs/plans/installation-to-coding-start-plan.txt` | Environment, phase order |
| ADR-006, 012 | `docs/decisions/` | Quantity and money precision |
| ADR-007, 016 | `docs/decisions/` | Organization/branch boundaries, scope model |
| ADR-008 | `docs/decisions/` | Business date vs calendar date |
| ADR-013 – 015 | `docs/decisions/` | Periods, chart of accounts, cost centres |
| ADR-017 | `docs/decisions/` | Source identity and idempotency |
| Kernel invariants | `docs/specs/accounting-kernel-invariants.md` | What inventory must not break |
| Phase 0 code | `apps/organizations`, `apps/units`, `apps/accounting`, `apps/core` | Actual implementation, not summaries |

**Two things the task brief assumed exist, and do not.**

1. **There is no SRS. SRS reconciliation is DEFERRED.**
   `docs/requirements/SRS.md` is referenced by `CLAUDE.md` and has never been
   added. Searched again at the start of Task 1.1 across the whole Desktop
   tree: the only documents present are the architecture plan, the
   installation plan, the environment bootstrap note, and three operational
   PDFs (a purchase request, a July claim statement, a contract). None is a
   requirements specification.

   **The correct statement of what has been checked is therefore:**

   > No contradiction was found against the architecture plan, the approved
   > ADRs, and the current implementation. Reconciliation against the
   > authoritative SRS has not been completed because the SRS is absent from
   > the repository.

   Any stronger claim — "nothing contradicts the SRS" — is unsupportable and
   must not appear in this repository. Every requirement below traces to the
   architecture plan or to an ADR, never to a business requirements document,
   and the `INV-*` and `AT-*` identifiers remain **repository-local** until an
   authoritative SRS is supplied and mapped.

   When the SRS arrives: place the original under
   `docs/requirements/source/`, preserve it byte-for-byte, record its
   filename, version or date, and SHA-256, and re-derive traceability from it.
   Do not rewrite its content.

   Task 1.1 proceeds under the approved architecture baseline. That is a
   deliberate, recorded limitation, not an oversight.
2. **The `AT-xxx` acceptance identifiers do not exist anywhere** in the
   repository or in the source documents. They are *established* by this task
   from the descriptions supplied with it, not referenced. See §14.

---

## 1. Source-document identifier durability (§C)

Inspected directly, not inferred.

| Property | Value |
|---|---|
| Django field | `models.CharField(max_length=64, blank=True)` |
| PostgreSQL column | `character varying(64)`, `NOT NULL` |
| Empty representation | `''` — never `NULL`, so the all-or-none check constraint is a string comparison |
| Companion fields | `source_document_type varchar(100)`, `source_event varchar(16)` |
| Unique index | `(organization_id, source_document_type, source_document_id, source_event) WHERE source_event <> ''` |

**It is already type-agnostic. Evidence:**

| Identifier kind | Example | Fits? |
|---|---|---|
| Integer primary key | `"145"` | Yes |
| UUID (canonical, hyphenated) | `"3f2504e0-4f89-11d3-9a0c-0305e82c3301"` — 36 chars | Yes |
| External/imported reference | `"SUP-2026-000431/A"` | Yes, to 64 chars |
| Composite natural key | `"BUNOOK/GRN/2026/00017"` | Yes |

**Leading zeros are preserved** because the column is text and nothing casts
it. `"0145"` and `"145"` are distinct identities — correct behaviour for a
document number, and the same reasoning ADR-014 applies to account codes.

**No change is recommended, and none is made.** Storing integers would have
been the wrong model: it is the case that could not later accommodate a UUID
or an imported reference.

### The one real gap: no canonical normalisation

`validate_source_identity` calls `.strip()` only to decide whether a field is
*present*. The value stored is the value supplied. So:

```
"145"    "145 "    " 145"    "0145"
```

are four different source identities, and the uniqueness guarantee treats them
as four different economic events. A module that trims inconsistently between
its normal path and its retry path would double-post, and the constraint that
exists to prevent exactly that would not fire.

**Approved (Task 1.2, before any inventory module emits a source identity).**
Normalise **centrally in the accounting service**, not in each caller — a rule
applied by convention in six modules is a rule that six modules can forget:

```python
source_document_type = source_document_type.strip().upper()
source_document_id = source_document_id.strip()
```

A value that becomes empty after stripping is rejected, not treated as absent.

`source_document_id` is **not** case-folded. External systems issue
case-sensitive references, and folding `abc-1` into `ABC-1` would merge two
genuinely different documents — the opposite failure, and a worse one.

Regression tests must prove that `"145 "` and `"145"` cannot become two source
identities after normalisation.

**Prefer the immutable internal document UUID or primary key as
`source_document_id`.** The human-readable document number is kept separately
for display and drill-down. A human number can be renumbered, re-sequenced
after a correction, or reused across years; an internal identifier cannot, and
the source identity must outlive any presentation decision.

Compatibility impact: none for existing rows. Phase 0 wrote no source
identities outside tests, verified by
`JournalEntry.objects.exclude(source_event="")` being empty in any deployed
database. Adding normalisation later, *after* Phase 1 modules are live, would
require a data migration and a uniqueness re-check — which is why it belongs
in Task 1.2 and not after.

---

## 2. Master-data ownership (§H)

Recommended ownership, checked against the architecture plan §9 and against
`ADR-007`. **No conflict found.**

| Object | Owner | Reason |
|---|---|---|
| `ItemCategory` | Organization | Reporting rollups must be comparable across branches |
| `InventoryItem` | Organization | The architecture plan lists a shared item master; two branches buying the same rice must cost it under one item |
| Item code | Organization (unique per organization) | Matches `Account`, `CostCenter`, `Branch` |
| `ItemPackageConversion` | Organization, per item | A sack of rice is 30 kg everywhere in the organization |
| Costing policy | Organization, overridable per item | One default, deliberate exceptions |
| Reason codes | Organization | Waste and adjustment analysis is organization-wide |
| Inventory account mapping | Organization | The chart is organization-owned (ADR-014) |
| `Warehouse` | **Branch** | Physical custody is a branch fact; the architecture plan puts warehouses under branch |
| Branch item activation | Branch | A branch that does not stock an item should not see it in pickers |
| Reorder settings | Branch | Reorder levels differ by branch throughput |
| `StockLocation` (bin) | **Warehouse** | A bin exists inside one physical store |
| Count/freeze state | Warehouse | A count freezes a physical store, not a company |

**Cost centres own nothing here.** They are a managerial dimension on journal
lines (ADR-015), not a custody structure. Inventory journal lines follow the
existing branch/cost-centre policy unchanged: the branch comes from the
warehouse's branch, and the cost centre is required exactly where
`Account.requires_cost_center` says so.

---

## 3. Item master (§I)

### Proposed model — `InventoryItem`

| Field | Type | Notes |
|---|---|---|
| `id` | BigAuto | Immutable internal identifier; never shown to users, never reused |
| `organization` | FK PROTECT | Owner |
| `code` | Char(32) | Unique per organization; see below |
| `name_ar` | Char(200) | Source language |
| `name_en` | Char(200) | Translation target |
| `category` | FK PROTECT | Leaf category only |
| `item_type` | Char(16) choices | Closed enum, below |
| `base_unit` | FK PROTECT → `UnitOfMeasure` | **Immutable once movements exist** |
| `is_active` | Bool | Archive, never delete |
| `tracks_lots` | Bool default False | Opt-in |
| `tracks_expiry` | Bool default False | Requires `tracks_lots` |
| `shelf_life_days` | PositiveSmallInt null | Only meaningful with `tracks_expiry` |
| `costing_method` | Char(24) choices | `MOVING_WEIGHTED_AVERAGE` only in Release 1 |
| `notes` | Text blank | |
| `created_at` / `updated_at` | From `TimeStampedModel` | |
| `history` | `HistoricalRecords()` | Mutable master data, per ADR-007 practice |

### Three fields deliberately removed at approval

| Removed | Why |
|---|---|
| `inventory_account` | Account resolution belongs **exclusively** to `AccountRole` / `AccountMapping` (§11). A foreign key here would be a second, competing resolution path, and the first time the two disagreed nobody would know which one posted |
| `allows_negative_stock` | A permanent field that automatically permits negative stock is not an exception — it is a silent, standing bypass with no actor and no reason. An override is a **per-posting** decision (§10) |
| `is_variable_weight` | **Derived**, not stored: an item is variable-weight when it has an active `VARIABLE` package conversion. Storing it duplicates a fact that already exists and invites the two to disagree. If a query or screen later proves it is needed for performance, it may be added *with* a consistency guarantee — not before |

Deliberately **absent** from Release 1: barcode/GTIN, supplier preferences,
purchase price lists, tax codes, images, min/max order quantities. Each
belongs to a later phase and none is needed to make the ledger trustworthy.

### Immutable once posted movements exist

`organization`, `base_unit`, `tracks_lots`, `costing_method`, and any change
to `tracks_expiry` that alters the valuation identity are frozen the moment
the item has posted movement history. Each of them changes what a stored
quantity or a stored value *means*, so changing one silently restates history
that is already reported and reconciled.

### Decisions and justification

**1. Item-code format.** `^[A-Z0-9][A-Z0-9._-]*$`, max 32 characters, stored
as text. Consistent with `Organization`, `Branch`, and `CostCenter`
(`^[A-Z0-9][A-Z0-9_-]*$`) with `.` added because supplier catalogues use it.
Not the `C-GG-SS-AAA` account shape: an item code carries no hierarchy, and
imposing one would force a re-code every time an item is re-categorised.

**Canonicalised before validation and storage:**

```python
code = code.strip().upper()
```

Only the canonical form is ever stored, which makes uniqueness
case-insensitive **economically** without needing a functional index or a
case-insensitive collation: `rice-272`, `Rice-272`, and `RICE-272 ` are one
code, because they are one thing on one shelf. A whitespace-only code, or one
that canonicalises to empty, is refused — the pattern requires a leading
alphanumeric, so padding cannot smuggle a duplicate past the constraint.

**2. Generated, manual, or both.** **Both.** The UI offers
`<CATEGORY_CODE>-NNNN` from a per-category sequence, and the operator may
overwrite it. Restaurants migrate from spreadsheets with existing codes
printed on shelf labels; refusing them would guarantee a parallel mapping
sheet, which is worse than an inconsistent code.

**3. Uniqueness scope.** Per organization —
`UniqueConstraint(organization, code)`. Identical to `Account` and
`CostCenter`.

**4. Archived codes stay reserved permanently.** Same rule as accounts
(ADR-014). A reissued code makes every historic movement, count sheet, and
printed label ambiguous.

**5. Category hierarchy — approved.** Organization-owned, parent/child,
**maximum depth 3**, items on **leaves only**. Restaurant reporting genuinely
needs `Food > Meat > Beef`. The leaf-only rule is the same invariant ADR-014
enforces for accounts and exists for the same reason: an item hanging on a
parent stops its children summing to it, and no category report can be trusted
afterwards.

Six guards, each a test:

| Guard | Error code |
|---|---|
| Category code unique per organization | constraint |
| No cycles — a category cannot be its own ancestor | `category_cycle` |
| Depth never exceeds 3 | `category_too_deep` |
| Items may reference leaf categories only | `category_not_leaf` |
| **A category holding items cannot acquire children** | `category_has_items` |
| **A category with children cannot receive items** | `category_has_children` |

The last two are the pair that makes the hierarchy trustworthy rather than
decorative, and they mirror the hierarchy-exclusivity rules already enforced
on the chart of accounts. **Re-parenting is checked for both depth and
cycles** — a move is the operation that creates an illegal tree, not the
initial insert.

Archived categories stay readable, and a category referenced by any item
cannot be deleted (`on_delete=PROTECT`).

**6. `ItemType` — closed, six values.** *(Amended: `FINISHED_GOOD` is
required.)*

| Value | Meaning | Why it exists now |
|---|---|---|
| `RAW_MATERIAL` | Rice, meat, oil | The bulk of stock |
| `SEMI_FINISHED` | Prepared sauce, marinated meat | Produced then consumed; Phase 3 needs the vocabulary and re-typing an item with movement history is a re-classification problem |
| `FINISHED_GOOD` | A produced output that is **physically stored and countable** — trays of baked bread, bottled sauce made in-house, packed catering portions held for tomorrow | Real stock that a count sheet must reach |
| `GOODS_FOR_RESALE` | Bottled water, canned drinks | Bought and sold untransformed — genuinely not a raw material |
| `PACKAGING` | Containers, cups, bags | Consumed per sale, not part of the recipe yield |
| `CONSUMABLE` | Cleaning supplies, gloves | Stocked and issued, never costed into a dish |

**`FINISHED_GOOD` does not mean "menu item".** The two are separate domains
and neither implies the other:

| | Lives in | Counted on a shelf? |
|---|---|---|
| `InventoryItem` (`FINISHED_GOOD`) | Inventory | **Yes** — it is physical stock |
| Menu item | Sales / Recipes (Phase 3–4) | No — produced to order |

A plated Chicken Mandi is a menu item and is **not** automatically an
`InventoryItem`. It is assembled on demand and never stored. Conversely a tray
of bread baked this morning and held for tomorrow *is* a `FINISHED_GOOD`
inventory item and must be countable and valued.

The link between the two — a menu item consuming inventory through a recipe —
is a Phase 3 relationship between two separate models, never a shared row.

**7. Can `base_unit` change once posted movements exist?** **No — never**,
enforced in the service and by a guard identical in spirit to
`configure_accounting`'s fiscal-year lock. Every posted quantity is stored in
base units; changing the base unit silently restates the entire history of
that item, and no report produced before the change would reconcile with one
produced after.

**8. Correcting an incorrect base UoM.** Not by editing. The path is:

```
1. Archive the item (is_active = False; the code stays reserved).
2. Create a replacement item with the correct base unit and a new code.
3. Post a MANUAL_ADJUSTMENT issuing the full balance out of the old item
   at its current moving-average value.
4. Post a MANUAL_ADJUSTMENT receiving the same value into the new item at
   the converted quantity.
5. Both movements share one source identity and one reason.
```

The pair is value-neutral, fully audited, and leaves the old item's history
intact and readable. This is the inventory analogue of correction-by-reversal.

**9. Shared master with branch activation.** Yes. The item master is
organization-owned; a `BranchItemSetting` row (branch, item, `is_stocked`,
reorder point, reorder quantity) makes an item visible and orderable at a
branch. Absence of a row means "not stocked here" — it does **not** block a
posting that legitimately arrives (a transfer into a branch that has not yet
configured the item must not fail); it controls pickers and reorder reports.

---

## 4. Item-specific conversions (§J)

The generic `UnitOfMeasure` layer already converts within a dimension —
kilogram ↔ gram, litre ↔ millilitre — at 12 decimal places, globally, with a
partial unique index ensuring one base per dimension. That is necessary and
insufficient. A *carton* is not a dimension member; it is a package whose
content differs per item.

### A package unit is not a unit of measure

**Amended at approval, and this is the central correction.** `CARTON`,
`SACK`, `CONTAINER`, and `CAN` must **never** be modelled as
`UnitOfMeasure` rows with a `factor_to_base`. A `UnitOfMeasure` carries a
*global, dimensional* factor — 1 kg is 1000 g everywhere, forever. A carton of
chicken and a carton of oil have nothing in common but the word.

Putting a package into `UnitOfMeasure` would force one universal factor for
"carton", and every item whose carton differs would then be silently wrong.

So there are two separate concepts:

| | `UnitOfMeasure` (exists, Phase 0) | `PackageUnit` (new) |
|---|---|---|
| Examples | KG, G, L, ML, PIECE, DOZEN | CARTON, SACK, CONTAINER, CAN, TRAY |
| Has a global factor | **Yes** — `factor_to_base` within a dimension | **No — none, ever** |
| Belongs to a dimension | Yes (MASS / VOLUME / COUNT) | No |
| Scope | Global reference data | Organization |
| Converts | Within its dimension | Only through `ItemPackageConversion` |

`PackageUnit` deliberately has **no factor field at all**. The absence is the
guarantee: there is nowhere to write a universal conversion, so nobody can.

### `PackageUnit`

| Field | Type | Notes |
|---|---|---|
| `organization` | FK PROTECT | |
| `code` | Char(20) | Canonical uppercase, unique per organization |
| `name_ar` / `name_en` | Char(100) | |
| `is_active` | Bool | |
| `history` | `HistoricalRecords()` | |

### `ItemPackageConversion`

| Field | Type | Notes |
|---|---|---|
| `organization` | FK PROTECT | Denormalised from item for scoping |
| `item` | FK PROTECT | Owner |
| `package_unit` | FK PROTECT → `PackageUnit` | |
| `conversion_type` | Char(8) choices | `FIXED` or `VARIABLE` |
| `factor_to_base` | Decimal(24, **12**) | Exact for `FIXED`; a **planning estimate only** for `VARIABLE` |
| `effective_from` | Date | |
| `effective_to` | Date null | Null = open-ended |
| `allows_fractional` | Bool default True | Half a sack yes; half a can no |
| `minimum_increment` | Decimal(18,3) null | Smallest orderable/issuable step |
| `is_default_purchase_package` | Bool default False | At most one active per item, partial unique index |
| `version` | PositiveInt | Increments per (item, package_unit); snapshotted on every posting |
| `is_active` | Bool | |
| `history` | `HistoricalRecords()` | |

### Every conversion resolves DIRECTLY to the item's base unit

**No chains.** If tomato paste has base unit `KG`:

```
1 CAN     ->  0.800000000000 KG      direct
1 CARTON  -> 24.000000000000 KG      direct
```

and never:

```
CARTON -> CAN -> KG        ← forbidden
```

A chain has to be resolved at posting time, and every link is a place where a
version, an effective date, or a rounding step can disagree with the others.
Two direct factors are trivially auditable; a chain is a small graph traversal
inside a posting service. If a carton's content changes from 30 cans to 24,
the carton's direct factor is versioned — the can's factor is untouched,
because a can did not change.

Entry in the item's own base unit needs no conversion row at all.

### FIXED versus VARIABLE

**`FIXED`** — the factor is exact and converts entered quantity to base
quantity arithmetically.

```
Rice          (base KG):     1 SACK    = 30.000000000000 KG
Chicken       (base PIECE):  1 CARTON  = 10.000000000000 PIECE
Oil           (base L):      1 CARTON  = 20.000000000000 L
Tomato paste  (base KG):     1 CAN     =  0.800000000000 KG
                             1 CARTON  = 24.000000000000 KG   <- direct, not via CAN
```

Entered 3 SACK → base `3 × 30 = 90.000 KG`. Deterministic, and the factor is
snapshotted onto the movement.

**`VARIABLE`** — the package is what was ordered and counted, but the base
quantity is *measured*, not derived. Meat is the canonical case.

```
Entered:              1 CONTAINER
Measured base:        17.650 KG        <- entered by the receiver
Implied factor:       17.650000000000  <- derived and recorded, never reused
```

For a `VARIABLE` conversion the stored `factor_to_base` is a **planning
estimate only** — used for ordering suggestions and for variance reporting —
and posting **requires** an explicit measured base quantity. A posting that
supplies only the package count for a `VARIABLE` conversion is refused with
`measured_quantity_required`. This is the rule that stops 1 container silently
becoming exactly 18.000 kg forever.

There is **no `is_variable_weight` field on the item.** An item is
variable-weight when it has an active `VARIABLE` conversion, derived on
demand. The same item legitimately has both a fixed retail pack and a variable
bulk container, so a single item-level flag could not have been true anyway.

### Effective dating, versioning, and overlap

- Rows are effective-dated on `(item, package_unit)`. **Overlapping active
  periods for one `(item, package_unit)` are refused** by an exclusion
  constraint — PostgreSQL `EXCLUDE USING gist` on
  `(item WITH =, package_unit WITH =, daterange(effective_from, effective_to) WITH &&)`.
  Two answers to "how many kilograms in a sack today" is not a data problem to
  resolve at query time; it is a data problem to prevent.
- Changing a factor **never updates a row in place once it has posting
  history**. It closes the current row (`effective_to`) and opens a new one
  with `version + 1`. A supplier that changes a 30 kg sack to 25 kg is a new
  packaging fact, not a correction of the old one, and every movement posted
  under the old packaging must keep meaning what it meant.
- If a row has **no** posting history it may be edited in place, which keeps
  ordinary data-entry mistakes from generating spurious versions.

### Snapshot requirements on posting

Every posted movement retains, immutably:

```
entered_quantity        Decimal(18,3)   what the human typed
entered_unit            FK             what they typed it in
conversion              FK null        the row used (null for base-unit entry)
conversion_version      PositiveInt    which version of it
conversion_factor       Decimal(24,12)  the factor actually applied
base_quantity           Decimal(18,3)   the authoritative signed quantity
```

Storing the factor as well as the conversion FK is deliberate redundancy: the
FK says which rule was used, the factor says what it was, and a later
correction to a mis-keyed historical row cannot retroactively change what a
posted movement meant.

### Reciprocal factors: derived, never stored

Only `factor_to_base` is stored. The inverse is computed at
`CALCULATION_PLACES` (6) when needed for display. Storing both invites them to
disagree, and `1/3` has no exact decimal representation, so a stored
reciprocal is a rounding error waiting to be multiplied back.

### Validation against the base unit

A `PackageUnit` has no dimension, so nothing to validate against — that is
precisely why the factor must be item-specific. What **is** validated: the
factor is strictly positive, and it resolves to the item's own `base_unit`
and no other.

Entry in a plain `UnitOfMeasure` other than the base unit still goes through
`apps/units/services.py`, which already refuses a cross-dimension conversion
(`_require_same_dimension`) — litres of meat remains a data error, and that
check is reused rather than reimplemented.

### Worked edge cases

| Case | Behaviour |
|---|---|
| Entry in the base unit itself | No conversion row needed; `conversion` null, factor `1.000000000000` |
| Item has no conversion for the entered package | Refused, `no_conversion_for_package` — never silently assume 1:1 |
| A package factor is requested via another package | Impossible: chains are not modelled |
| `allows_fractional = False`, entered `2.5 CAN` | Refused, `fractional_not_allowed` |
| Conversion expired at the effective date | Refused, `no_effective_conversion` — with the date named |
| Backdated posting into a period with an older factor | The factor effective **on the movement's effective date** is used, not today's |
| `VARIABLE` package, measured 0 | Refused: a receipt must move a non-zero quantity |
| Factor changes after a draft was created but before posting | Re-resolved at posting; the draft holds no factor |

---

## 5. Warehouses and locations (§K)

### Decisions

**1. Warehouse versus `StockLocation`.** Both, with a sharp division:

| | `Warehouse` | `StockLocation` (bin) |
|---|---|---|
| Owner | Branch | Warehouse |
| Release 1 | Required | **Optional, deferred to Task 1.7** |
| Owns quantity | Yes | Yes, when enabled |
| **Owns moving-average value** | **Yes** | **No — never** |
| User-creatable | Yes | Yes |

**2 & 3. Which dimension owns quantity.** Warehouse, always. Location refines
it when the warehouse enables locations, and the sum of a warehouse's location
quantities must equal its warehouse quantity — a reconciliation test, not a
convention.

**4. Which dimension owns moving-average value.** **The warehouse.** This is
the most consequential decision in this section. If value were held per bin,
moving a box from bin A to bin B inside one store would revalue stock, and a
warehouse-level cost would have to be recomputed by weighted aggregation on
every read. A bin is a *findability* concept; it carries no economics.

**5. Warehouse type — a closed enum** *(amended)*.

| Type | Meaning | User-creatable |
|---|---|---|
| `PHYSICAL` | Main Store, Kitchen Store, any real place | Yes |
| `PRODUCTION_WIP` | Work in progress during production | Yes |
| `IN_TRANSIT` | Stock dispatched and not yet received | **No — system-controlled** |

`IN_TRANSIT` is created automatically, one per branch. A system warehouse
**cannot be renamed, archived, or converted into a normal warehouse by a
normal user**, and once Task 1.5 exists it accepts only approved transfer
commands. Warehouse codes are canonical uppercase and unique **per branch**.

**6. May users create arbitrary system warehouse types?** No. `warehouse_type`
is closed and `is_system` is set only by migration or a seeding command. A
user-settable "system" flag is a way to make an ordinary warehouse exempt from
the rules that protect the ledger.

**7. May one branch reach another branch's warehouse?** Not through generic
scope. A branch-to-branch transfer requires the acting user to hold
`inventory.post_transfer` at **both** branches — exactly the
`_require_at_every_branch` pattern already proven in
`apps/accounting/commands.py`, reused rather than reinvented. Anything else
would require weakening branch scope, which Phase 0 spent considerable effort
making airtight.

### Warehouse authorization scope *(amended)*

Warehouse scope **extends `BranchMembership`** rather than introducing an
independent role-bearing membership. A second role-bearing model would mean
two places that grant authority, and eventually two answers to "what may this
person do here".

```
BranchMembership.warehouse_scope_mode :  ALL | SELECTED
BranchMembershipWarehouse             :  (branch_membership, warehouse)
```

| Rule | |
|---|---|
| `ALL` | Every warehouse in that branch, **including ones created later** |
| `SELECTED` | Only the explicitly listed warehouses |
| Cross-branch selection | Refused — a selected warehouse must belong to the membership's own branch |
| Organization authority | Reaches organization-owned master data per its permissions; it does not silently confer warehouse custody |
| Django groups | Never substitute for organization, branch, or warehouse scope |

**Existing memberships default to `ALL`,** so the migration cannot silently
revoke anybody's access. Restriction is opt-in; that direction is the safe one
to get wrong.

**8. How branch-to-branch transfer works later.** Two movements and one
document, never a single instantaneous hop:

```
TRANSFER_DISPATCH   source warehouse        -> IN_TRANSIT (source branch)
TRANSFER_RECEIPT    IN_TRANSIT (source)     -> destination warehouse
TRANSFER_SHORTAGE   IN_TRANSIT (source)     -> written off, if less arrived
```

Stock in transit remains on the source branch's books until received, which
is both the accounting truth and the answer to "who is responsible for it
right now".

**9. Archived warehouses with movement history.** `is_active = False`, never
deleted; `on_delete=PROTECT` refuses the deletion anyway. An archived
warehouse must have zero balance across every item — refused otherwise with
`warehouse_not_empty`, because archiving a warehouse holding stock would make
that stock unreachable while still counting in organization totals.

**10. Freeze.** A `Warehouse.freeze_state` of `FROZEN` refuses every posting
whose source or destination is that warehouse, except the count adjustment
that the freeze exists to serve. See §10.

### Initial operational structure

Named here as *types*, not seeded as data (§K forbids seeding real names):
Main Store, Kitchen Store, Production/WIP, In-Transit (system). A Packaging
Store is created only where packaging is physically controlled separately —
otherwise it is an item category inside Main Store, and inventing a warehouse
for it produces transfers nobody performs.

---

## 6. Lots, batches, and expiry (§L)

**Opt-in per item.** `tracks_lots` defaults to False, and
`tracks_expiry` requires `tracks_lots` — an expiry date with no lot to attach
it to has nothing to expire.

| Question | Decision |
|---|---|
| When is a lot mandatory? | On every inbound movement for an item with `tracks_lots`; refused otherwise with `lot_required` |
| When is expiry mandatory? | On every inbound movement for an item with `tracks_expiry` |
| Lot-code uniqueness | Per `(organization, item)`. The same code from two suppliers for two items is not a collision |
| Supplier lot vs internal lot | Both stored: `supplier_lot_code` free text, and an internal `code` generated when the supplier gives none. The internal code is what the ledger references |
| Production lot compatibility | The `Lot` model carries a nullable `produced_by_source` identity so Phase 3 can point a production batch at it without a schema change |
| **Is moving-average held per lot?** | **Yes, when `tracks_lots` is enabled.** Lots have genuinely different purchase costs; a blended warehouse cost combined with lot-specific issue selection would charge out a cost the physical unit never had |
| Issue selection policy | **Release 1: the lot is chosen explicitly by the operator.** No automatic FEFO/FIFO |
| Expired-lot posting | Outbound from an expired lot is refused by default; overridable only with `inventory.post_expired_lot`, a reason, and an audit event |
| Items that do not track lots | `lot` is null on every movement and on the balance row; the valuation key degenerates cleanly |

**FEFO/FIFO picking is deliberately not implemented.** It is a *selection*
policy, and Release 1's valuation remains moving weighted average regardless.
Automatic selection without a physical scanning workflow produces picks the
store cannot follow, and a store that ignores the system's pick has a ledger
that no longer describes the shelf.

---

## 7. Business documents versus ledger effects (§M)

The separation Phase 0 established between a `JournalEntry` and the business
event behind it applies here unchanged, and for the same reason: one mutable
row cannot simultaneously mean "what the user is typing", "what happened", and
"what is on the shelf".

```
InventoryDocument           mutable while DRAFT      human document number
  └─ InventoryDocumentLine  mutable while DRAFT      what the user entered
        ↓ posting
StockMovement               IMMUTABLE                signed base qty + value
  └─ (valuation snapshot)   IMMUTABLE                cost applied, avg before/after
        ↓ projection
StockBalance                REBUILDABLE              current qty + value + avg
```

### Proposed models

| Model | Purpose | Mutability |
|---|---|---|
| `InventoryDocument` | Header: type, number, warehouse(s), effective date, status, reason, approvals | DRAFT editable; POSTED frozen |
| `InventoryDocumentLine` | Item, entered qty, entered unit, lot, measured base qty | DRAFT editable |
| `StockMovement` | One immutable signed effect on one (warehouse, item, lot) | Immutable |
| `StockBalance` | Projection: qty, value, moving-average, `last_movement_id` | Rebuildable |
| `ValuationLayer` | Per-receipt cost layer; carries FIFO forward without a rewrite | Immutable |
| `ValuationAllocation` | Which layer an outbound consumed, and how much | Immutable |
| `OpeningStockDocument` / `...Line` | A distinct document type, not a generic receipt | DRAFT editable; POSTED frozen |

**`ValuationLayer` and `ValuationAllocation` exist in Release 1 even though
moving weighted average does not strictly need them.** They are the strategy
boundary §G.5 requires: with layers recorded from day one, introducing FIFO
later is a new consumption strategy over existing data. Without them it is a
migration of history that cannot be reconstructed, because the information was
never captured.

A document number is human-facing and gapless per `(organization, document
type, year)`, taken under `select_for_update` exactly as
`JournalNumberSequence` already does. A draft holds no number — the same rule
Task 0.7 established for journal entries, for the same reason.

---

## 8. Movement taxonomy (§N)

Twelve types, closed. Signs are from the perspective of the warehouse named on
the movement row; a transfer is two rows, not one row with two warehouses.

| Type | Qty | Value | Source | Dest | Valuation | Accounting | Approval | Neg-stock check |
|---|---|---|---|---|---|---|---|---|
| `OPENING` | + | + | — | WH | Sets initial avg | Dr Inventory / Cr Opening equity | Yes | No (must be first) |
| `RECEIPT` | + | + | — | WH | Recomputes avg | Dr Inventory / Cr GRNI or clearing | Configurable | No |
| `ISSUE` | − | − | WH | — | At current avg | Dr Consumption / Cr Inventory | Configurable | **Yes** |
| `RETURN_IN` | + | + | — | WH | **At original issue cost** | Reverses the issue's accounts | Yes | No |
| `RETURN_OUT` | − | − | WH | — | At current avg | Dr Supplier/clearing / Cr Inventory | Yes | **Yes** |
| `TRANSFER_DISPATCH` | − | − | WH | IN_TRANSIT | At current avg | Dr In-transit / Cr Inventory | Configurable | **Yes** |
| `TRANSFER_RECEIPT` | + | + | IN_TRANSIT | WH | **At dispatch value** | Dr Inventory / Cr In-transit | Yes | No |
| `TRANSFER_SHORTAGE` | − | − | IN_TRANSIT | — | At dispatch value | Dr Shortage loss / Cr In-transit | **Yes** | No |
| `WASTE` | − | − | WH | — | At current avg | Dr Waste expense / Cr Inventory | **Yes** | **Yes** |
| `COUNT_ADJUSTMENT` | ± | ± | WH | WH | Gains at current avg; losses at current avg | Dr/Cr Count variance | **Yes** | No (it *is* the correction) |
| `MANUAL_ADJUSTMENT` | ± | ± | WH | WH | Gains need an explicit unit cost | Dr/Cr Adjustment account | **Yes** | Only on decrease |
| `REVERSAL` | ∓ | ∓ | mirrors | mirrors | **At the original movement's value** | Mirrors the original | Yes | **Yes, when the mirror decreases stock** |

Nothing is added speculatively. `TRANSFER_SHORTAGE` earns its place because
without it a dispatch that never fully arrives leaves value stranded in
in-transit forever, and the alternative — silently reducing the receipt — makes
the loss invisible.

**`RETURN_IN` values at the original issue cost, not the current average.**
Returning yesterday's unused mise en place at today's average would create a
gain or loss out of a movement that changed nothing economically. It requires
a link to the issuing movement.

**`REVERSAL` restores the original's value exactly**, not the current average.
A reversal must be value-neutral against its original or it is not a reversal.

***(Amended)* A reversal is not exempt from the negative-stock check.** The
`REVERSAL` row in the table above shows "No" for the availability check only
where the mirror *increases* stock. Where the mirror **decreases** current
stock, the check applies in full:

| Reversing | Mirror direction | Availability check |
|---|---|---|
| An untouched receipt | decrease | **Applies** — and passes, the goods are still there |
| A receipt whose goods were already issued | decrease | **Applies and refuses** — the stock is gone |
| An issue | increase | Not applicable |

Correcting a consumed receipt means correcting the dependent effects first, or
using an explicitly authorised exception. Exempting reversals would make
"reverse the receipt" the standard route to a negative balance, which is the
single thing the check exists to prevent.

---

## 9. Moving weighted-average algorithm (§O)

### The valuation key

```
(warehouse, item, lot)
```

`lot` is null for items that do not track lots. **Organization and branch are
derivable** — a warehouse belongs to exactly one branch, which belongs to
exactly one organization — and are stored denormalised on `StockBalance` for
tenancy filtering and index efficiency, never as part of the identity. The
architecture plan's "Organization + Branch + Warehouse + Item" describes the
same key with the derivable parts spelled out.

Stating it as the minimal key matters: it means there is exactly one balance
row per physical stock position and no possibility of two rows disagreeing
about the same shelf.

### Precision contract

| Quantity | Places | Constant |
|---|---|---|
| Stored base quantity | 3 | `QUANTITY_PLACES` |
| Intermediate quantity calculation | 6 | `CALCULATION_PLACES` |
| Conversion factor | 12 | `FACTOR_PLACES` |
| Posted IQD value | 3 | `MONEY_PLACES` |
| Unit cost / moving average | 6 | `UNIT_PRICE_PLACES` |
| Rounding | `ROUND_HALF_UP` | ties away from zero |

All six already exist in `apps/core/quantity.py` and `apps/core/money.py`.
Inventory adds no new precision policy and must not.

### The algorithm

Let a balance hold quantity `Qb` (3 dp), value `Vb` (3 dp), average `A` (6 dp).

**Receipt of `Q` at unit cost `C`:**

```
value_in = quantize_money(Q × C)          # 3 dp, ROUND_HALF_UP
Qn       = Qb + Q                          # exact, both 3 dp
Vn       = Vb + value_in                   # exact, both 3 dp
A_new    = quantize_unit_price(Vn / Qn)    # 6 dp
```

**Receipt when `Qb == 0`:** identical arithmetic. If `Vb ≠ 0` while `Qb == 0`
— which §9.5 forbids — the receipt raises `residual_value_at_zero_quantity`
rather than absorbing it silently.

**Issue of `Q`:**

```
if Q > Qb and not negative-stock override:  refuse `insufficient_stock`
Qn = Qb − Q
if Qn == 0:
    value_out = Vb                         # the depleting movement takes ALL remaining value
else:
    value_out = quantize_money(Q × A)
Vn = Vb − value_out
A_new = A if Qn > 0 else Decimal("0")      # average is undefined at zero, not stale
```

**The full-depletion rule is the residual policy.** When quantity reaches
exactly zero the outbound movement is valued at the entire remaining book
value, so `Qn == 0 ⟹ Vn == 0` holds by construction. There is no residual to
allocate, no adjusting entry, and — critically — **no mutation of any
historical movement**. The difference is absorbed into the cost of the goods
actually issued, which is where it economically belongs.

Worked example:

```
Balance:  Q = 3.000 kg,  V = 3000.001 IQD,  A = 1000.000333
Issue 3.000 kg
  Q × A  = 3000.000999 → quantized 3000.001
  Qn = 0 → value_out = Vb = 3000.001    (identical here; the rule bites when they differ)
  Vn = 0.000   A_new = 0.000000
```

Where `Q × A` and `Vb` diverge, `Vb` wins. Always.

**Guard:** if `Qn > 0` and `Vn < 0`, raise `negative_value_with_positive_quantity`.
That state is unreachable through the service and indicates a defect; failing
loudly is the only honest response.

### The eighteen cases

| # | Case | Behaviour |
|---|---|---|
| 1 | Receipt into positive stock | Recompute average as above |
| 2 | Receipt at zero quantity | Same formula; new average = C |
| 3 | Issue at current average | `Q × A`, quantized once |
| 4 | Full depletion to zero | Value out = entire `Vb`; average reset to 0 |
| 5 | Decimal residual at zero | Cannot occur — case 4 forbids it by construction |
| 6 | Return of issued stock | `RETURN_IN` at the **original issue's** unit cost, linked to it |
| 7 | Supplier return | `RETURN_OUT` at current average |
| 8 | Positive adjustment | Requires an explicit unit cost; treated as a receipt |
| 9 | Negative adjustment | At current average; negative-stock check applies |
| 10 | Waste | At current average; negative-stock check applies |
| 11 | Warehouse transfer | Dispatch at source average; receipt at **dispatch value**, so no gain/loss appears from moving goods |
| 12 | Transfer shortage | Missing quantity written off from in-transit at dispatch value |
| 13 | Backdated movement | See below — **posting order, not effective-date order** |
| 14 | Reversal of a receipt | Mirrors the original's quantity and value; recomputes the average forward |
| 15 | Reversal of an issue | Mirrors the original's quantity and value |
| 16 | Lot-enabled items | Every rule applies per `(warehouse, item, lot)` |
| 17 | Concurrent issues | Serialised by row lock — §11 |
| 18 | Rebuild from ledger | Replay movements in `posted_sequence` order; result must equal the projection exactly |

### Backdated movements — the policy that needs approval

**Recommendation: an effective date may be backdated within an OPEN period,
but valuation follows posting order, never effective-date order.**

The moving average is computed against the balance as it stands at the moment
of posting. A receipt backdated to the 3rd, posted on the 10th, affects the
average from the 10th forward. It does **not** retro-price the issues of the
5th and 7th.

The alternative — recomputing history — would rewrite the value of movements
that are already posted, already reported, and already reconciled to the
general ledger. The architecture plan says backdated transactions "must not
silently rewrite history", and immutability is a Phase 0 invariant that
Inventory does not get to relax.

Consequence, stated plainly: **quantity as-of a past date is exact; valuation
as-of a past date is the valuation that was known then.** That is the honest
behaviour and it is what an auditor expects, but it must be a conscious
decision because it surprises people who expect a spreadsheet's recalculation.
Where a genuine restatement is required, the answer is an explicit
`MANUAL_ADJUSTMENT` revaluation with its own audit trail — visible, not
implicit. **No periodic revaluation engine is built in Release 1.**

### Three timestamps, and two report modes that must be named

Every movement retains all three:

| Field | Meaning |
|---|---|
| `effective_at` | when it happened in the business |
| `posted_at` | when it entered the ledger |
| `posted_sequence` | the total order valuation was computed in |

They give two legitimate historical views, answering different questions:

| Mode | Ordered by | Answers |
|---|---|---|
| **Effective-date view** | `effective_at`, as currently known | "What did we hold on the 5th, given everything we now know?" |
| **Posted-as-of view** | `posted_sequence` up to a cutoff | "What did the books say on the 5th, as they stood then?" |

**Every report must state which cutoff semantics it uses. A report labelled
only "as of" is forbidden.** The two views diverge exactly when a backdated
movement exists — which is exactly the situation someone is investigating when
they run the report — so an unlabelled "as of" is at its least trustworthy
precisely when it matters most.

### Rebuild

`StockBalance` carries `last_movement_id` and `last_posted_sequence`. Rebuild
replays every movement for a key in `posted_sequence` order and compares. Any
divergence is a defect and fails the reconciliation test — it is never
"repaired" by overwriting the projection, because a projection that can be
quietly corrected proves nothing.

---

## 10. Negative stock, concurrency, and counts (§Q, §R)

### The race this must prevent

```
T1: read balance 10 kg      T2: read balance 10 kg
T1: check 8 ≤ 10  ok        T2: check 8 ≤ 10  ok
T1: post −8 → 2             T2: post −8 → −6      ← both "validated"
```

### Locking

- Every outbound posting takes `SELECT ... FOR UPDATE` on the affected
  `StockBalance` rows **before** the availability check, and holds the lock
  through the write. The check and the write are inside one lock, or the check
  means nothing.
- **Lock order is deterministic:** rows are locked in a single query ordered by
  `StockBalance.pk` ascending. A multi-line document touching several items
  therefore always acquires locks in the same sequence, so two concurrent
  documents cannot each hold what the other needs.
- Transaction boundary: `transaction.atomic()` around document → movements →
  balance updates → journal entry, exactly as `post_entry` already does.
  `ATOMIC_REQUESTS` stays `False`; the boundary is the service, not the request.
- Database backstop — **and an honest limit on what it can be.** A plain
  `CHECK (quantity >= 0)` on `StockBalance` cannot be used: an *authorised*
  override legitimately produces a negative balance, and an unconditional
  check would block the one exception the design exists to permit. The balance
  row also cannot express the rule on its own, because it does not know which
  posting drove it negative.

  So the enforcement is layered, and the layers are not equally strong:

  | Layer | What it guarantees |
  |---|---|
  | Service check inside the row lock | The primary gate. No unauthorised posting can go negative |
  | `StockMovement.negative_stock_override` + `PERMISSION_OVERRIDE` audit event | Every negative balance is attributable to a named actor and a reason |
  | Reconciliation test and standing exception report | **Any** negative balance traces to an authorised override — a divergence is a defect |

  The third layer is a test and a report rather than a constraint. That is a
  genuine weakening compared with the accounting kernel's triggers, and it is
  recorded here rather than glossed: the rule is conditional on facts outside
  the row being checked, so it cannot be a row constraint. Task 1.2 decides
  whether a statement-level trigger joining movement to balance is worth its
  cost.
- Retry/conflict: a lock wait is a wait, not a failure. Deadlock is prevented
  by ordering rather than retried. `SELECT ... FOR UPDATE NOWAIT` is **not**
  used — a busy warehouse would surface spurious failures to storekeepers.
- Idempotency interaction: the idempotency check happens **inside** the same
  transaction and after the lock, so a retry that arrives concurrently with the
  original blocks, then finds the completed effect and returns it.

### Override

`inventory.override_negative_stock` — organization-scoped, requires a non-empty
reason and a recorded actor, and writes a `PERMISSION_OVERRIDE` audit event
separate from the movement, exactly as the soft-closed posting override does in
`apps/accounting/commands.py`. Every override appears on a standing exception
report. Negative stock is never allowed merely because a posting would
otherwise fail.

### Concurrency test plan (PostgreSQL, real COMMIT)

1. Two threads, `transaction=True`, both issuing 8 from a balance of 10 →
   exactly one succeeds, one raises `insufficient_stock`, final balance 2.
2. Two threads issuing from two items in opposite order → both complete; no
   deadlock (proves the lock ordering).
3. Retry of an in-flight identical command → blocks, then returns the first
   result; exactly one movement exists.
4. Trigger backstop: raw `UPDATE` driving a balance negative → `IntegrityError`.
5. Rebuild after concurrent load → projection equals ledger replay.

### Stock counts — `HARD_FREEZE`

| Aspect | Decision |
|---|---|
| Scope | One warehouse; optionally filtered to a category or item list |
| Freeze | `Warehouse.freeze_state = FROZEN` refuses all other postings to it |
| Cutoff | The freeze timestamp is the cutoff; the book snapshot is taken then |
| Blind count | Counted quantity is entered without showing book quantity |
| Book snapshot | Stored per line at cutoff, immutable |
| Variance | `counted − book`, computed, never entered |
| Approval | `inventory.approve_stock_count`, distinct from conducting it |
| Adjustment | One `COUNT_ADJUSTMENT` movement per varying line, on approval |
| **Gain valuation** *(amended)* | At the current average **only if** quantity is positive and the average is positive. If quantity is zero, or the average is zero or undefined, an **explicit approved unit cost is required** — never quantity at zero value |
| **Maker-checker** *(amended)* | `approver_id != conductor_id`, enforced always — including when one person holds both permissions. Holding both is a convenience, not a licence to sign off your own count |
| Accounting | Variance to a count-variance account by the standard mapping |
| Reopening | A closed count is never edited; a new count is performed |
| Audit | Actor, cutoff, both quantities, variance, approver, reason |
| Posting during freeze | Refused with `warehouse_frozen`, naming the count |

---

## 11. Opening stock and accounting integration (§P)

### The atomic source event

One `transaction.atomic()` produces: the opening document → `OPENING`
movements → `StockBalance` rows → `ValuationLayer` rows → one `JournalEntry`.
Any failure rolls back all of it.

```
Dr   Inventory control account       (resolved by mapping, never a hard-coded id)
Cr   Opening balance equity / opening clearing account
```

Source identity, using the Phase 0 contract unchanged:

```
organization          = the organization
source_document_type  = "INVENTORY_OPENING"
source_document_id    = the opening document's number
source_event          = SourceEvent.POSTED
idempotency_key       = "inventory-opening:<document number>"
```

Requirements: a signed cutoff timestamp; source evidence (count sheet
reference); approval by `inventory.post_opening_stock`; per line the item,
warehouse, lot, quantity, unit cost, and total value; **one cutoff date for the
whole document** — mixed opening dates are refused, because an "opening
balance" spread across dates is not an opening balance.

Correction is by reversal of the whole document, never by editing a line.

### The blocking gap: no posting profile exists

**Inspected, and confirmed absent.** There is no `AccountRole`, no
`PostingProfile`, no account-mapping table anywhere in `apps/accounting`.
`JournalEntry.posting_rule_version` is a free-text label — a version stamp,
not a mapping. Today the only way to reach an account is by code or primary
key, and §P forbids hard-coding either.

**Minimum required before Task 1.3** (and no more than this):

| Component | Shape |
|---|---|
| `AccountRole` | Closed enum: `INVENTORY_CONTROL`, `INVENTORY_OPENING_EQUITY`, `INVENTORY_COUNT_VARIANCE`, `INVENTORY_WASTE_EXPENSE`, `INVENTORY_IN_TRANSIT`, `INVENTORY_SHORTAGE_LOSS`, `INVENTORY_ADJUSTMENT` |
| `AccountMapping` | `(organization, role, item null, item_category null, effective_from, effective_to) → Account` |
| Resolver priority *(amended)* | **item-specific → item-category → organization default → `account_role_unmapped`.** Most specific wins; there is no fallback to a guess |
| `resolve_account(role, *, organization, category, on_date)` | Selector; raises `account_role_unmapped` naming the role — never falls back to a guess |
| Effective dating | Yes. A chart change mid-year must not restate prior postings |

This is a small, well-bounded addition to `apps/accounting`, and it belongs
there rather than in `apps/inventory`: the chart is organization-owned
accounting property, and Purchases, Sales, and Payroll will all need the same
resolver. Building it inside Inventory would guarantee a second one later.

**It is a Task 1.3 prerequisite and is not built in Task 1.1.** Correspondingly
`InventoryItem` carries **no** `inventory_account` foreign key — account
resolution has exactly one home, and a second one on the item would compete
with it silently.

---

## 12. Permissions and scope (§S)

Eighteen permissions, closed, following the ADR-016 model exactly: a Django
permission says *what*, a membership says *where*, and neither alone is
authorization.

| Permission | Scope | Sensitive |
|---|---|---|
| `inventory.view_item` | Organization | |
| `inventory.manage_categories` | Organization | |
| `inventory.manage_items` | Organization | |
| `inventory.manage_conversions` | Organization | **Yes** — changes how quantities are interpreted |
| `inventory.manage_warehouses` | Branch | |
| `inventory.view_stock` | Branch | |
| `inventory.view_valuation` | Branch | **Yes** — exposes cost and margin |
| `inventory.create_draft_movement` | Branch | |
| `inventory.post_opening_stock` | Organization | **Yes** — sets the ledger's starting point |
| `inventory.post_receipt` | Warehouse | |
| `inventory.post_issue` | Warehouse | |
| `inventory.post_transfer` | Warehouse (both ends) | |
| `inventory.post_waste` | Warehouse | **Yes** — destroys value |
| `inventory.conduct_stock_count` | Warehouse | |
| `inventory.approve_stock_count` | Branch | **Yes** — separation of duties from conducting |
| `inventory.post_adjustment` | Branch | **Yes** — arbitrary quantity and value change |
| `inventory.reverse_movement` | Branch | **Yes** |
| `inventory.override_negative_stock` | Organization | **Yes** — the most sensitive |

**Warehouse scope is new** and needs the Phase 0 layer extended: a
`WarehouseMembership`, or a `warehouses` allow-list on `BranchMembership`. The
default when a user holds branch access but no warehouse restriction is
**all warehouses in that branch** — restriction is opt-in, so introducing
warehouse scope cannot silently lock out existing users.

### Approved default role map

| Role | Inventory permissions |
|---|---|
| **OWNER** | All eighteen — a trusted operational proprietor |
| **ACCOUNTING_MANAGER** | `view_item`, `view_stock`, `view_valuation`, `post_opening_stock`, `approve_stock_count`, `post_adjustment`, `reverse_movement`, `override_negative_stock` |
| **MANAGER** | `view_item`, `manage_categories`, `manage_items`, `manage_conversions`, `manage_warehouses`, `view_stock`, `view_valuation`, `create_draft_movement`, `post_receipt`, `post_issue`, `post_transfer`, `post_waste`, `conduct_stock_count`, `approve_stock_count`, `post_adjustment`, `reverse_movement` |
| **STOREKEEPER** | `view_item`, `view_stock`, `create_draft_movement`, `post_receipt`, `post_issue`, `post_transfer`, `conduct_stock_count` |
| **PURCHASING** | `view_item`, `view_stock`, `view_valuation` |
| **ACCOUNTANT** | `view_item`, `view_stock`, `view_valuation` |
| **VIEWER** | `view_item`, `view_stock` |

The separations that matter, each deliberate:

- **A storekeeper never sees cost.** No `view_valuation`. They also cannot
  approve a count, post waste, adjust, reverse, or override.
- **`MANAGER` holds both conduct and approve — and still cannot approve their
  own count.** Maker-checker is enforced on the *act*
  (`approver_id != conductor_id`), not on the permission. Holding both is a
  convenience for a small branch, never a licence to sign off your own work.
- **`PURCHASING` gets no master-data mutation and no receipt posting.**
  Ordering and taking custody are different jobs; whoever chose the supplier
  should not also confirm what arrived.
- **A normal `ACCOUNTANT` cannot post opening stock.** It sets the ledger's
  starting point, so it sits with `ACCOUNTING_MANAGER` and `OWNER`.
- **`ACCOUNTING_MANAGER` performs no warehouse operations** — no receipt,
  issue, transfer, or count. They see everything and approve; they do not
  move stock.

**Role names are mapping defaults and never appear in a domain service.** A
deployment changes this table; no inventory code changes with it.

## 13. API and UI contract (§T)

### Commands, not CRUD

Posted stock movements are never exposed as a writable resource. The endpoint
set follows the document model:

```
POST   /api/v1/inventory/openings/                 create draft
PATCH  /api/v1/inventory/openings/{id}/            amend draft only
POST   /api/v1/inventory/openings/{id}/post/
POST   /api/v1/inventory/openings/{id}/reverse/

POST   /api/v1/inventory/documents/                create draft (typed)
PATCH  /api/v1/inventory/documents/{id}/           amend draft only
DELETE /api/v1/inventory/documents/{id}/           discard draft only
POST   /api/v1/inventory/documents/{id}/post/
POST   /api/v1/inventory/documents/{id}/reverse/

POST   /api/v1/inventory/counts/                   open a count (freezes)
POST   /api/v1/inventory/counts/{id}/submit/
POST   /api/v1/inventory/counts/{id}/approve/      posts the adjustments

GET    /api/v1/inventory/stock/                    on-hand, scoped
GET    /api/v1/inventory/movements/                history, scoped, read-only
```

Reusing Phase 0 conventions without exception: session auth by default,
`OutOfScope` → 404, `PermissionMissing` → 403, domain conflicts → 409,
validation → 422, and **every decimal transported as an exact string in both
directions**.

### Native UI

Inside the existing shell, Arabic RTL, Django templates + htmx 2.0.4
(vendored, no CDN, no Node) per ADR-011. **No new frontend framework.** Normal
users are never sent to Django admin, which stays a developer tool.

First screens, in build order: Item Categories → Items → Item Conversions →
Warehouses → Stock on Hand → Movement History.

The `inventory` module **already exists** in `apps/core/navigation.py`
(`key="inventory"`, phase المرحلة ١) with fifteen sections stubbed and
`available=False`: الأصناف, مجموعات الأصناف, المخازن ومواقع المطبخ, الأرصدة
الافتتاحية, الإدخال المخزني, الصرف المخزني, التحويلات, المرتجعات, الهالك
والتلف, الجرد, التسويات, حركة المخزون, تقييم المخزون, حدود إعادة الطلب.

**One section is missing and must be added in Task 1.1: item conversions**
(تحويلات وحدات الصنف). It is a first-class Release 1 screen — §4 makes
item-specific packaging the difference between a usable ledger and a wrong one
— and the existing rail has no slot for it. Not added here, because Task 1.0
builds no UI.

---

## 14. Test and reconciliation matrix (§U)

The 28 required checks, each mapped to the phase that must deliver it. `AT-*`
identifiers are **established here** — they appear nowhere in the repository or
source documents and are recorded from the descriptions supplied with this
task.

| AT | Meaning |
|---|---|
| AT-002 | Inventory value reconciles to the general ledger |
| AT-007 | Stock balance rebuilds exactly from the movement ledger |
| AT-008 | Scope and privacy — no cross-tenant read or write |
| AT-009 | Idempotency — one economic event, one effect |
| AT-011 | Historical effective data is not silently restated |
| AT-012 | Import atomicity |

| # | Test | Task | AT |
|---|---|---|---|
| 1 | Item code unique per organization | 1.1 | AT-008 |
| 2 | Foreign-organization item injection blocked | 1.1 | AT-008 |
| 3 | Foreign-branch warehouse access blocked | 1.1 | AT-008 |
| 4 | Base-UoM dimension validation | 1.1 | |
| 5 | Fixed package conversion | 1.1 | |
| 6 | Variable-weight package conversion | 1.1 | |
| 7 | Conversion snapshot stays historical after the master changes | 1.2 | AT-011 |
| 8 | No float in transport or storage | 1.1 | |
| 9 | Posted movement immutable | 1.2 | |
| 10 | Reversal restores quantity and value exactly | 1.4 | |
| 11 | `StockBalance` rebuild equals ledger replay | 1.2 | AT-007 |
| 12 | Moving-average calculation, all 18 cases | 1.2 | |
| 13 | Full depletion leaves no unexplained residual | 1.2 | AT-007 |
| 14 | Concurrent issue cannot create negative stock | 1.2 | |
| 15 | Negative stock blocked | 1.2 | |
| 16 | Unauthorized override rejected | 1.2 | AT-008 |
| 17 | Closed-period movement rejected | 1.2 | AT-011 |
| 18 | Duplicate source event cannot double-post | 1.3 | AT-009 |
| 19 | Same key + changed payload conflicts | 1.3 | AT-009 |
| 20 | Same key in another organization is independent | 1.3 | AT-009 |
| 21 | Transfer dispatch reconciles to receipt + shortage | 1.5 | AT-002 |
| 22 | Opening stock value equals its journal entry | 1.3 | AT-002 |
| 23 | Inventory control balance reconciles to valuation | 1.3 | AT-002 |
| 24 | Audit captures authoritative before/after state | 1.2 | |
| 25 | Arabic locale does not change technical decimal strings | 1.1 | |
| 26 | Fresh database receives inventory reference data | 1.7 | |
| 27 | Import rollback is atomic | 1.7 | AT-012 |
| 28 | Real COMMIT-boundary constraints exercised | 1.2 | |

---

## 15. Decision table — resolved (§W)

**All fourteen decisions were approved on 2026-08-09.** Where approval changed
the recommendation, the amendment is stated; the original is kept so the change
is visible rather than quietly overwritten.

| # | Decision | Resolution | Amended at approval? |
|---|---|---|---|
| 1 | Item-code format | `^[A-Z0-9][A-Z0-9._-]*$`, ≤32, unique per organization, manual **or** generated, reserved forever | **Yes** — canonicalised `strip().upper()` before validation and storage; whitespace-only refused |
| 2 | Category hierarchy | Organization-owned, parent/child, depth ≤3, items on leaves only | **Yes** — six explicit guards added, including "a category with items cannot acquire children" and re-parent depth/cycle checks |
| 3 | `ItemType` enum | Closed, **six** values | **Yes** — `FINISHED_GOOD` is **required**, meaning physically stored and countable output. It does **not** mean menu item; those remain separate domains |
| 4 | Warehouse vs location | Warehouse branch-owned and owns valuation; location warehouse-owned, quantity only, deferred to 1.7 | **Yes** — closed `warehouse_type`: `PHYSICAL`, `PRODUCTION_WIP`, `IN_TRANSIT`; codes unique per branch |
| 5 | Valuation key | `(warehouse, item, lot)`; organization and branch derivable | **Yes** — null-lot uniqueness stated explicitly (`nulls_distinct=False`, or two partial constraints); never left to SQL NULL semantics |
| 6 | Lot/expiry | Opt-in per item; average per lot when enabled; manual selection; no FEFO | No |
| 7 | Fixed vs variable packages | Both; `VARIABLE` requires a measured base quantity at posting | **Yes** — packages are a **separate `PackageUnit` model with no factor field**, never `UnitOfMeasure` rows; all conversions resolve **directly** to the item's base unit, no chains |
| 8 | Backdated movements | Backdatable within an OPEN period; valuation follows posting order | **Yes** — three timestamps retained (`effective_at`, `posted_at`, `posted_sequence`); reports must name which cutoff they use; bare "as of" forbidden |
| 9 | Negative-stock override | Organization-scoped permission + reason + actor + audit + exception report | **Yes** — **no permanent per-item flag.** Override is a per-posting exception. Reversals that decrease stock are **not** exempt |
| 10 | Inventory account mapping | `AccountRole` + effective-dated `AccountMapping` in `apps/accounting`, a Task 1.3 prerequisite | **Yes** — resolver priority item → category → organization default → `account_role_unmapped`; **no `InventoryItem.inventory_account`** |
| 11 | Opening cutoff and source | One cutoff per document; evidence; approval; reversal-only correction | No |
| 12 | Count freeze | Warehouse-level `HARD_FREEZE`; blind count; approval separate from counting | **Yes** — maker-checker enforced (`approver_id != conductor_id`) even when one person holds both permissions; positive gains need an explicit unit cost where the average is zero or undefined |
| 13 | `source_document_id` durability | Keep `varchar(64)` — already type-agnostic | **Yes** — normalise **centrally in the accounting service**; prefer the immutable internal UUID/PK, keep the human number separately |
| 14 | Warehouse permission scope | Extend `BranchMembership` with `warehouse_scope_mode` (`ALL` / `SELECTED`) plus `BranchMembershipWarehouse` | **Yes** — no independent role-bearing `WarehouseMembership`; existing memberships default to `ALL` |

### Still open

Nothing blocks Task 1.1.

**Deferred, and honestly so:** reconciliation against an authoritative SRS,
because no SRS exists in this repository (§0). The `INV-*` and `AT-*`
identifiers remain repository-local acceptance identifiers until one is
supplied.

## 16. What Task 1.0 deliberately did not do

No models, no migrations, no services, no API, no UI, no seed data, no
warehouse names, no opening balances. No supplier, purchase, recipe,
production, sales, or payroll work. `source_document_id` was inspected and
left unchanged.
