# Phase 3 — Recipes, Kitchen and Production: task breakdown and exit gates

Proposed 2026-08-16 by Task 3.0, **rewritten the same day by Task 3.0A** to
carry the structured steps, nested sub-recipes, servings, corrected consumption
and Release 1 production boundary that Task 3.0A added to the specification. The
governing principle is the same one both earlier phases proved: **nothing
depends on a figure until the figure is reconcilable.**

**Amended again by Task 3.0B**, after the owner supplied the Arabic recipe book
and two plate-card decks. Task 3.1 gains provenance and measurement basis, Task
3.10 gains the ten-condition data gate and the conflict report, and the data
gate's open decisions change: KD-05 and KD-06 are **closed by source**, while
KD-19 and KD-20 are new.

Task numbering and dependency order are unchanged; what each task owns has
grown. Where a task depends on an owner decision from the specification's
register (§22), the decision id is named in its **Depends on** line — an open
`KD-*` there is a real gate, not a note.

## Why this shape

**The recipe before the batch.** A batch is drafted from an approved recipe
version; a version is approved on a recipe. Master data before documents, the
Phase 2 ordering applied again.

**Costing before production, because costing is a read.** Recipe costing
(3.3) needs nothing but approved versions and the Phase 1 averages, so it
lands before any stock moves — and gives the batch screens a cost to display
from their first day.

**Posting in its own task, after drafting.** Drafting a batch (3.4) consumes
nothing and can ship with screens and tests before the posting task (3.5)
touches the kernel. 3.5 is the phase's first stock-moving task and its first
certification boundary.

**Meals are independent of production.** Meal records (3.7) depend on
versions, not on batches — a staff meal of a plated dish references a portion
recipe no batch will ever produce. They join production only at the variance
report (3.8).

**The variance last among the reads, the verifier after everything.** Usage
variance (3.8) needs actual consumption (3.5's movements plus Phase 1's) and
theoretical consumption (3.7's records plus 3.4's plans); `verify_kitchen`
(3.9) proves the whole set against the ledgers.

## The tasks

### Task 3.0 — Domain specification — **APPROVED 2026-08-16**

Specification (RCP-001 – RCP-126), 52 proposed invariants, this breakdown,
**three** proposed ADRs, ten diagrams and the decision register. No code, no
models, no migrations.

Amended by **Task 3.0A**, which added the formal source audit (including the
`KhanMandiRecipe.xlsx` workbook the first pass never opened), structured recipe
steps, nested sub-recipes, servings, the worked scenarios, the profitability
boundary, the corrected consumption partition, and the Release 1 production
constraints. Amended again by **Task 3.0B**, which audited the Arabic recipe
book and the two plate-card decks page by page, closed KD-03, KD-05 and KD-06
against those sources, and added provenance, measurement basis and the two-layer
serving rule.

**Exit: MET.** The owner approved the specification on 2026-08-16 and answered
all six open decisions (spec §22.1). The register now shows **zero** rows
requiring an owner decision. Task 3.1 is released.

The approved Release 1 decisions that bind later tasks: real recipes may be
captured as **DRAFT** but not approved until `KM-RCP-004` is complete (KD-02,
binding **3.2**); no `KitchenStation` and a nullable station (KD-07, binding
**3.1**); atomic same-date one-warehouse production (KD-09, releasing **3.5**);
prices and margins out of Phase 3 (KD-13); no unsourced mass-to-volume
conversion (KD-19, binding **3.10**); undocumented prepared mixes stay
unapproved drafts (KD-20, binding **3.10**). Depth 3, one primary output and
`shelf_life_days` from the batch business date were approved as recommended.

---

### Task 3.1 — Recipe master — **COMPLETE**

`Recipe`, `RecipeLine`, `RecipeLineSubstitute` (draft versions only at this
point — the version model arrives complete in 3.2, so 3.1 may ship the
version row in DRAFT without the approval lifecycle), organization scoping,
`view_recipe` / `manage_recipe` / `view_recipe_cost` permissions, Arabic RTL
screens inside the shell, the recipe card, demo recipes.

Added by Task 3.0A:

- **Line fields the workbook proves are needed** — `measured_quantity`
  alongside the approved `quantity`, per-line `loss_rate`, and `cost_class`
  (`FOOD` / `PACKAGING` / `ACCOMPANIMENT`) — RCP-060 – RCP-062.
- **`RecipeStep` and `RecipeStepIngredient`** with the step editor and the
  method section of the recipe card, shares constrained, duration and
  temperature left null (RCP-063 – RCP-069).
- **`RecipeServing`** with the primary-serving index and the unit-dimension
  check at entry (RCP-082 – RCP-085, RCP-091).
- **The no-hard-coding convention test** over `apps/kitchen/**.py` (RCP-082) —
  written in 3.1 so it guards every later task.

`KitchenStation` is created **only if KD-07 is answered yes**; the default
leaves `station` null and the table unbuilt.

Added by Task 3.0B, once the recipe book and plate cards made real figures
possible:

- **Provenance on every row** — `source_document` and `source_page`, both set
  or both null, on lines, steps and servings (RCP-119, invariant 47).
- **`measurement_basis`** on every quantity — `RAW` / `PREPARED` / `COOKED` /
  `PLATED` — because the book's 350 g is carved cooked meat and the cards'
  500 g is a raw piece, and nothing may aggregate across the two (RCP-120,
  invariant 48).
- **The no-cross-plate-derivation convention test** (RCP-124, invariant 51),
  written here beside the RCP-082 test so both guard every later task.
- **Serving rows may now carry sourced quantities** — 0.500 حبة, 0.350 KG,
  0.500 KG — each citing its page. They are still **data**, never constants in
  code.

**Visible route required.** Depends on: 3.0 approval (given 2026-08-16). No
open `KD-*` blocked this task.

**Delivered.** `apps.kitchen` with nine models and two migrations; `Recipe`,
`RecipeCategory`, `RecipeBranch`, a DRAFT-only `RecipeVersion`, `RecipeLine`,
`RecipeLineSubstitute`, `RecipeStep`, `RecipeStepIngredient` and
`RecipeServing`; twenty-five services; the three permissions and their role
map; the command API; Arabic RTL screens; read-only admin; `seed_kitchen_demo`
with five recipes; 126 tests. **Zero stock movements and zero journal entries**,
proved by counting rather than asserted. `station` is **not** a field —
`KitchenStation` is not created (KD-07) and §5A.2 rejected a free-text station
string, so the column arrives only if the owner revisits KD-07.

**Exit criteria:** the recipe card renders lines, steps and servings in Arabic
RTL with cost columns omitted (not blanked) without `view_recipe_cost`; share
and serving constraints refuse at the database; both convention tests pass; a
row with one of `source_document` / `source_page` set is refused at the
database; two quantities of different `measurement_basis` refuse to aggregate;
demo recipes visible on the route and clearly fictional (RCP-126).

---

### Task 3.2 — Recipe versions and approval

**Split into 3.2A and 3.2B during implementation.** This is an implementation
checkpoint split only: the dependency order is unchanged, Task 3.3 still depends
on the whole of 3.2, and no scope was removed. The split exists because the two
halves have different shapes — the approval boundary is a set of rules that must
arrive together, and the component graph is a recursive structure that can be
built on top of a finished boundary but not beside a half-built one.

Effective dating with the exclusion constraint, maker-checker approval
(`approve_recipe_version`, never the author), supersession closing the prior
range in the same transaction, immutability of approved versions (whole-row
allowlist triggers), branch applicability, version resolution by date and
branch.

#### Task 3.2A — lifecycle, approval, effective dating and immutability

**Delivered.** A six-state lifecycle (`DRAFT`, `SUBMITTED`, `APPROVED`,
`ACTIVE`, `REJECTED`, `SUPERSEDED`) with no `EXPIRED` — expiry is derived from
the range, never stored; `RecipeVersionReview` carrying `KM-RCP-004`'s four
signatures, append-only; `RecipeVersionBranchScope` with organization-wide
activation **materialised** per branch so `EXCLUDE USING gist` can enforce it;
`resolve_recipe_version(recipe, branch, on_date)` with a required business date
and two stable error codes; supersession closing the predecessor at the day
before the replacement begins, in one transaction; whole-row allowlist triggers
across all eight owned tables; five new permissions; command API; twelve Arabic
RTL screens; deterministic version comparison on business keys;
`verify_recipe_versions`; nine demo recipes covering every lifecycle state.
**Zero stock movements and zero journal entries**, proved by counting.
`ADR-024` written and accepted.

**Exit criteria:** every range boundary resolves correctly, including the final
included day; two overlapping activations cannot both commit at real COMMIT; a
raw `UPDATE` and a raw `DELETE` are refused on the version and on every owned
child row; the author, the submitter and every reviewer are each refused the
final approval; a real recipe cannot be approved on demo evidence and a demo
recipe cannot claim a signed form; a superseded version still resolves for its
own dates.

#### Task 3.2B — the nested recipe graph

**Complete.** Delivered `RecipeComponent`, the mutual-exclusion rule, cycle and
depth bounds, activation-time effective-coverage validation, whole-row component
immutability, the component API and ten Arabic RTL screens. See specification
§26 for what building it settled.

A first implementation additionally required a child to cover the parent's whole
future range and blocked child supersession while active parents referenced it.
Both were withdrawn by owner policy: the exact child-version FK is frozen and
stays valid after the child is superseded for new selection (§26.4).

**With 3.2B, Task 3.2 is complete.**

Added by Task 3.0A:

- **`RecipeComponent`** — non-stocked sub-recipes referencing an exact approved
  child version (RCP-070 – RCP-075, RCP-081).
- **The mutual-exclusion constraint** that makes double counting
  unrepresentable: a recipe with an `output_item` is referenceable only as a
  line, one without only as a component (RCP-070).
- **Cycle and depth validation** on every draft save and again at approval,
  with named errors and a bounded walk (RCP-076, RCP-077).
- **Effective-date and branch compatibility** checks against every child
  (RCP-074, RCP-075).
- **Freezing extended**: approval freezes lines, steps, servings **and**
  components together, one allowlist trigger family (RCP-064).

**Visible route required.** Depends on: 3.2A, and KD-08 for the depth constant
(default 3).

**Exit criteria:** `A → B → C → A` and `A → A` both refused; depth beyond the
limit refused; a stocked recipe refused as a component and a non-stocked recipe
refused as a line item, at both the service and the importer's validation stage;
all four child objects immutable after approval — the fourth joins the trigger
family Task 3.2A already built, rather than needing its own.

---

### Task 3.3 — Recipe costing, plate cost, snapshots — **BUILT** (2026-08-17)

Derived version cost as a posted-as-of read; append-only cost snapshots; cost
columns gated by `view_recipe_cost`, omitted not blanked. No new posting; no
stored cost anywhere.

**Built:** `apps/kitchen/costing.py` (the engine), `apps/kitchen/snapshots.py`
(the one write), `apps/kitchen/cost_reconciliation.py` + the
`verify_recipe_cost_snapshots` command, `apps/inventory/valuation.py` (a
read-only bulk valuation query — the only Inventory change), migrations 0008 and
0009, six API routes, five Arabic RTL screens, and one promoted navigation entry.

**Plate cost is built here**, from the version's primary `RecipeServing`: no
model carries a `portions_per_batch` column, and the primary serving holds the
same fact exactly with RCP-084 guaranteeing exactly one. `plate_cost` equals
that serving's own standard rate, and both are frozen into every snapshot.

**Every serving definition receives an exact allocation, at any count.** The
distribution is analytic and stored in five numbers, so 50,000 portions cost
what 10 cost and reconstruct exactly. Screens may cap how many example rows they
list; the calculation is never capped.

**What genuinely remains for Task 3.5:** the exact allocation of a *posted*
`ProductionBatch`'s actual cost across the servings it really produced. That is
a different figure from the standard plate cost, and it needs production to
exist. See spec §27.11.

Added by Task 3.0A:

- **Recursive component roll-up**, quantized once at the top and nowhere in
  between (RCP-078).
- **Cost per serving** as a 6 dp rate, and the **exact-remainder allocation** of
  a real batch cost across produced servings through
  `apps/core/allocation.allocate` (RCP-086, RCP-087).
- **The food / packaging / accompaniment split**, reproducing `KM-RCP-004`'s own
  summary, with a component's cost distributed across the classes of its own
  lines (RCP-092).
- The cost-ratio column rendered as "—" with its reason, never as zero
  (RCP-093).

Depends on: 3.2.

**Exit criteria:** a hand-computed three-level tree matches to the fils; the
serving allocation of a deliberately awkward split sums exactly to the batch
cost; the class split reconciles to the total; no cost is stored anywhere.

---

### Task 3.4 — Production batches: drafting — **Done**

`ProductionBatch` / `ProductionBatchLine`, drafting from an approved version
scaled by multiplier, actual-quantity editing, substitutes offered, optional
lines omittable, one-warehouse rule, `view_production` /
`create_production_batch` permissions (warehouse-scoped), screens, draft
demo batch. Nothing posts.

**Who owns what, stated because earlier notes in this file and in the code got
it wrong.** `ProductionBatch` is **Task 3.4's**, not 3.5's:

| Task 3.4 owns | Task 3.5 owns |
| --- | --- |
| the `DRAFT` batch | the document number |
| flattened requirements and their exact paths | lots and locations |
| scaling and reset-and-rescale | availability and reservation |
| actual consumption rows | valuation of what was consumed |
| approved substitution | Inventory posting |
| the actual output figure | GL posting |
| readiness (derived, never stored) | `POSTED` and `REVERSED` |
| discard | reversal |

Three tables rather than the two the spec sketched. `ProductionBatchActualLine`
is normalized out of the sketched `consumed_quantity` column, because a partial
substitution — 3 kg of the primary plus 1 kg of an approved stand-in — is two
facts about one requirement and a single column can hold only one of them. The
departure is recorded in the domain spec's section 28.

**As built, beyond the original list:**

- **One shared expansion engine.** `apps/kitchen/expansion.py` is the only walk
  of the component graph; Task 3.3's costing was moved onto it in this task, so
  a card and a requirement list cannot disagree about what a recipe contains.
- **A deferred rescale-consistency trigger** (migration 0015). The multiplier is
  revisable while a batch is a draft, so `multiplier`,
  `expected_output_quantity` and every `planned_base_quantity` are checked
  against each other at COMMIT. Revisable is not independently mutable.
- **Cross-dimensional consumption is never aggregated.** A requirement met with
  an approved stand-in in another dimension reports its rows separately and says
  *not quantitatively comparable* rather than printing a sum. Task 3.5 values
  each row separately; nothing here invents a physical conversion ratio.

Added by Task 3.0A:

- **Component-tree flattening at draft**: every leaf becomes a batch line with
  its scaled planned quantity, and no intermediate item, movement or WIP row is
  created (RCP-079).
- **`source_component_version` and `component_path`** on each flattened line, so
  a two-year-old batch reconstructs its exact tree (RCP-080).
- **Draft inertness proved**, not assumed: a draft reserves no stock, reduces no
  availability, and appears in no valuation or reorder read (RCP-096).

**Visible route required.** Depends on: 3.2. One previously inert Kitchen
navigation entry — `أوامر الإنتاج` — was promoted, and nothing else.

**Exit criteria, all met:** flattening produces exactly the leaf lines with
correct paths and scaled quantities; a stocked sub-recipe line is *not*
expanded; drafting a batch changes no balance anywhere. Proved by a census
taken before and after the whole scenario — create, edit, substitute, rescale,
reset, record an output, verify, discard — over `StockMovement`,
`StockLedgerEntry`, `StockBalance` (values, not counts), `StockLocationBalance`,
`JournalEntry`, `JournalLine`, `InventoryLot`, `RecipeCostSnapshot` and posted
batches.

---

### Task 3.5 — Production posting

The phase's kernel task: one atomic stock entry (`PRODUCTION_OUT` lines +
`PRODUCTION_IN` output valued by `inbound_value` = Σ consumed values), the
per-account net journal with the no-journal case, gapless numbering at
posting, lot creation writing `produced_by_*`, expired-ingredient refusal
(already the kernel's), reversal mirroring with availability checks,
`post_production_batch` / `reverse_production_batch`, real-COMMIT
concurrency tests, posted demo batch.

**What 3.4 left standing for this task to remove, deliberately and by name:**

- the check constraint `production_batch_is_draft_only_until_task_3_5`, which
  refuses any status but `DRAFT`. It is named after the task that must delete
  it, so nobody removes it while tidying;
- `production_batch_draft_has_no_number`, which survives that deletion: a
  `POSTED` batch may carry a number, a `DRAFT` may never;
- migration 0011's freeze triggers, which already permit `status` and `number`
  to move so that 3.5 does not have to rewrite them.

**And one thing 3.5 must value rather than convert.** A requirement met with an
approved stand-in in another dimension has consumption in both, and 3.4
deliberately never adds them. Each row is valued separately at posting; there is
no conversion ratio between kilograms and litres to find, and inventing one
would post a figure no scale produced.

Added by Task 3.0A:

- **The Release 1 boundary enforced, not assumed** — the seven conditions of
  RCP-094, with **named refusals** for multi-day and partial-completion attempts
  rather than a silent approximation (RCP-095).
- **Three explicit no-journal tests** (RCP-113): a zero-net batch writes *no*
  `JournalEntry`; a differing-account batch writes one netted, balanced entry
  with full source identity; and the stock ledger entry carries the identity in
  **both** cases.

**First task in Phase 3 that moves stock. Certification boundary: run the
complete project suite.** Depends on: 3.4, and **KD-09** — a YES answer blocks
this task until WIP custody, WIP accounting, issue/completion events and
partial-completion arithmetic are specified and approved (RCP-097). The default
is NO, and 3.1 – 3.4 proceed regardless.

**Exit criteria:** value conservation to the fils on every posted batch;
`verify_inventory_against_gl` clean with production movements included; a
multi-day attempt refused with its named error; reversal mirrors exactly and
refuses when the output has since been consumed.

---

### Task 3.6 — Yield, loss and batch variance

The yield report (expected vs actual output, per batch and per version), the
batch variance report (planned vs consumed per line, cost consequence),
informational rates displayed beside measured reality. Reads only.

Added by Task 3.0A: **declared line loss beside observed line loss** (RCP-060),
and **batch variance grouped by `component_path`**, so "was the overspend in the
dish or in the blend?" has an answer (RCP-080).

Depends on: 3.5.

**Exit criteria:** a batch whose yield differs from expectation shows the
difference on the report and **nowhere in the ledger**; grouping by component
reconciles to the ungrouped total.

---

### Task 3.7 — Staff and complimentary meals

`MealRecord`: version resolution frozen at record time, cancellation not
edit, `record_meal` (branch-scoped), screens, meal log, demo meals. No stock,
no journal (RCP-043; the reclassification stays deferred per RCP-044).

Added by Task 3.0A: **every meal surface — screen, report and CSV — carries
RCP-108's statement in words**, that the record explains consumption and is *not*
sufficient for employee-benefit or promotional-expense reporting. A quantities-
only report in a system that has costs is otherwise read as "these cost
nothing".

**Visible route required.** Depends on: 3.2.

**Exit criteria:** a recorded meal moves no stock and writes no journal, proved
by assertion on both ledgers; the statement is present on all three surfaces;
cancelled meals are excluded from every variance read.

---

### Task 3.8 — Consumption and usage variance

Theoretical consumption over the recordable quantity sources (batch plans, meal
records) with the Phase 4 socket left visible, and the usage variance report
with its coverage labelled honestly.

**Rewritten by Task 3.0A.** Actual consumption is no longer "the charter's
formula" — that formula double-counts against this system's documents (spec
§11.1, ADR-026). This task delivers **two** reads:

- **Batch actual consumption** (§11.2): consumed quantities, less linked
  material returns, plus linked waste.
- **Kitchen warehouse flow** (§11.3): an **exhaustive partition** of the
  warehouse's posted movements — custody, supply, production use, non-production
  use, loss, corrections — in which every movement lands in exactly one bucket,
  transfers are custody rather than consumption, and finished-output waste is
  kept out of ingredient consumption.
- **`BatchDocumentLink`** (RCP-100 – RCP-102): kitchen-owned, holding keys into
  inventory, mutating nothing, with attribution capped at the source line under
  `select_for_update`.

Depends on: 3.5, 3.7.

**Exit criteria:** the stock identity of RCP-104 reconciles exactly to the Phase
1 warehouse balance for every sampled warehouse and period; a transfer into the
kitchen appears in the custody column and in **no** consumption figure; over-
attribution across two batches is refused; `apps.inventory` still contains no
reference to `apps.kitchen`.

---

### Task 3.9 — Reports and `verify_kitchen`

The report family completed under the Phase 1 contract (list in spec §12),
`verify_kitchen` composed into the standing verification suite with **eight**
proofs: (1) per-batch stock/journal agreement, (2) value conservation to the
fils, (3) source-identity uniqueness, (4) `verify_inventory_against_gl` clean
with production included, and — added by Task 3.0A — (5) **every journal-less
batch's per-account nets recomputed and proved zero**, (6) no over-attribution
on any linked inventory line, (7) the warehouse partition identity reconciling,
(8) no orphan links.

Proof (5) is the one that matters most: a journal that is correctly silent and a
journal that is wrongly missing look identical from the outside, and only the
recomputation tells them apart.

**Run the complete project suite at this boundary.** Depends on: 3.6, 3.8.

**Exit criteria:** every proof runs against planted defects and fails on each;
the verifier reports and never repairs.

---

### Task 3.10 — Imports, demo completion and hardening

Preview-first recipe imports on the Task 1.7 framework — registered from
`apps.kitchen` exactly as procurement registered its kinds; no import posts
anything. Cross-tenant and concurrency sweeps, admin lockdown, export formula
protection (inherited), the complete demo command, the visible route matrix,
HTMX verification.

Added by Task 3.0A: the import surface covers **lines, steps, components and
servings** as well as recipe masters, with each kind's columns listed in spec
§5A.3, §5B.2 and §5C.3; a component row naming a stocked recipe is refused at
**validation**, so the preview shows the refusal rather than the apply failing
(RCP-070). Recipe media stays a **reference**, not an upload — actual file
storage would mean the Task 1.7 file-upload security rules, and is deferred with
those rules cited (§5A.2).

Added by Task 3.0B: the import surface must also carry **provenance**
(`source_document`, `source_page`) on every row, retain **both sides of every
conflict** rather than reconciling them (RCP-121, invariant 49), and write
**`DRAFT` only** — no import path may produce an `APPROVED` version (RCP-118,
invariant 50). A **conflict report** lists the eight known disagreements of spec
§24.6 side by side for the chef and accountant.

Depends on: 3.9.

**Data gate — this is the one to watch.** Task 3.10's **code** depends on 3.9
only. Its **acceptance as real recipe data** depends on the ten conditions of
RCP-125, of which the still-open owner decisions are **KD-02** (a filled and
signed `KM-RCP-004`), **KD-19** (what converts a sauce كاسة to the plate's
grams) and **KD-20** (who documents the five appetizer `خلطة` recipes). KD-05
and KD-06 were **closed by Task 3.0B** against the recipe book and plate cards
and no longer gate anything. Until the ten conditions hold, everything this task
loads is `DEMO`-namespaced fiction and must be described as such — never as Khan
Mandi's recipes (RCP-058, RCP-126).

Note what the gate does **not** say: the recipe book being sourced does not make
the *costing* layer sourced. The book has no costs, no signatures and no
effective dates, so KD-02 survives Task 3.0B untouched.

**Exit criteria:** a failed import applies zero rows; a second run creates no
duplicate; every import kind previews before it applies; no import can produce
an approved version; a planted pair of conflicting source rows both land and
appear on the conflict report; every imported row carries its document and page;
the demo command is idempotent, DEBUG-only and fictional.

---

### Task 3.11 — Phase 3 exit gate

All **46** kitchen invariants verified against cited tests. Value conservation
on every posted batch. Version resolution correct at every date boundary.
`verify_kitchen` and `verify_inventory_against_gl` clean on a fresh
PostgreSQL database migrated from zero and seeded twice. Demo visible on
every route. Complete suite, all quality gates, zero pending migrations,
traceability citing real tests.

Added by Task 3.0A, as explicit gate items:

- **Every `RCP-*` row in traceability cites a real test**, or the exit records
  why not. RCP-001 – RCP-116 all sit at `Specified` today.
- **The consumption partition reconciles** on the fresh database, not only in
  unit tests (RCP-104).
- **No journal-less batch is unexplained** — the verifier's proof (5) runs over
  the whole seeded set.
- **The `KD-*` register is re-read at the gate**: any decision still open is
  listed in the exit report with what it affects. An unanswered question is a
  disclosed risk, not a silent one.

**Exit:** tag `phase-3-kitchen-complete`. Not merged into `main`.

## Exit gates, restated

A task is not complete until:

- focused tests pass;
- affected-domain tests pass;
- security and concurrency tests pass where applicable;
- ruff, ruff format, mypy, `manage.py check` and `makemigrations --check` pass;
- reconciliation is clean where applicable;
- demo data exists and the rendered route was actually opened;
- the work is committed and the branch pushed;
- unresolved errors are zero.

A full-project suite runs at the 3.5 and 3.9 boundaries, at 3.11, and
whenever a change reaches the inventory or accounting kernel — not after
every small step.
