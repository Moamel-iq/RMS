# Overnight autonomous delivery — progress

Checkpoint contract for the 20-step pipeline. Updated after every completed
step and at least every 30–45 minutes. Chat memory is not the record; this is.

---

CURRENT_PIPELINE_STEP: 04/20 — Supplier item catalogue (NOT STARTED)
CURRENT_TASK: none in flight
LAST_GREEN_COMMIT: be918c0
LAST_PUSHED_COMMIT: be918c0
CURRENT_BRANCH: phase/2-procurement (tracking origin)

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

NEXT_EXACT_ACTION: Step 04 — Task 2.2, the supplier item catalogue.
`SupplierItem` linking `Supplier` to `inventory.InventoryItem`: supplier
SKU, purchase package, lead time, minimum order, preferred flag, effective
dating with an `EXCLUDE USING gist` no-overlap constraint, versioning
rather than editing once referenced. Services, scope-safe selectors,
permissions, API, Arabic RTL screens, demo catalogue rows against the five
existing inventory demo items. See Task 2.0 §3 (PRC-005 – PRC-008).

A catalogue price values nothing and no posting service may read it —
PRC-005 is an AST boundary test, not a comment.

NEXT_EXACT_COMMAND:
```
cd "C:/Users/muama/Desktop/Khan Mandi/System/khan-mandi-rms"
git branch --show-current                      # expect phase/2-procurement
.venv/Scripts/python.exe -m pytest apps/procurement -q   # expect 41 passed
```

DEMO_STATE: `khan_mandi_dev` seeded and visible; sign in as `moamel`,
organization DEMO-KHAN-MANDI; start at http://127.0.0.1:8000/inventory/stock/.
Procurement: three demo suppliers seeded and visible at
http://127.0.0.1:8000/procurement/suppliers/ (`0 created` on re-run).
RECONCILIATION_STATE: all three inventory verifiers clean on `khan_mandi_dev`
and `khan_mandi_p1_exit`

BLOCKERS: none

## Phase 2 starting facts

- Reuse `GOODS_RECEIVED_NOT_INVOICED` (already mapped, account `2-01-02-001`).
- Supplier balances are derived from documents, never a mutable field.
- Goods receipt posts through the existing inventory kernel; procurement adds
  no second posting path.
- Demo: three suppliers (DEMO-MEAT-SUPPLIER, DEMO-CHICKEN-SUPPLIER,
  DEMO-GROCERY-SUPPLIER) against the existing five items.
- `view_supplier` is Django's **builtin** view permission for the `Supplier`
  model, not a custom one. Declaring it in `Meta.permissions` is an
  `auth.E005` clash. The codename is still `procurement.view_supplier`.
- `apps/procurement/views.py` subclasses the list/write/action bases from
  `apps.inventory.views` rather than copying them. Extracting them into
  `apps.core` is worth doing when a third module needs them; it is a refactor
  of certified code and does not belong inside a feature task.

## Closed follow-ups

Both Phase 1 hygiene items are done.

- **Traceability** (`e1afe79`). All 60 aspirational rows reconciled; 275
  unanchored citations given their file; `tests/test_traceability.py` fails the
  build if a row cites a test that does not exist.
- **Suite runtime** (`d9ff702`). The three demo-seeding files went from 18:48
  to 2:05 on the same 123 tests, by sharing one seed per module. Two
  transactional classes moved to `test_location_concurrency.py` and
  `test_import_constraints.py`; `refuse_transactional_tests` stops the
  incompatible combination from ever going quietly green. The move broke
  two traceability citations, which the full suite caught and `51024b6`
  fixed forward.

Measured honestly: the three files went 18:48 → 2:05 back to back on the
same machine, but the **full** suite went 38:42 → 34:56, not the ~22
minutes that saving alone implies. The file-level number is a controlled
comparison; the full-suite number is against a baseline taken on a
different day. Where the rest of the difference went is not established,
and no further optimisation was attempted — the known repeated setup was
the item, and it is addressed.

## Step log

| Step | Status | Commit / tag | Notes |
|---|---|---|---|
| 0A Task 1.7A | **CERTIFIED, PUSHED** | d6044e9 + 45136af | 1597 passed |
| 0B Task 1.7B | **CERTIFIED, PUSHED** | e49da77 | 1613 passed |
| 01 Inventory exit | **COMPLETE** | tag `phase-1-inventory-complete` | fresh DB, 29 routes, pre-commit green |
| B1 Traceability | **COMPLETE, PUSHED** | e1afe79 | 0 unresolved citations, was 233 |
| B2 Suite runtime | **COMPLETE, PUSHED** | d9ff702 | 123 tests, 18:48 → 2:05 |
| 02 Procurement spec | **COMPLETE, PUSHED** | d6c2b0f | 67 PRC requirements, 50 invariants, ADR-022/023 |
| 03 Supplier master | **COMPLETE, PUSHED** | be918c0 | 41 tests, 3 demo suppliers, route + htmx verified |
| 04–20 | not started | — | — |
