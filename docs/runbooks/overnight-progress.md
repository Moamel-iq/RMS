# Overnight autonomous delivery — progress

Checkpoint contract for the 20-step pipeline. Updated after every completed
step and at least every 30–45 minutes. Chat memory is not the record; this is.

---

CURRENT_PIPELINE_STEP: Prerequisite 0B — Task 1.7B stock locations
CURRENT_TASK: 1.7B core complete (models, services, ledger hook, invariant 22, tests)
LAST_GREEN_COMMIT: 1c6ac79
LAST_PUSHED_COMMIT: 45136af (phase/1-inventory — Task 1.7A certified)
CURRENT_BRANCH: phase/1-location

ACTIVE_WORKTREES:
- `khan-mandi-rms` — phase/1-location. Single lane; the 17b worktree was retired
  after 1.7A landed because it could not run Django (no `.env`).

ACTIVE_DATABASES:
- `khan_mandi_dev` — development, seeded
- `test_khan_mandi_dev` — test runs
- `khan_mandi_t17a_check` + seven earlier verification databases (all kept)

RUNNING_TESTS: none
FAILED_TESTS: none
FIX_BRANCHES: none
ERRORS_REMAINING: 0

NEXT_EXACT_ACTION: 1.7B remainder — location screens/routes/HTMX, demo locations,
`verify_stock_projection` location comparison, docs, then certify
NEXT_EXACT_COMMAND:
```
cd "C:/Users/muama/Desktop/Khan Mandi/System/khan-mandi-rms"
.venv/Scripts/python.exe -m pytest apps/inventory/tests/test_locations.py -q
```

DEMO_STATE: `khan_mandi_dev` seeded; `0 created, 79 reused`; no demo locations yet
RECONCILIATION_STATE: all three verifiers clean; `verify_locations` folded into
`verify_inventory_accounting`

BLOCKERS: none

ASSUMPTIONS:
- Locations carry quantity only (ADR-018 §2). The valuation key, `_StockKey` and
  the moving-average kernel are untouched.
- `sum(located) + unlocated == warehouse quantity`, unlocated **derived**.
- Outbound release order is ascending location code — a deterministic tie-break,
  explicitly not FEFO/FIFO, which ADR-018 keeps behind a strategy boundary.
- A location-to-location move posts no `StockMovement`.
- Locations remain optional; a warehouse may hold everything unlocated forever.
- Per-key count freezing stays deferred; counts remain FULL_WAREHOUSE + HARD_FREEZE.

---

## Step log

| Step | Status | Commit | Notes |
|---|---|---|---|
| 0A Task 1.7A | **CERTIFIED, PUSHED** | d6044e9 + 45136af | 1597 passed, 0 failed |
| 0B Task 1.7B | core done | 385ead6, 1c6ac79 | schema, services, hook, invariant, 16 tests |
| 01–20 pipeline | not started | — | blocked on 0B |
