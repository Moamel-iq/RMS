# Khan Mandi RMS — Claude Code Instructions

## Authoritative sources

Read these before any architectural or business decision:

1. `docs/requirements/SRS.md`
2. `docs/architecture/architecture-charter.md`
3. `docs/decisions/`
4. The current module specification in `docs/specs/`

When sources conflict, stop implementation, identify the conflict, and report
it. Do not silently invent a business rule.

The approved environment plan is
`docs/plans/installation-to-coding-start-plan.txt`. Where the other planning
documents in `docs/plans/` disagree with it (WSL2, uv, Python 3.13, day-one
Docker/Celery), the installation plan wins. See ADR-010.

## Stack

- Python 3.14, Django 5.2 LTS, PostgreSQL 18
- Django Ninja for the API
- Django templates + htmx 2.0.4 (vendored, no CDN, no Node) — ADR-011
- pytest-django, factory_boy, Hypothesis
- Ruff and mypy
- venv + pip-tools for dependencies
- Windows development through PyCharm

## Frontend rules

- Arabic is the source language: message IDs are the Arabic strings. English
  is a translation target. `gettext` is not installed, so `compilemessages`
  cannot run yet.
- CSS logical properties only (`padding-inline-start`, `text-align: start`).
  Never `left`/`right` on the inline axis — one stylesheet serves RTL and LTR.
- Never put `dir="auto"` on an empty input: it resolves to LTR and puts the
  padding on the wrong side of a right-to-left field.
- htmx views return the fragment with HTTP 200 on validation failure (htmx
  does not swap error responses) and `HX-Redirect` on success.
- Vendored JS is upgraded deliberately, never by a transitive bump.

## Architecture rules

- Thin API/views.
- Commands and state changes live in explicit service functions.
- Complex reads live in selectors/query services.
- Posted inventory and accounting effects are append-only.
- Corrections use reversal and replacement, never edit or delete.
- Use Decimal, never float, for money, quantities, rates, unit costs,
  percentages, and conversions.
- Use moving weighted-average inventory costing.
- Reject negative stock during normal posting.
- Use `transaction.atomic()` for complete posting operations.
- Use idempotency keys for operations that may be retried.
- Never hide essential posting logic in signals or `Model.save()`.
- Do not add a Branch FK to every model automatically.
- Enforce organization/branch access in services and queries.
- Use `Asia/Baghdad`, and a separate branch business date. Never derive the
  business date as `date(timestamp)`.
- Preserve Arabic text and RTL requirements.
- Every money- or stock-touching function requires tests.
- Prefer database constraints for enforceable invariants.

## Safety rules

Never do any of the following without explicit human approval:

- Delete migration files.
- Reset, flush, or drop a database.
- Run destructive SQL.
- Read, print, copy, or commit `.env` secrets.
- Commit personal employee, payroll, supplier, or investor data.
- Install a new dependency.
- Modify multiple modules in one task.
- Use `--dangerously-skip-permissions`.
- Change an accepted accounting, costing, or rounding policy.
- Mark tests as skipped merely to make the suite pass.

## Working method

For each task:

1. Read the related requirements and ADRs.
2. Restate the acceptance criteria.
3. Identify edge cases and migration impact.
4. Write or update tests first.
5. Implement the smallest complete change.
6. Run the project quality commands.
7. Show `git diff` and summarize risks.
8. Do not commit unless asked.

## Commands

Use the virtual environment explicitly. Activation is not required.

```
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\mypy.exe apps config tests
.\.venv\Scripts\pre-commit.exe run --all-files
```

Dependency changes: edit `requirements.in` or `requirements-dev.in`, then
recompile and sync. Never edit the generated `.txt` locks by hand.

```
.\.venv\Scripts\pip-compile.exe requirements.in --output-file=requirements.txt
.\.venv\Scripts\pip-compile.exe requirements-dev.in --output-file=requirements-dev.txt
.\.venv\Scripts\pip-sync.exe requirements-dev.txt
```

Note: pip is pinned to 26.1.2 in this venv. pip 26.2+ breaks pip-tools 7.6.0.
Do not upgrade pip until pip-tools ships a compatible release. See ADR-010.

## Phase order

Phase 0 Foundations → 1 Inventory → 2 Procurement & AP → 3 Recipes &
Production → 4 Sales & Settlements → 5 Accounting & Treasury → 6 HR & Payroll
→ 7 Reports & Close → 8 Controlled AI.

Do not start a phase before the previous one meets its definition of done.

Phase 0 task order: 0.1 bootstrap · 0.2 custom User · 0.3 organization/branch ·
0.4 units of measure · 0.5 audit foundation · 0.6 accounting journal kernel ·
0.7 permissions/admin/API · 0.8 completion.

## Definition of done

A task is not complete until:

- Acceptance criteria are satisfied.
- Tests cover normal and failure paths.
- All tests pass.
- Ruff and mypy pass.
- No unexplained migration is pending.
- No secret or real personal data is added.
- Documentation and traceability are updated.
- The diff contains only the requested concern.
