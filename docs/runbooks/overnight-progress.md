# Overnight autonomous delivery — progress

Checkpoint contract for the 20-step pipeline. Updated after every completed
step and at least every 30–45 minutes. Chat memory is not the record; this is.

---

CURRENT_PIPELINE_STEP: 12/20 — Supplier invoices and payables (NOT STARTED)
CURRENT_TASK: none in flight
LAST_GREEN_COMMIT: ca18fc9
LAST_PUSHED_COMMIT: ca18fc9
WORKING_TREE: clean
RUNNING_TESTS: none
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

NEXT_EXACT_ACTION: Step 12 — Task 2.10, supplier invoices and payables.
`SupplierInvoice` + `SupplierInvoiceLine`, lifecycle
DRAFT → APPROVED → POSTED → REVERSED, every transition authorised by the
locked row. An invoice creates or confirms a **payable** and touches no
stock at all (PRC-038): no movement, no `StockBalance` write, no location
quantity. Price differences against the receipt are preserved for Step 13
matching and Step 14 variance, never hidden in the payable.

What Step 11 leaves in place for it:
- `PROCUREMENT_GOODS_RECEIPT` is the receipt's canonical source type;
  the invoice needs its own, following the same `PROCUREMENT_*` shape.
- `GoodsReceiptLine.posted_value` is the GRNI figure an invoice will clear.
  It is stored, not derived, precisely so Step 13 can allocate against it.
- `apps/procurement/reconciliation.py` holds `verify_goods_receipt`,
  `verify_order_received_quantities`, `verify_grni` and `verify_procurement`.
  Extend those rather than starting a second verifier module.
- `apps/procurement/posting.py` is the shape to copy: one atomic command per
  economic event, the documented lock order, source identity derived from the
  document's own `public_id`.
- Do **not** create Step 13 match allocations during invoice posting. The
  invoice may reference receipt lines; allocations stay explicit Step 13 rows.

Duplicate protection (PRC-037, G3): unique per
`(organization, supplier, normalized supplier_invoice_number)`. Trim
surrounding whitespace consistently *before* validation, uniqueness lookup,
fingerprint and persistence, so `"INV-001"` and `" INV-001 "` cannot become
two invoices. Preserve meaningful leading zeros; never cast to int.

NEXT_EXACT_COMMAND:
```
cd "C:/Users/muama/Desktop/Khan Mandi/System/khan-mandi-rms"
git branch --show-current                     # expect phase/2-procurement
.venv/Scripts/python.exe -m pytest apps/procurement -q   # expect 346 passed
```

DEMO_STATE: `khan_mandi_dev` seeded and visible; sign in as `moamel`,
organization DEMO-KHAN-MANDI; start at http://127.0.0.1:8000/inventory/stock/.
Procurement: 3 suppliers, 6 catalogue rows, 4 purchase requests, 2
quotations, one award, 3 purchase orders, one order revision and **5 goods
receipts** — GRN-2026-000001 posted (60 kg rice against the order),
GRN-2026-000002 posted with three of twelve chicken cartons rejected,
GRN-2026-000003 posted from a weighed variable container, GRN-2026-000004
posted then reversed, and DEMO-GRN-PARTIAL-2 left DRAFT so a reader has
something to inspect and post by hand. The seed is idempotent: a second run
leaves movements at 44, journals at 30 and receipts at 5.

Routes verified rendering for an authorised user (Django test client with
`force_login`, so no credential is read or typed):
/procurement/suppliers/, /procurement/catalogue/, /procurement/requests/,
/procurement/quotations/, /procurement/requests/<pk>/comparison/,
/procurement/orders/, /procurement/orders/<pk>/history/,
/procurement/receipts/, /procurement/receipts/<pk>/.
Commands are POST-only: /procurement/receipts/<pk>/post/ and
/procurement/receipts/<pk>/reverse/ — a GET returns 405.
HTMX verified: the receipt list returns a fragment only (3,465 bytes against
17,035 for the full page) and `?status=POSTED` narrows it to the three posted
rows with no draft chip.

Requests are raised by `demo-storekeeper` and decided by `moamel`, because
maker-checker is a database constraint and one actor could not do both.

RECONCILIATION_STATE: clean on `khan_mandi_dev` across all four verifiers —
`verify_procurement`, `verify_organization`, `verify_inventory_against_gl`
and `verify_locations`. Step 11 is the first task where a procurement
document reaches either ledger, so the Batch 2 claim ("no journal or ledger
entry cites a procurement source") is now deliberately false and replaced by
the equalities in `apps/procurement/reconciliation.py`: accepted quantity ==
movement quantity == location effect, and posted value == movement value ==
inventory-control debit == GRNI credit.

ASSUMPTIONS:
- An award requires a reason unconditionally, not only when the winner is
  dearer as PRC-017 states. Stricter than specified, and deliberate: a
  field that is usually empty is a field nobody reads.
- Purchasing issues but never approves a purchase order. The spec does not
  name the split; it follows the same separation the request already uses.

- Task 2.0 §7 gives the receipt three statuses only: DRAFT, POSTED,
  REVERSED. Inspection is line data, and readiness is derived
  (`is_ready_to_post`), so no INSPECTED/READY state was invented.
- Delivery-reference uniqueness is scoped per supplier, not globally.
- An inspected-but-unposted receipt reserves nothing on the order;
  `add_receipt_line` compensates by also subtracting other drafts.

- Task 2.9. A receipt where **every** line was rejected is refused rather
  than posted as a zero-effect physical record. Task 2.0 §7 gives the
  document three statuses and no zero-value posted state, and inventing one
  would mean inventing accounting for it. The rejection stays recorded on
  the draft, which is where the supplier claim lives anyway.
- The posting idempotency key is derived from the receipt's own `public_id`
  rather than accepted from the caller. Posting *this receipt* is the whole
  command and a posted receipt is frozen by a trigger, so a retry cannot
  present the same key with a different payload. The kernel still refuses a
  duplicate on source identity independently.
- A reversal picks stock back out of the bin the receipt filled, but only as
  much as that bin still holds; the kernel's deterministic auto-release
  covers any remainder. Moving goods between shelves is ordinary warehouse
  work and must not make a reversal impossible. The warehouse total falls by
  exactly the accepted quantity either way, which is the invariant.
- `post_goods_receipt` re-reads the order under a lock and refuses a
  cancelled one, even though `create_goods_receipt` already checked. The
  order can be cancelled while a draft sits on somebody's screen, and
  posting is the act that makes the answer permanent.
- Command endpoints are POST-only URL routes rather than Django Ninja
  operations, because that is the convention every procurement command
  already follows and inventory exposes no posting command over its API
  either. There is deliberately no writable generic CRUD over a posted
  receipt.

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
- Every lifecycle guard re-reads its document under a row lock rather than
  trusting the instance it was handed. Found three times; now stated once
  in `apps/procurement/lifecycle.py::lock_and_require_status` and covered
  by `TestTheStaleInstanceRule`. Every new lifecycle service uses it.
- Demo and test dates must stay meaningful as the calendar moves. Two
  defects came from validity windows that had silently expired.
- `_require_draft` re-reads status under a row lock rather than trusting the
  instance handed to it. A stale in-memory DRAFT would otherwise let a line
  be added to an approved request, and no constraint could catch it.
- `ProcurementDocumentSequence` is procurement's own gapless counter.
  Numbers are drawn at submission, never at creation.
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
| 04 Supplier catalogue | **COMPLETE, PUSHED** | 637bd16 | 36 tests, 6 rows, gist no-overlap, AST boundary test |
| 05 Purchase requests | **COMPLETE, PUSHED** | 1692d50 | 39 tests, 4 demo requests, maker-checker at the database |
| 06 Supplier quotations | **COMPLETE, PUSHED** | 63c82be | 35 tests, 2 demo offers, derived totals |
| **Batch 2 cert (04–06)** | **PASS** | 63c82be | 301 tests, verifiers clean, no procurement posting |
| 07 Comparison and award | **COMPLETE, PUSHED** | c169941 | 28 tests, ranking inversion visible on the route |
| 08 Purchase orders | **COMPLETE, PUSHED** | c89ac1d | 36 tests, 3 demo orders, chain visible end to end |
| 09 PO change control | **COMPLETE, PUSHED** | ee2365e | 28 tests, versioned history, shared lifecycle helper |
| **Batch 3 cert (07–09)** | **PASS** | ee2365e | 414 tests, verifiers clean, no procurement posting |
| 10 Goods receipt | **COMPLETE, PUSHED** | aa12633 | 50 tests, 4 demo drafts, seam activated, no posting path |
| 11 Receipt posting + GRNI | **COMPLETE, PUSHED** | ca18fc9 | 346 tests, 6 real-COMMIT races, demo 39→44 movements and 25→30 journals |
| 12–20 | not started | — | — |
