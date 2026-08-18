# Phase 3 — verification deliberately deferred to Task 3.11

**Status:** open, and **complete through Task 3.9**. Nothing on this page has
been run for Tasks 3.5 – 3.9, and nothing on it may be described as passing
until Task 3.11 runs it.

Rows 1 – 17 were recorded by Tasks 3.5 – 3.7. Rows 18 – 32 were added by Task
3.8. The "known gaps" section at the end records scope decisions rather than
skipped checks, so that Task 3.11 does not mistake one for the other.

## Why this page exists

The owner set an accelerated implementation policy for Tasks 3.5 – 3.9:
production code first, one definitive verification pass at the Phase 3 exit
gate rather than a full campaign per task. That is a deliberate trade and this
page is the other half of it — the record of what was *not* checked, written
down at the moment it was skipped rather than reconstructed afterwards.

A deferral nobody wrote down is indistinguishable from an oversight. This page
is what makes the difference visible.

## What each task did run

Per task, only the fast checks:

- `manage.py check`
- `manage.py makemigrations --check --dry-run`
- `ruff check` and `ruff format --check` on the paths that task changed
- `mypy` on the application modules that task changed
- a route or service smoke check, to prove the new code loads and executes
- a focused test **only** where a posting path, a trigger or a specific defect
  needed direct proof

The full pre-commit hook set runs on every commit except the preservation
checkpoint, which used `--no-verify` once so that the approved Inventory UI was
not reformatted at the moment it was being preserved.

## What is deferred to Task 3.11

| # | Deferred | Why it matters |
|---|---|---|
| 1 | The complete Kitchen suite | The last full run was 872 passed / 4 failed, and those four were fixed afterwards without a full re-run |
| 2 | The complete project suite | Last green at 3175 tests on the Task 3.4 tree; Tasks 3.5 – 3.9 have not been measured against it |
| 3 | Fresh PostgreSQL database migrated from zero | Migrations 0017 – 0020 have been applied incrementally, never from nothing |
| 4 | All production-posting real-COMMIT races | Written and green in isolation at Task 3.5; not re-run since the surface was wired |
| 5 | All reversal races | As above |
| 6 | Meal concurrency | Task 3.7 |
| 7 | `BatchDocumentLink` attribution concurrency | Task 3.8 |
| 8 | The report and CSV security matrix | Redaction is structural, but structural is a claim until it is tested |
| 9 | Every HTMX / full-page parity check | Each report answers both; only some pairs have been exercised |
| 10 | The Demo double-run proof | The Task 3.5 demo seeds postings and a reversal; idempotency is designed, not proved |
| 11 | Inventory-to-GL reconciliation with production included | The construction satisfies it; the verifier proves construction met reality |
| 12 | The complete Kitchen test suite | Task 3.9 built `verify_kitchen`; the suite behind it has not been run since Task 3.4 |
| 13 | The complete project suite | Not run under the accelerated policy |
| 14 | A fresh migration from zero | Every migration since 0017 has only ever been applied incrementally |
| 15 | `pre-commit run --all-files` | Hooks run per commit on changed files only |
| 16 | Full `mypy apps config tests` | Run per task on changed modules |
| 17 | Full `ruff check .` and `ruff format --check .` | As above |

## Added by Task 3.8

| # | Not run | Why it matters |
|---|---|---|
| 18 | Every `BatchDocumentLink` constraint, as tests | Five check constraints and two partial unique indexes. The five guards were proved by direct probe on the development database (service refusal, deferred trigger with the service bypassed, `ACTIVE` immutability, delete refusal, cancellation releasing the quantity) — but a probe is one path, not a matrix |
| 19 | The **movement-partition classification matrix** | Seventeen buckets against fifteen `MovementType` values, including both `WASTE` splits, both `MANUAL_ADJUSTMENT` splits, and a `REVERSAL` of each. Only the buckets the demo happens to produce have been exercised: 15 of 17 appeared, and `PRODUCTION_OUTPUT`/`COUNT_GAIN` combinations remain unseen |
| 20 | `classify_kitchen_movement` raising on an unknown type | The refusal is the design — a new `MovementType` must be classified explicitly rather than defaulted. Nothing yet asserts the raise |
| 21 | The actual-consumption equations, as tests | `Σ positive actuals = Σ \|PRODUCTION_OUT\|` per item and `Σ movement values = input_value = output_value` both hold on the demo batch. One batch is not the equation |
| 22 | The stock identity at scale | It holds across 15 `(warehouse, item)` rows today. It is the partition's only proof and deserves a property test over generated movement sets |
| 23 | The theoretical **sales** adapter | Cannot be written until Phase 4 exists. What can be certified now is that `SALES` reports `DEFERRED_TO_PHASE_4` and that no surface claims finality |
| 24 | The meal-equivalent deduplication policy | Task 3.8 refuses to combine production plans with meal expansions because no portion-to-batch key exists. If Phase 4 introduces one, this decision must be revisited — and until then the refusal itself should be a test |
| 25 | Report security, as tests | Money is structurally omitted rather than blanked; verified by one 403 and one absent column on the development database, not by a permission matrix |
| 26 | API security, as tests | Seven new endpoints. Scope resolution is 404 and missing authority is 403 by construction; neither is asserted |
| 27 | The HTMX / full-page matrix | All 14 report routes answered both with a smaller fragment and no nested shell. That is a smoke, not a parity proof over filter and pagination combinations |
| 28 | CSV formula safety | `_safe` prefixes the five leading characters, and the coverage rows go through it too. Untested since Task 3.6 |
| 29 | Demo idempotency, as a test | Proved by running the seed twice and comparing ten counts. Not automated |
| 30 | `verify_kitchen` **end to end** | The severity contract is now proved by `apps/kitchen/tests/test_verify_kitchen_severity.py`: ERROR fails, ADVISORY and COVERAGE_LIMITATION do not, an unclassifiable movement and a broken stock identity both become ERROR findings. What remains for 3.11 is a planted defect in *each of the ten sections*, driven through the real command against a real database |
| 31 | Every reconciliation command, together | The four Kitchen and three Inventory verifiers are composed by `verify_kitchen`; each has been run, but not as one gate on a fresh database |
| 32 | All concurrency cases | Attribution cap under two concurrent writers, meal recording, posting and reversal races |

## Known gaps recorded rather than fixed

These are **not** deferred verification — they are scope decisions written down
so Task 3.11 does not mistake them for oversights:

- **Task 3.7's meal API routes were never built.** `GET/POST
  /api/v1/kitchen/meals/`, `GET /api/v1/kitchen/meals/{id}/` and
  `POST /api/v1/kitchen/meals/{id}/cancel/` are specified and absent; the HTML
  surface is complete. Task 3.8 added its own seven endpoints, so the API is
  otherwise current.
- **Task 3.7's documentation updates were not made** to the Task 3.0 spec, the
  phase-3 breakdown, the invariants list or the traceability matrix. Task 3.9
  updates the breakdown and this manifest; the spec's §11.2 formula is
  superseded by ADR-026 rather than edited in place.
- **`SUPPLIER_RETURN_OUT` and `TRANSIT_SHORTAGE_LOSS` are no longer public
  buckets.** Task 3.8 first added them beyond the approved fifteen; they are now
  internal subcategories under `ECONOMIC_RETURN_OR_REVERSAL` and
  `CUSTODY_TRANSFER_OUT` respectively, and `MovementBucket` is back to fifteen
  (ADR-026 §2.1). What Task 3.11 should certify is the **netting**: a supplier
  return must reduce supply and not consumption, which the subcategory split is
  what makes true.

## What Task 3.11 must do with this page

Run every row, then delete the row or mark it with the run that cleared it. A
row still open at the exit gate is a disclosed risk to be listed in the exit
report, not a thing to quietly drop.
