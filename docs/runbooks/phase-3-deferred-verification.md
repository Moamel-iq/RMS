# Phase 3 — verification deliberately deferred to Task 3.11

**Status:** open. Nothing on this page has been run for Tasks 3.5 – 3.9, and
nothing on it may be described as passing until Task 3.11 runs it.

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
| 12 | A complete `verify_kitchen` run | Task 3.9 composes it |
| 13 | `pre-commit run --all-files` | Hooks run per commit on changed files only |
| 14 | Full `mypy apps config tests` | Run per task on changed modules |
| 15 | Full `ruff check .` and `ruff format --check .` | As above |

## What Task 3.11 must do with this page

Run every row, then delete the row or mark it with the run that cleared it. A
row still open at the exit gate is a disclosed risk to be listed in the exit
report, not a thing to quietly drop.
