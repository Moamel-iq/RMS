# Task 3.0 — Recipes, Kitchen and Production domain specification

- **Status:** Specification only, **awaiting approval**. Task 3.0 creates no
  models, migrations, services, API or UI. Implementation begins at Task 3.1,
  and only after this document is approved — with amendments recorded here,
  the way Task 1.0's were.
- **Date:** 2026-08-16
- **Branch:** `phase/3-kitchen`, from the `phase/2-procurement` head
  (`55361ff`), after tags `phase-2-procurement-complete` and
  `accounting-module-complete`.
- **Related:** ADR-006, ADR-012, ADR-016 – ADR-020,
  `docs/invariants/inventory-invariants.md`,
  `docs/invariants/procurement-invariants.md`,
  `docs/invariants/kitchen-invariants.md` (proposed),
  `docs/tasks/phase-3-task-breakdown.md`, proposed ADR-024 and ADR-025.

Recipes are where stock becomes menu. Everything Phase 4 will claim about a
sale's cost and margin rests on what a recipe says a dish consumes and what
the ledger says those ingredients were worth, so the contract has to be
settled before any of it is written. The charter names the phase "Recipes,
Kitchen, Production, and Costing" and states why it sits here: *"This module
consumes inventory and uses its weighted-average costs. It creates the bridge
between stock and menu profitability."*

---

## 0. Source material, and what is missing from it

| Source | Where | Used for |
|---|---|---|
| Architecture charter | `docs/architecture/architecture-charter.md` — Part 1 §3 (yield is not a conversion), §6 (recipe costing), Phase 3 scope, the approved rules list | Every scope line and formula in this document |
| ADR-006 | `docs/decisions/` | Quantity precision; `CALCULATION_PLACES = 6` was reserved for "recipe lines" from the start |
| ADR-012 | `docs/decisions/` | Money precision; allocation; recipe costing named on the never-round list |
| ADR-018 / ADR-019 / ADR-020 | `docs/decisions/` | The valuation kernel, role resolution, and the same-branch accounting precedent production leans on |
| Kernel invariants | `docs/specs/accounting-kernel-invariants.md` | What a posting must satisfy |
| Inventory invariants | `docs/invariants/inventory-invariants.md` | What a stock movement must satisfy |
| Phase 1–2 code | `apps/inventory`, `apps/accounting`, `apps/procurement`, `apps/units` | The actual kernel, not a summary of it — including everything it already reserved for this phase |

**There is still no SRS.** `docs/requirements/SRS.md` is referenced by
`CLAUDE.md` and has never been added; Task 1.0 §0 and Task 2.0 §0 recorded
the same absence. Nothing has changed. The correct statement is therefore the
one Task 1.0 established:

> No contradiction was found against the architecture plan, the approved
> ADRs, and the current implementation. Reconciliation against the
> authoritative SRS has not been completed because the SRS is absent from the
> repository.

Every `RCP-*` requirement below traces to the architecture charter, to an
ADR, or to the Phase 1–2 implementation. They are **repository-local
identifiers** until an authoritative SRS arrives and is mapped.

**What earlier phases already reserved for this one.** This spec invents less
than it looks like it does, because Phase 1 laid vocabulary down deliberately:
`MovementType.PRODUCTION_IN` / `PRODUCTION_OUT` exist in the closed enum with
their directions fixed; `WarehouseType.PRODUCTION_WIP` exists;
`ItemType.SEMI_FINISHED` and `FINISHED_GOOD` exist with the boundary already
documented ("Menu items belong to Sales and Recipes and are linked to
inventory through a recipe in Phase 3, never by sharing a row");
`InventoryLot.produced_by_document_type` / `produced_by_document_id` exist,
written by nothing yet; ADR-006 reserved 6-decimal intermediate precision for
recipe lines; and `apps/units` records that production yield "is not a
conversion at all; it is a production outcome with loss and variance." This
document's job is to spend that vocabulary, not to re-mint it.

---

## 1. The three kinds of event, and why only one moves stock

The charter's Phase 3 scope lists fifteen bullets. They resolve into three
kinds of event, and keeping them apart is the load-bearing decision:

| Event | Stock effect | Accounting effect | Owns |
|---|---|---|---|
| Editing a recipe or version | none | none | Recipe master data |
| Approving a recipe version | none | none | The version lifecycle |
| **Posting a production batch** | **ingredients out, output in, one atomic entry** | **per-account net, often nothing (§8)** | The only stock-moving act in this phase |
| Recording a staff or complimentary meal | none | none (Release 1 — §10) | The consumption ledger's memo side |
| Kitchen issue, return, waste, transfer | already Phase 1's | already Phase 1's | `apps.inventory`, unchanged |

Read the "already Phase 1's" row carefully, because it is where this phase
gets smaller and more honest at the same time. The charter's own definition
of actual consumption is:

> Warehouse issues to kitchen
> + production usage
> + recorded waste
> + stock adjustments
> + transfers into the kitchen
> − returns from the kitchen
> = actual consumption

Every term except "production usage" is a document that shipped in Phase 1:
the issue, the return-from-issue, the waste document, the adjustment, the
transfer. **Actual consumption is therefore a derived read over movements
that already exist, plus the production usage this phase adds — not a new
family of documents.** Building second kitchen-flavoured copies of Phase 1
documents would give the variance report two sources for one fact.

**RCP-001.** Phase 3 adds exactly one stock-moving document: the production
batch. Kitchen issues, returns, waste, adjustments and transfers remain the
Phase 1 documents, posted through the Phase 1 services, unchanged. If the
kitchen needs a movement shape inventory does not have, that is a change to
inventory, made in inventory, with inventory's tests — the same rule
procurement followed (PRC-029).

**RCP-002.** Recipe and version edits touch no stock and no journal, ever. A
recipe is an intention; the batch is the event. Nothing in the recipe
aggregate may write to `apps.inventory` or `apps.accounting`.

**RCP-003.** No Phase 3 model may combine two of the events above in one row.
A production batch that also edited its recipe, or a meal record that also
moved stock, would re-create the one-editable-form failure the charter warns
against.

---

## 2. The app and its boundary

**RCP-004.** The module is one new app, `apps.kitchen`, matching the
navigation module key (`kitchen`, "المطبخ والوصفات") that has carried its
thirteen planned sections since Phase 0. It imports `apps.inventory`,
`apps.accounting` and `apps.units`; nothing imports it. Phase 4 will import
it to read recipes — the dependency arrow points from sales to kitchen, never
back.

**RCP-005.** The kitchen module owns recipes, versions, production batches,
meal records, and the consumption/variance/costing reads. It owns **no**
account-role overrides, no second posting path, no copy of any inventory
document, and no menu item — a menu item is a Phase 4 model that will point
at a recipe from its own side.

---

## 3. The recipe

```
Recipe
    organization        FK, PROTECT — recipes are organization master data
    code                canonical strip().upper(), unique per organization
    name_ar             the dish, in the kitchen's language
    name_en             optional
    recipe_type         BATCH | PORTION
    output_item         FK InventoryItem, nullable — required for BATCH (§7)
    notes               free text
    is_active           archive flag; never deleted
    public_id           UUID, immutable
```

**RCP-006.** A recipe is organization master data, shared across branches
like a supplier or an item. Which branches actually *use* a version is a
property of the version (§4), not of the recipe — the dish is one dish; where
it is cooked varies.

**RCP-007.** The charter's two recipe kinds are a closed type, not two
models. A **batch recipe** produces a stored inventory item (`output_item`,
type `SEMI_FINISHED` or `FINISHED_GOOD`): a pot of mandi rice, a batch of
sauce. A **portion recipe** describes a plated dish assembled to order — its
output is a menu item that is deliberately **not** an `InventoryItem`
(the boundary Phase 1 documented and tested), so `output_item` is null and
the recipe exists for costing and consumption arithmetic, never for stock.

**RCP-008.** `output_item`, when set, must belong to the same organization
and be of type `SEMI_FINISHED` or `FINISHED_GOOD` — enforced by a database
`CheckConstraint` on type where possible and by the service everywhere. A
batch recipe producing a `RAW_MATERIAL` is a data-entry error with accounting
consequences.

**RCP-009.** A recipe carries **no cost field**. Every cost is derived from a
version's lines against the ledger's moving averages, every time it is asked
for, or read from an explicit dated snapshot (§6). A stored "current cost" is
a second source of truth that drifts — the same rule as the supplier balance
(PRC-003).

**RCP-010.** Menu-item mapping in this phase is the recipe's own identity
(`code`, names, `public_id`). Phase 4's `MenuItem` will carry the foreign
key. Phase 3 builds nothing speculative for it — no placeholder menu model,
no mapping table.

---

## 4. Recipe versions

```
RecipeVersion
    recipe              FK, CASCADE-protected by PROTECT on posted references
    version             1, 2, 3 … unique per recipe
    status              DRAFT → APPROVED → SUPERSEDED
                                └→ DISCARDED (drafts only)
    effective_from      date
    effective_to        date, nullable — open-ended
    batch_size          Decimal(qty), the quantity the line quantities describe
    expected_output     Decimal(qty) — what batch_size of inputs should yield
    portions_per_batch  Decimal(qty), nullable — plate-cost divisor (§6)
    preparation_loss    rate, 6 dp, informational (§5)
    cooking_yield       rate, 6 dp, informational (§5)
    branches            M2M, empty = every branch
    approved_by / at    the checker; never the author
    created_by / at
    notes, instructions free text — the preparation card
    public_id           UUID, immutable
```

**RCP-011.** Recipe versions are **effective-dated**, and resolution by date
is the only resolution: the version whose `[effective_from, effective_to]`
range covers the asked-for date, for the asked-for branch. The charter's rule
is verbatim and absolute: *"Historical sales must use the recipe version that
was effective when the item was sold. A recipe changed in September must not
silently change the theoretical cost of July sales."*

**RCP-012.** Approved versions of one recipe may not overlap in effective
range for the same branch — a database exclusion constraint, the same
mechanism the supplier catalogue uses (invariant 7). Two versions both
claiming Tuesday would make every historical read ambiguous.

**RCP-013.** Approval is maker-checker: `approved_by` must differ from
`created_by`, enforced by a `CheckConstraint` as well as the service — the
purchase-request pattern (PRC-010). The charter lists "Approval status" as a
recipe attribute; a recipe that costs meals and drives consumption
arithmetic is a claim about money, and somebody other than its author agrees
to it.

**RCP-014.** An **approved version is immutable** — header and lines — except
for closing its effective range (setting `effective_to`) and superseding it
with the next version. Correcting an approved version means a new version.
Posted batches snapshot the version they used; editing it after the fact
would restate what they meant. Drafts are freely editable and may be
discarded.

**RCP-015.** Only `APPROVED` versions may be produced from, costed for a
report, or counted in theoretical consumption. A draft is somebody typing.

**RCP-016.** Version numbers are per-recipe, sequential, assigned at
creation. Superseding is explicit: approving version N+1 with an overlapping
open range closes version N's range at N+1's `effective_from − 1 day`, in
the same transaction, and records the supersession in the audit trail.

**RCP-017.** Branch applicability is a restriction, not a copy: a version
either applies at a branch or does not. Per-branch quantity variations are
different versions (or different recipes). One version with per-branch line
overrides would make "what does this dish consume" have no single answer.

---

## 5. Recipe lines

```
RecipeLine
    version             FK, CASCADE while draft; frozen with the version
    sequence            explicit, unique per version
    item                FK InventoryItem, PROTECT
    quantity            Decimal, 6 dp (CALCULATION_PLACES) — per batch_size,
                        GROSS: what leaves stores, not what survives cooking
    unit                the entry unit; converted through the item's
                        conversions to base at entry, both recorded
    base_quantity       Decimal, 6 dp — the converted figure costing uses
    is_optional         flag — costed, but a batch may omit it
    note                free text

RecipeLineSubstitute
    line                FK, CASCADE
    substitute_item     FK InventoryItem, PROTECT
    priority            explicit ordering
```

**RCP-018.** Line quantities are **gross**: the quantity issued to the
kitchen for one `batch_size`, before preparation loss and cooking shrinkage.
`preparation_loss` and `cooking_yield` on the version are recorded rates —
the charter lists both — but they are **informational**: the costing input is
the gross line quantity, and the yield reality is captured where it actually
happens, on the batch (§9). Encoding loss both as a rate and inside the
quantities would double-count it the day the two disagree.

**RCP-019.** Line quantities persist at `CALCULATION_PLACES` (6 dp) — the
precision ADR-006 reserved for exactly this. A recipe consumes 0.008 kg of
saffron per batch; three stored decimals would round it to 0.008 → fine, but
0.0004 of a costly spice to zero. Conversion to base units happens once at
entry through the item's own conversions, full precision carried, quantized
once at the storage boundary (ADR-006's counterexample rule).

**RCP-020.** A line's item may be any active inventory item of the same
organization. Lot-level detail does not belong on a recipe: which lot a batch
consumes is decided at the batch, by the kernel's ordinary rules.

**RCP-021.** Optional lines are costed by default and omittable per batch. An
optional line is a real ingredient that is sometimes skipped — costing it at
zero would understate every plate cost that includes it.

**RCP-022.** Substitutes are an informational table: the batch screen offers
them when the primary item is short, and the batch records what was
**actually** consumed. Costing always uses actual consumption at posting; the
substitute table never enters cost arithmetic. (The charter lists substitute
ingredients as a recipe attribute; this is the smallest honest reading — a
suggestion vocabulary, not an alternate bill of materials.)

---

## 6. Recipe costing, plate cost, and snapshots

**RCP-023.** A version's cost is **derived, never stored on the version**:

```
version cost (as of date D, warehouse W)
    = Σ over lines: base_quantity × moving average of (item, W) as of D
plate cost = version cost ÷ portions_per_batch
```

computed at full precision, quantized once at the money boundary (3 dp). The
"as of" uses the posted-as-of read the Phase 1 reports already implement —
the audit answer, reproducing what the books said.

**RCP-024.** The charter's invariant is the test: *"A recipe cost equals the
sum of its effective component costs."* No cached figure may exist that could
disagree with that sum.

**RCP-025.** A **cost snapshot** is an explicit, dated, immutable record: the
version, the date, the per-line unit costs and extensions, and the plate
cost, written by a person or a scheduled read — never implicitly by editing.
The charter lists "Cost snapshot or cost-calculation date" as a recipe
attribute; the snapshot is how a menu decision ("we priced the mandi off
March costs") stays explicable in September. Snapshots are append-only.

**RCP-026.** Historical cost questions resolve version-first, then costs:
the version effective at the asked-for date (RCP-011), costed at that date's
averages. Both halves are date-driven; neither may silently use today.

**RCP-027.** Cost visibility is a separate permission (§13), the
`view_supplier_cost` pattern: a cook reads the preparation card and the
quantities; what the dish costs is not part of cooking it.

---

## 7. Production batches

```
ProductionBatch
    organization / branch     denormalised, like every posting document
    warehouse                 FK — ONE warehouse; inputs leave it and the
                              output enters it (§8)
    recipe_version            FK RecipeVersion, PROTECT — APPROVED only
    number                    gapless per organization, drawn at posting
    multiplier                Decimal — how many batch_sizes this batch is
    status                    DRAFT → POSTED → REVERSED
    produced_at               business date + the branch's cutoff snapshot
    output_quantity           Decimal(qty) — ACTUAL output, entered
    output_lot                created at posting when the item tracks lots
    stock_entry / journal_entry   links, written once at posting
    posted_by / reversed_by / reasons / audit people
    public_id                 UUID, immutable — the source-document identity

ProductionBatchLine
    batch                FK
    sequence             explicit
    item                 FK — from the recipe line or a recorded substitute
    recipe_line          FK, nullable — the line this fulfils, if any
    planned_quantity     scaled from the recipe at draft: line × multiplier
    consumed_quantity    ACTUAL, editable while draft, 3 dp at posting
    lot                  nullable — chosen at posting where tracked
```

**RCP-028.** A batch is drafted **from an approved recipe version**, scaled
by its multiplier: lines pre-filled at `base_quantity × multiplier`, then
adjusted to what the kitchen actually used before posting. Ad-hoc production
with no recipe is deliberately absent in Release 1 (§16): a batch with no
recipe has no theoretical side, and the variance report is the point of the
module.

**RCP-029.** The batch names **one warehouse**. Ingredients are consumed from
it; the output enters it. Producing "into" another warehouse is a batch plus
a Phase 1 transfer — two documents, because two things happened. This also
keeps the lock footprint the kernel's canonical-order rules already handle.

**RCP-030.** `consumed_quantity` is the truth and the recipe is the plan. An
operator may consume more, less, omit an optional line, or add a substitute
line. The batch records reality; the *difference* from plan is the batch
variance report's business (§9), never a posting refusal — refusing would
teach kitchens to falsify quantities to match the recipe.

**RCP-031.** `output_quantity` is entered, not derived: the scale decides,
exactly as a variable-package receipt's measured weight does (PRC-026). An
expected figure (`expected_output × multiplier`) is displayed beside it; a
batch may not post with a zero or negative output.

**RCP-032.** Only batch recipes (with `output_item`) can be produced.
Portion recipes describe assembly-to-order; "producing" one would create
stock of an item that deliberately does not exist (RCP-007).

**RCP-033.** Draft batches are editable and deletable; posted batches are
immutable except reversal — whole-row allowlist triggers, the same mechanism
every posted document already uses.

---

## 8. Posting a batch: valuation, and the journal that is usually silence

Posting is one atomic act: one stock ledger entry carrying every
`PRODUCTION_OUT` effect and the `PRODUCTION_IN` effect, the gapless number,
the audit event, and the journal **if there is anything to say** — or none of
it.

**RCP-034.** **Value is conserved through the batch.** Each consumed line
leaves at the kernel's moving average (an ordinary outbound; the kernel's
exact-depletion and negative-stock rules apply unchanged). The output enters
with `inbound_value = Σ consumed values` — the exact-figure channel the
kernel built for returns and transfer receipts (`MovementInput.inbound_value`:
"arithmetic the caller already did against a posted movement"). No value is
created, destroyed, or re-derived through `quantity × unit_cost`.

**RCP-035.** **Yield loss is absorbed, not posted.** If 50 kg of inputs worth
70,000 become 42 kg of cooked rice, the 42 kg are worth 70,000 and the unit
cost says so. There is no yield-variance journal, and that is a decision, not
an omission: this is a moving-average system with no approved standard cost,
so there is no approved figure to hold a variance against. Yield problems
surface on the yield report (§9) and as unit-cost drift — where a kitchen
manager can see them — not in a GL account nobody reconciles. (Proposed
ADR-025 records this; a future standard-costing election would supersede it
explicitly.)

**RCP-036.** The journal is the **per-account net** of the batch's movements:
ingredients leave through the control accounts their balances carry (ADR-019
§7 — "an outbound leaves through the account it entered"); the output enters
through its own item's resolved `INVENTORY_CONTROL`. Lines are netted per
account, zero-value lines are omitted (the kernel refuses them), and **when
every account nets to zero — the common case, one shared inventory control
account — no journal exists at all.** A journal that says nothing is not
written. The alternative, washing every batch through a production clearing
account so a journal always exists, was considered and rejected: two entries
that always net to zero are motion without information, and the batch's
source identity lives on the **stock** ledger entry either way — the
drill-down goes to the stock ledger, which is where a production event's
truth is.

**RCP-037.** When the nets are non-zero (item-scoped control mappings differ
between inputs and output), the journal is:

```
Dr  Inventory control (output's account)      Σ output value entering it
    Cr  Inventory control (each input's)      that account's consumed value
```

netted, with the batch's source identity (`KITCHEN_PRODUCTION_BATCH` /
`public_id` / `POSTED`), satisfying `verify_inventory_against_gl` by
construction: the GL moves on exactly the accounts, branches and values the
movements moved.

**RCP-038.** When the output item tracks lots, posting creates the lot and
writes `produced_by_document_type` / `produced_by_document_id` — the fields
Phase 1 reserved with the comment "nothing writes them in Phase 1." Expiry
follows the item's `shelf_life_days` from the batch's business date.

**RCP-039.** Expired ingredients cannot enter a batch: `PRODUCTION_OUT` is
deliberately **not** in the kernel's expired-lot removal set, and stays out.
The rule's documented purpose is "stop expired food reaching a kitchen";
production is that exact path.

**RCP-040.** Reversal mirrors the original — the output leaves at its posted
value, each ingredient returns at its consumed value, through the kernel's
standard reversal which already refuses when the output has since been
consumed (availability is checked). Once only, with a reason, elevated
permission (§13). Idempotency and identity follow ADR-017 unchanged: key
unique per organization with a request fingerprint, one journal per
`(organization, KITCHEN_PRODUCTION_BATCH, public_id, POSTED)`.

---

## 9. Yield, loss, and batch variance — reads, not postings

**RCP-041.** The yield report derives, per posted batch and in aggregate per
recipe version: expected output (`expected_output × multiplier`) versus
actual `output_quantity`; yield ratio; input-side variances per line
(`consumed − planned`); and the cost consequence (actual unit cost versus
the version's as-of plate cost). Nothing here posts. The charter's list of
what variance reveals — over-portioning, noncompliance, unrecorded waste,
theft, wrong counts, wrong conversions, wrong recipes, yield problems — is a
list of questions for a manager, not journal entries.

**RCP-042.** Preparation-loss and cooking-yield rates on the version (RCP-018)
appear on this report beside the measured reality, which is how the
informational rates earn their keep: a version claiming 85% yield whose
batches run 70% is a recipe that needs correcting — through a new version.

---

## 10. Staff meals and complimentary meals

```
MealRecord
    organization / branch
    meal_type            STAFF | COMPLIMENTARY
    recipe               FK Recipe (portion recipes included), PROTECT
    recipe_version       resolved at record time by date and branch, frozen
    quantity             Decimal(qty) — portions
    consumed_on          business date
    reason / beneficiary free text — "وجبة موظفين الوردية المسائية"
    recorded_by / at     audit
    status               RECORDED → CANCELLED (a correction, audited)
    public_id            UUID
```

**RCP-043.** Meal records move **no stock and post no journal in Release 1**,
and the reasoning must be stated because it is counter-intuitive: the
ingredients of a staff meal were already physically consumed by kitchen
issues and production batches — their cost has already left stock through
Phase 1's postings. What a meal record adds is the **explanation**: it enters
the theoretical-consumption side (§11) so that fed-but-not-sold portions do
not surface as unexplained variance. A second stock posting would
double-count the physical flow.

**RCP-044.** Reclassifying staff-meal cost out of consumption expense into a
staff-benefit expense account is real accounting practice and is **deferred,
recorded**: it needs an approved journal shape, an expense role, and a
theoretical-cost basis for the transfer, none of which exists in any approved
document. The deferral follows the standing discipline (PRC-044's shape): the
records exist from day one, so the reclassification task, when approved,
starts with its data already accumulated.

**RCP-045.** Meal records are corrections-by-cancellation, not edits: a
recorded meal that was wrong is cancelled with a reason and re-recorded. The
variance report reads only `RECORDED` rows.

---

## 11. Theoretical consumption, actual consumption, and the variance

**RCP-046.** **Actual consumption** is the charter's formula, computed as a
read over posted Phase 1 and Phase 3 movements for a **selected kitchen
warehouse** and period: issues in, plus production consumption, plus waste,
plus adjustments, plus transfers in, minus returns out of the kitchen. No
new flag identifies "the kitchen" — the reader selects the warehouse, the
same way every Phase 1 report scopes. (A future `is_kitchen` convenience
flag would be presentation, and can arrive with evidence it is needed.)

**RCP-047.** **Theoretical consumption** for a period is
`Σ (recorded quantities × the version effective at each record's date)` over
the quantity sources that exist: production batches (planned lines), and
meal records. **Sold quantities are Phase 4's contribution** — the charter
places "sales-driven recipe consumption" in Phase 4 explicitly — and the
calculators built here take a quantity source, so Phase 4 plugs sales in
without touching the arithmetic. Until then the report labels its coverage
honestly: variance against a theoretical side that excludes sales is a
production-and-meals variance, and the screen says so.

**RCP-048.** The backflush election is **not made here.** The charter permits
MVP backflush ("approved sales generate recipe consumption automatically")
provided it is "written down as an explicit simplification." Release 1's
actual consumption is manual kitchen issues — already shipped, already the
truth. If Phase 4 elects backflush, Phase 4's specification writes that
decision down with its own ADR; this document's variance machinery works
identically either way, which is the reason the decision can wait.

---

## 12. Reports

Every report obeys the Phase 1 contract: a named cutoff mode, organization
and branch scope from memberships, cost columns omitted rather than blanked
without the cost permission, exact Decimals in CSV with formula
neutralisation, HTMX filters that survive pagination, and no repair button
anywhere.

| Report | Answers |
|---|---|
| Recipe list and card | What a dish is made of, per version, with the preparation card |
| Recipe cost | Version cost and plate cost, as of a date, per warehouse |
| Cost snapshots | What the dish cost when the menu was priced |
| Production log | Batches per period: recipe, multiplier, output, who posted |
| Yield and loss | Expected vs actual output, per batch and per version |
| Batch variance | Planned vs consumed per line, cost consequence |
| Actual consumption | The charter's formula, per kitchen warehouse and period |
| Theoretical consumption | Recorded quantities × effective versions, by source |
| Usage variance | Actual − theoretical, per item, with coverage labelled |
| Meal log | Staff and complimentary meals, by period and reason |

**RCP-049.** The reconciliation obligation, `verify_kitchen`, mirrors its
siblings and proves: (1) every posted batch's stock entry and journal (where
one exists) agree with its lines — consumed values, output value, per-account
nets; (2) value conservation holds for every batch: output inbound value
equals the sum of consumed values, to the fils; (3) every batch journal
traces to exactly one batch; (4) `verify_inventory_against_gl` stays clean
with production movements included — which it will by construction (RCP-037),
and the verifier proves construction met reality.

**RCP-050.** Verification reports and refuses to repair.

---

## 13. Permissions and scope

Every permission is a permission **plus a scope** (ADR-016). Out of scope is
404; in scope without authority is 403.

| Permission | Scope | Notes |
|---|---|---|
| `view_recipe` | organization | The card, quantities, instructions |
| `manage_recipe` | organization | Create, edit drafts, archive |
| `approve_recipe_version` | organization | Never the author |
| `view_recipe_cost` | organization | Costs, snapshots, plate cost — separate from the card |
| `view_production` | **warehouse** | Batches at warehouses the caller reaches |
| `create_production_batch` | **warehouse** | Drafting consumes nothing |
| `post_production_batch` | **warehouse** | It moves stock |
| `reverse_production_batch` | **warehouse** | Elevated, like the receipt's |
| `record_meal` | branch | Staff and complimentary both; a branch act |
| `view_kitchen_report` | organization | The report family, one permission (the Phase 2 pattern) |

**RCP-051.** Production permissions are warehouse-scoped because a batch
moves stock, and inventory already scopes stock movement that way (PRC-060's
rule). Recipes are organization master data and scope like the supplier
catalogue.

**RCP-052.** Cost visibility is separate from document visibility, exactly as
inventory and procurement hold it: a cook sees the recipe, a storekeeper sees
the batch quantities; `view_recipe_cost` gates every money column, omitted
not blanked.

**RCP-053.** No generic writable CRUD for any posted record, no writable
admin. Commands, not table editing.

---

## 14. API, UI and demo

**RCP-054.** The API is commands: `approve_recipe_version`,
`post_production_batch`, `reverse_production_batch`, `record_meal`,
`cancel_meal`. Money and quantities cross the wire as exact strings, both
directions. Every command carries an organization-scoped idempotency key
matched against a request fingerprint (ADR-017, unchanged).

**RCP-055.** Screens are Arabic-first, RTL, inside the existing shell, HTMX
with full-page fallback, CSS logical properties only. The thirteen kitchen
navigation entries go live task by task; none before its screen exists.

**RCP-056.** Demo data extends the existing tooling under
`docs/development/demo-data-policy.md`: namespace `DEMO-KITCHEN-V1`,
idempotent, DEBUG-only, posted operations through the real services. Exactly
**two recipes** — one batch recipe (رز مندي — تجريبي, producing a new
`DEMO-RICE-COOKED` semi-finished item) and one portion recipe (مندي دجاج —
تجريبي, no output item) — against the existing demo items. **One new demo
item is permitted and named here** because a batch recipe requires a
produced output and none of the five Phase 1 demo items is producible; no
other new items. One posted batch, one draft batch to inspect, one staff
meal, one complimentary meal, every kitchen screen showing something.

**RCP-057.** Imports (recipe master and lines, preview-first on the Task 1.7
framework) belong to the hardening task and follow §16.8's boundary: master
data and drafts import; nothing that posts imports.

---

## 15. Source identity and account roles

| Document | `SourceEvent` |
|---|---|
| Production batch | `POSTED` |
| Production batch reversal | `REVERSED` |

Source document type: `KITCHEN_PRODUCTION_BATCH`, owned by `apps.kitchen`,
following the module-constant pattern (`PROCUREMENT_*`, `INVENTORY_*`).
`source_document_id` is the batch's immutable `public_id` (PRC-067's rule).
`SourceEvent` itself is **not extended** — `POSTED` and `REVERSED` suffice,
as they have for every module so far.

Meal records and recipe edits carry no source identity because they post
nothing; they are audited documents, not accounting events.

### New account roles

**None.** This is deliberate and worth a table's worth of emphasis: a
production batch moves value **between inventory control accounts** that
already exist and are already resolved by ADR-019 mechanics — inputs leave
through the accounts their balances carry; the output resolves
`INVENTORY_CONTROL` for its own item. No WIP account (batches are atomic,
same-day, one entry — there is no "in progress" state to hold value), no
yield-variance account (RCP-035), no new domain in `AccountRoleDomain`. The
deferred staff-meal reclassification (RCP-044) will name its expense role in
its own approved task, which is when a role with a posting rule behind it
will exist — "a role with no posting rule behind it is a grant nobody can
audit."

---

## 16. What this specification deliberately does not do

Naming these is the point. An omission that is written down is a decision;
one that is not is a defect waiting to be discovered by an accountant.

1. **No menu items and no sales-driven consumption.** Menu items, sold
   quantities, and the backflush election are Phase 4, per the charter's own
   phase table. The theoretical-consumption calculators take a quantity
   source so Phase 4 plugs in without rework (RCP-047, RCP-048).
2. **No work-in-progress accounting.** A batch is atomic: consumed and
   produced in one posting, one business date. Multi-day WIP holding value
   across a period boundary needs a WIP account, a holding policy and a
   period interaction nobody has approved; a same-day kitchen does not.
3. **No multi-output batches and no by-product valuation.** One batch, one
   output. Splitting one input pool across several outputs requires an
   allocation basis — by weight, by value, by declared share — and the wrong
   basis silently misprices every portion downstream, the same trap as
   landed cost (PRC-046). The recipe's by-product field from the charter is
   honoured as a note until a basis is approved.
4. **No standard costing and no yield-variance postings.** Moving average is
   the system (ADR-018); yield reality is absorbed into unit cost and
   reported, not journalled against a standard nobody has set (RCP-035).
5. **No ad-hoc (recipe-less) production.** A batch without a plan has no
   variance, and the variance is the point. A genuinely new dish gets a
   draft recipe first, which is cheap by design.
6. **No staff-meal expense reclassification** — deferred with its data
   accumulating from day one (RCP-044).
7. **No nutritional, allergen, or regulatory recipe data.** Real, valuable,
   and not an inventory-costing concern; a later field addition, not a
   redesign.
8. **No recipe-level pricing or margin targets.** Selling prices are Phase 4;
   a recipe knows its cost, not its price.
9. **No automatic production suggestions** (par levels driving batch
   proposals). Reorder reporting exists in Phase 1; converting it into
   production orders without a human is a separate decision, the
   no-automatic-reordering rule applied to the kitchen.
10. **No direct import of a posted batch.** Recipes and drafts import;
    a posted batch is only ever created by its service (§16.8's rule,
    unchanged).

---

## 17. Task breakdown

Dependency order. Each is a separate commit with its own tests, gates and
demo data; none begins before its predecessor is green. The full breakdown
with exit gates is `docs/tasks/phase-3-task-breakdown.md`.

| Task | Delivers | Depends on |
|---|---|---|
| 3.0 | This specification, invariants, breakdown, two proposed ADRs | — |
| 3.1 | Recipe master: model, lines, substitutes, screens, demo | 3.0 approval |
| 3.2 | Recipe versions: effective dating, approval, supersession | 3.1 |
| 3.3 | Recipe costing reads, plate cost, snapshots | 3.2 |
| 3.4 | Production batches: drafting, scaling, actual quantities | 3.2 |
| 3.5 | Production posting: valuation, journal, lots, reversal | 3.4 |
| 3.6 | Yield, loss and batch variance reports | 3.5 |
| 3.7 | Staff and complimentary meal records | 3.2 |
| 3.8 | Consumption and usage variance: actual, theoretical, the report | 3.5, 3.7 |
| 3.9 | Report family completion + `verify_kitchen` | 3.6, 3.8 |
| 3.10 | Recipe imports, demo completion, hardening | 3.9 |
| 3.11 | Phase 3 exit gate | all |

The first stock-moving task is 3.5; the affected-domain suite runs there and
the complete project suite at the 3.5 and 3.9 boundaries and at 3.11.
**Exit:** tag `phase-3-kitchen-complete`. Not merged into `main`.

---

## 18. Proposed ADRs

Only two, and only because each records a policy that outlives its
implementation and that a future reader would otherwise reconstruct wrongly.

- **ADR-024 — Recipe versioning and the effective-dated cost basis.** Why
  versions are effective-dated and immutable once approved; how a date
  resolves a version and then a cost; what a snapshot is for; why no cost is
  ever stored on the recipe. (RCP-011 – RCP-016, RCP-023 – RCP-026.)
- **ADR-025 — Production batch valuation.** Value conservation through the
  batch; yield absorption into unit cost rather than variance postings; the
  per-account net journal and the legitimate no-journal case; the one-output
  rule and what multi-output would require; why there is no WIP account.
  (RCP-034 – RCP-037, §16 items 2 – 4.)

No ADR is proposed for effective dating as a mechanism (the supplier
catalogue settled the pattern), for account resolution (ADR-019), for source
identity (ADR-017), or for movement immutability (ADR-018). Restating a
decision in a second document is how two documents come to disagree.
