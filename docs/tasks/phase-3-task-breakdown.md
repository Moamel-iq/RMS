# Phase 3 — Recipes, Kitchen and Production: task breakdown and exit gates

Proposed 2026-08-16 by Task 3.0, **rewritten the same day by Task 3.0A** to
carry the structured steps, nested sub-recipes, servings, corrected consumption
and Release 1 production boundary that Task 3.0A added to the specification. The
governing principle is the same one both earlier phases proved: **nothing
depends on a figure until the figure is reconcilable.**

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

### Task 3.0 — Domain specification — **THIS TASK**

Specification (RCP-001 – RCP-116), 46 proposed invariants, this breakdown,
**three** proposed ADRs, ten diagrams and the blocking decision register. No
code, no models, no migrations.

Amended by **Task 3.0A**, which added the formal source audit (including the
`KhanMandiRecipe.xlsx` workbook the first pass never opened), structured recipe
steps, nested sub-recipes, servings, the worked scenarios, the profitability
boundary, the corrected consumption partition, and the Release 1 production
constraints.

**Exit:** `docs/tasks/task-3-0-recipes-production-domain-spec.md` approved, with
amendments recorded in the document, and every decision in its §22 register
marked *blocks Task 3.1* answered. **There are currently none**, so approval
alone releases 3.1.

---

### Task 3.1 — Recipe master

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

**Visible route required.** Depends on: 3.0 approval. No open `KD-*` blocks
this task.

**Exit criteria:** the recipe card renders lines, steps and servings in Arabic
RTL with cost columns omitted (not blanked) without `view_recipe_cost`; share
and serving constraints refuse at the database; the convention test passes; demo
recipes visible on the route.

---

### Task 3.2 — Recipe versions and approval

Effective dating with the exclusion constraint, maker-checker approval
(`approve_recipe_version`, never the author), supersession closing the prior
range in the same transaction, immutability of approved versions (whole-row
allowlist triggers), branch applicability, version resolution by date and
branch.

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

**Visible route required.** Depends on: 3.1, and KD-08 for the depth constant
(default 3).

**Exit criteria:** `A → B → C → A` and `A → A` both refused; depth beyond the
limit refused; a stocked recipe refused as a component and a non-stocked recipe
refused as a line item, at both the service and the importer's validation stage;
all four child objects immutable after approval.

---

### Task 3.3 — Recipe costing, plate cost, snapshots

Derived version cost and plate cost as posted-as-of reads; append-only cost
snapshots; cost columns gated by `view_recipe_cost`, omitted not blanked.
No new posting; no stored cost anywhere.

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

### Task 3.4 — Production batches: drafting

`ProductionBatch` / `ProductionBatchLine`, drafting from an approved version
scaled by multiplier, actual-quantity editing, substitutes offered, optional
lines omittable, one-warehouse rule, `view_production` /
`create_production_batch` permissions (warehouse-scoped), screens, draft
demo batch. Nothing posts.

Added by Task 3.0A:

- **Component-tree flattening at draft**: every leaf becomes a batch line with
  its scaled planned quantity, and no intermediate item, movement or WIP row is
  created (RCP-079).
- **`source_component_version` and `component_path`** on each flattened line, so
  a two-year-old batch reconstructs its exact tree (RCP-080).
- **Draft inertness proved**, not assumed: a draft reserves no stock, reduces no
  availability, and appears in no valuation or reorder read (RCP-096).

**Visible route required.** Depends on: 3.2.

**Exit criteria:** flattening produces exactly the leaf lines with correct paths
and scaled quantities; a stocked sub-recipe line is *not* expanded; drafting a
batch changes no balance anywhere.

---

### Task 3.5 — Production posting

The phase's kernel task: one atomic stock entry (`PRODUCTION_OUT` lines +
`PRODUCTION_IN` output valued by `inbound_value` = Σ consumed values), the
per-account net journal with the no-journal case, gapless numbering at
posting, lot creation writing `produced_by_*`, expired-ingredient refusal
(already the kernel's), reversal mirroring with availability checks,
`post_production_batch` / `reverse_production_batch`, real-COMMIT
concurrency tests, posted demo batch.

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

Depends on: 3.9.

**Data gate — this is the one to watch.** Task 3.10's **code** depends on 3.9
only. Its **acceptance as real recipe data** depends on **KD-02** (a filled and
signed `KM-RCP-004`), **KD-05** (whole/half shape per item) and **KD-06** (are
gram servings used at all). Until those are answered, everything this task loads
is `DEMO`-namespaced fiction and must be described as such — never as Khan
Mandi's recipes (RCP-058).

**Exit criteria:** a failed import applies zero rows; a second run creates no
duplicate; every import kind previews before it applies; the demo command is
idempotent and DEBUG-only.

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
