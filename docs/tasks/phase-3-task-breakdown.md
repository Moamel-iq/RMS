# Phase 3 — Recipes, Kitchen and Production: task breakdown and exit gates

Proposed 2026-08-16 by Task 3.0. The governing principle is the same one both
earlier phases proved: **nothing depends on a figure until the figure is
reconcilable.**

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

Specification, invariants, task breakdown, two proposed ADRs. No code, no
models, no migrations.

**Exit:** `docs/tasks/task-3-0-recipes-production-domain-spec.md` approved,
with amendments recorded in the document.

---

### Task 3.1 — Recipe master

`Recipe`, `RecipeLine`, `RecipeLineSubstitute` (draft versions only at this
point — the version model arrives complete in 3.2, so 3.1 may ship the
version row in DRAFT without the approval lifecycle), organization scoping,
`view_recipe` / `manage_recipe` / `view_recipe_cost` permissions, Arabic RTL
screens inside the shell, the recipe card, demo recipes.

**Visible route required.** Depends on: 3.0 approval.

---

### Task 3.2 — Recipe versions and approval

Effective dating with the exclusion constraint, maker-checker approval
(`approve_recipe_version`, never the author), supersession closing the prior
range in the same transaction, immutability of approved versions (whole-row
allowlist triggers), branch applicability, version resolution by date and
branch.

**Visible route required.** Depends on: 3.1.

---

### Task 3.3 — Recipe costing, plate cost, snapshots

Derived version cost and plate cost as posted-as-of reads; append-only cost
snapshots; cost columns gated by `view_recipe_cost`, omitted not blanked.
No new posting; no stored cost anywhere.

Depends on: 3.2.

---

### Task 3.4 — Production batches: drafting

`ProductionBatch` / `ProductionBatchLine`, drafting from an approved version
scaled by multiplier, actual-quantity editing, substitutes offered, optional
lines omittable, one-warehouse rule, `view_production` /
`create_production_batch` permissions (warehouse-scoped), screens, draft
demo batch. Nothing posts.

**Visible route required.** Depends on: 3.2.

---

### Task 3.5 — Production posting

The phase's kernel task: one atomic stock entry (`PRODUCTION_OUT` lines +
`PRODUCTION_IN` output valued by `inbound_value` = Σ consumed values), the
per-account net journal with the no-journal case, gapless numbering at
posting, lot creation writing `produced_by_*`, expired-ingredient refusal
(already the kernel's), reversal mirroring with availability checks,
`post_production_batch` / `reverse_production_batch`, real-COMMIT
concurrency tests, posted demo batch.

**First task in Phase 3 that moves stock. Certification boundary: run the
complete project suite.** Depends on: 3.4.

---

### Task 3.6 — Yield, loss and batch variance

The yield report (expected vs actual output, per batch and per version), the
batch variance report (planned vs consumed per line, cost consequence),
informational rates displayed beside measured reality. Reads only.

Depends on: 3.5.

---

### Task 3.7 — Staff and complimentary meals

`MealRecord`: version resolution frozen at record time, cancellation not
edit, `record_meal` (branch-scoped), screens, meal log, demo meals. No stock,
no journal (RCP-043; the reclassification stays deferred per RCP-044).

**Visible route required.** Depends on: 3.2.

---

### Task 3.8 — Consumption and usage variance

Actual consumption (the charter's formula over a selected kitchen warehouse),
theoretical consumption over the recordable quantity sources (batch plans,
meal records) with the Phase 4 socket left visible, the usage variance report
with its coverage labelled honestly.

Depends on: 3.5, 3.7.

---

### Task 3.9 — Reports and `verify_kitchen`

The report family completed under the Phase 1 contract (list in spec §12),
`verify_kitchen` composed into the standing verification suite: per-batch
stock/journal agreement, value conservation to the fils, source-identity
uniqueness, and `verify_inventory_against_gl` clean with production included.

**Run the complete project suite at this boundary.** Depends on: 3.6, 3.8.

---

### Task 3.10 — Imports, demo completion and hardening

Preview-first recipe imports (master and lines) on the Task 1.7 framework —
registered from `apps.kitchen` exactly as procurement registered its kinds;
no import posts anything. Cross-tenant and concurrency sweeps, admin
lockdown, export formula protection (inherited), the complete demo command,
the visible route matrix, HTMX verification.

Depends on: 3.9.

---

### Task 3.11 — Phase 3 exit gate

All kitchen invariants verified against cited tests. Value conservation on
every posted batch. Version resolution correct at every date boundary.
`verify_kitchen` and `verify_inventory_against_gl` clean on a fresh
PostgreSQL database migrated from zero and seeded twice. Demo visible on
every route. Complete suite, all quality gates, zero pending migrations,
traceability citing real tests.

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
