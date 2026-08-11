# Overnight autonomous delivery — progress

Checkpoint contract for the 20-step pipeline. Updated after every completed
step and at least every 30–45 minutes. Chat memory is not the record; this is.

---

CURRENT_PIPELINE_STEP: 02/20 — Procurement domain specification (NOT STARTED)
CURRENT_TASK: none in flight
LAST_GREEN_COMMIT: e49da77
LAST_PUSHED_COMMIT: e49da77
CURRENT_BRANCH: phase/2-procurement (created from tag, pushed, tracking origin)

ACTIVE_WORKTREES:
- `khan-mandi-rms` — phase/2-procurement. Single lane, clean tree.

ACTIVE_DATABASES (none to be dropped):
- `khan_mandi_dev` — development, seeded and visible
- `test_khan_mandi_dev` — test runs
- `khan_mandi_p1_exit` — Phase 1 exit verification, seeded
- `khan_mandi_t17a_check`, `khan_mandi_t16_check`, `_t15_`, `_t14_`, `_t13_`,
  `khan_mandi_ledger_check`, `khan_mandi_inv_check`, `khan_mandi_freshcheck`

RUNNING_TESTS: none
FAILED_TESTS: none
FIX_BRANCHES: none
ERRORS_REMAINING: 0

NEXT_EXACT_ACTION: Step 02 — write the Procurement domain specification
(suppliers, catalogue, requests, quotations, comparison, PO, receipts,
inspection, invoices, matching, GRNI, returns, credit notes, payments,
allocations, aging, reports, imports, accounting, permissions, demo), plus
procurement invariants, task decomposition and traceability. Docs only; no
models until the spec is internally consistent with the existing architecture.

NEXT_EXACT_COMMAND:
```
cd "C:/Users/muama/Desktop/Khan Mandi/System/khan-mandi-rms"
git branch --show-current      # expect phase/2-procurement
.venv/Scripts/python.exe -m pytest -q      # expect 1613 passed
```

DEMO_STATE: `khan_mandi_dev` seeded and visible; `0 created, 83 reused` on
re-run; sign in as `moamel`, organization DEMO-KHAN-MANDI, branch
فرع البنوك — تجريبي; start at http://127.0.0.1:8000/inventory/stock/
RECONCILIATION_STATE: all three verifiers clean on both `khan_mandi_dev` and
`khan_mandi_p1_exit`

BLOCKERS: none

## Phase 2 starting facts

- Reuse `GOODS_RECEIVED_NOT_INVOICED` (already mapped, account `2-01-02-001`).
- Supplier balances are derived from documents, never a mutable field.
- Goods receipt posts through the existing inventory kernel; procurement adds
  no second posting path.
- Demo: three suppliers (DEMO-MEAT-SUPPLIER, DEMO-CHICKEN-SUPPLIER,
  DEMO-GROCERY-SUPPLIER) against the existing five items. Extend
  `seed_inventory_demo` or add `seed_procurement_demo` — one or the other, not
  a competing third command.

## Known follow-up (not a blocker)

The suite takes ~38–45 min because `seed_inventory_demo` runs per test across
~120 report/import/location tests at ~10 s each — about a third of the runtime
in one fixture. Fixing it edits those tests, so it invalidates any certifying
candidate and belongs in its own scoped task.

Sixty-one traceability rows from Tasks 1.1–1.2 still read `Specified`. They are
implemented; the rows cite test names that were written as intentions and never
matched to the tests that exist. Remapping them by hand is a documentation task
of its own — see the note at the head of `traceability.md`.

---

## Step log

| Step | Status | Commit / tag | Notes |
|---|---|---|---|
| 0A Task 1.7A | **CERTIFIED, PUSHED** | d6044e9 + 45136af | 1597 passed |
| 0B Task 1.7B | **CERTIFIED, PUSHED** | e49da77 | 1613 passed |
| 01 Inventory exit | **COMPLETE** | tag `phase-1-inventory-complete` | fresh DB, 29 routes, pre-commit green |
| 02 Procurement spec | not started | — | next |
| 03–20 | not started | — | — |
