# Overnight autonomous delivery — progress

Checkpoint contract for the 20-step pipeline. Updated after every completed
step and at least every 30–45 minutes. Chat memory is not the record; this is.

---

CURRENT_PIPELINE_STEP: Prerequisite 0B — Task 1.7B, certifying
CURRENT_TASK: Task 1.7B stock locations — implementation complete
LAST_GREEN_COMMIT: 7ea595f
LAST_PUSHED_COMMIT: 45136af (phase/1-inventory — Task 1.7A certified and pushed)
CURRENT_BRANCH: phase/1-location (not yet pushed)

ACTIVE_WORKTREES:
- `khan-mandi-rms` — phase/1-location. Single lane; the 17b worktree was retired
  after 1.7A landed because it could not run Django without `.env`.

ACTIVE_DATABASES:
- `khan_mandi_dev` — seeded, includes demo locations
- `test_khan_mandi_dev` — certification suite running. **Do not touch.**
- `khan_mandi_t17a_check` + seven earlier verification databases (all kept)

RUNNING_TESTS: full suite on the 1.7B candidate, `/tmp/cert17b.txt`
FAILED_TESTS: none open
FIX_BRANCHES: none
ERRORS_REMAINING: 0

NEXT_EXACT_ACTION: on green — push `phase/1-location`, then Step 01 (Inventory
phase exit: fresh database, tag `phase-1-inventory-complete`, branch
`phase/2-procurement`)
NEXT_EXACT_COMMAND:
```
cd "C:/Users/muama/Desktop/Khan Mandi/System/khan-mandi-rms"
git push origin phase/1-location
```

DEMO_STATE: `0 created, 83 reused`; 3 bins in DEMO-MAIN with a put-away and an
internal move; unlocated remainder visible; `/inventory/reports/locations/`
renders 15 rows
RECONCILIATION_STATE: `verify_inventory_accounting` (now including
`verify_locations`), `verify_stock_ledger`, `verify_stock_projection` all clean

BLOCKERS: none

ASSUMPTIONS:
- Locations carry quantity only (ADR-018 §2); valuation key untouched.
- `sum(located) + unlocated == warehouse quantity`, unlocated derived.
- Outbound release: unlocated first, then bins in ascending code order — a
  deterministic tie-break, explicitly not FEFO/FIFO.
- Locations optional and permanently so; a warehouse may stay fully unlocated.
- Per-key count freezing deferred; counts remain FULL_WAREHOUSE + HARD_FREEZE.
- `import_opening_draft` reserved, granted to no role.

## Known follow-up (not a blocker)

The suite takes ~37–45 min because `seed_inventory_demo` runs per test across
~120 report/import/location tests at ~10 s each — roughly a third of the
runtime in one fixture. Fixing it means editing those tests, which invalidates
whatever candidate is certifying, so it is a scoped task of its own rather than
an edit to squeeze in.

---

## Step log

| Step | Status | Commit | Notes |
|---|---|---|---|
| 0A Task 1.7A | **CERTIFIED, PUSHED** | d6044e9 + 45136af | 1597 passed |
| 0B Task 1.7B | certifying | 385ead6 · 1c6ac79 · 2c0fb9d · 302e09a · 7ea595f | 16 location tests; 98 combined |
| 01–20 pipeline | not started | — | blocked on 0B |
