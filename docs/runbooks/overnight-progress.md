# Overnight autonomous delivery — progress

Checkpoint contract for the 20-step pipeline. Updated after every completed
step and at least every 30–45 minutes. Chat memory is not the record; this is.

---

CURRENT_PIPELINE_STEP: 13/20 — Three-way matching (NOT STARTED)
CURRENT_TASK: none in flight
LAST_GREEN_COMMIT: 64d94f8
LAST_PUSHED_COMMIT: 64d94f8
WORKING_TREE: clean
RUNNING_TESTS: none
CURRENT_BRANCH: phase/2-procurement (tracking origin)

ACTIVE_WORKTREES:
- `khan-mandi-rms` — phase/2-procurement. Single lane, clean tree.

ACTIVE_DATABASES (none to be dropped):
- `khan_mandi_dev` — development, seeded and visible
- `test_khan_mandi_dev` — test runs
- `khan_mandi_p1_exit` — Phase 1 exit verification, seeded
- `khan_mandi_p2_b4` — Batch 4 verification, migrated from zero and
  seeded; created by Step 12, never to be dropped
- `khan_mandi_t17a_check`, `khan_mandi_t16_check`, `_t15_`, `_t14_`, `_t13_`,
  `khan_mandi_ledger_check`, `khan_mandi_inv_check`, `khan_mandi_freshcheck`

RUNNING_TESTS: none
FAILED_TESTS: none
FIX_BRANCHES: none
ERRORS_REMAINING: 0

NEXT_EXACT_ACTION: Step 13 — Task 2.11, explicit three-way matching.
`MatchAllocation` among purchase order line, goods receipt line and supplier
invoice line. Ordered, accepted, invoiced, matched quantity and value;
quantity and price variance; derived exception status (PRC-042 — never a
stored flag). Partial and multiple allocations both ways; over-allocation
impossible under a row lock **and** as a reconciliation invariant.

The boundary Step 13 inherits, and the one it may not cross alone:

- `apps/procurement/invoices.py::_require_every_line_has_a_route` refuses to
  post any invoice carrying an `INVENTORY` line, with
  `invoice_awaiting_matching`. That refusal is correct and must NOT be
  removed by Task 2.11 on its own. The GRNI/variance entry (Task 2.0 §9) needs
  the *matched receipt value* AND the variance account, and
  `PURCHASE_PRICE_VARIANCE` is deliberately unseeded until Task 2.12 posts to
  it. Matching may become complete as a non-financial workflow; the financial
  POST activates when 2.11 and 2.12 land together.
- `TestTheMatchingBoundary` in `test_supplier_invoices.py` is the marker.
  Its assertions — no `MatchAllocation` model, no variance role, an invoice
  that approves and holds — are the negatives whose positive twins Task 2.11
  and 2.12 must write. Replace them; do not merely delete them.
- `SupplierInvoiceLine.receipt_line` and `.order_line` already exist and are
  populated. They are **evidence, not allocation**: nothing consumes a
  receipt's matchable remainder today, and `test_no_match_allocation_is_
  created_by_referencing_a_receipt` holds that line.
- `GoodsReceiptLine.posted_value` is the GRNI figure an allocation clears.
  Stored, not derived, precisely so 2.11 can allocate against it.

NEXT_EXACT_COMMAND:
```
cd "C:/Users/muama/Desktop/Khan Mandi/System/khan-mandi-rms"
git branch --show-current                     # expect phase/2-procurement
.venv/Scripts/python.exe -m pytest apps/procurement -q   # expect 437 passed
```

DEMO_STATE: `khan_mandi_dev` seeded and visible; sign in as `moamel`,
organization DEMO-KHAN-MANDI. Procurement now shows 3 suppliers, 6 catalogue
rows, 4 purchase requests, 2 quotations, one award, 3 purchase orders, one
order revision, 5 goods receipts and **4 supplier invoices**:

- `DEMO-SINV-EXPENSE` — SINV-2026-000001, POSTED. A delivery charge of 75,000
  with no goods on it: `Dr` 5-01-02-003 (cost centre DELIVERY),
  `Cr` 2-01-01-001. The whole of what Task 2.10 can post.
- `DEMO-SINV-GOODS` — APPROVED and waiting. Bills the rice that arrived, at
  1,450 against the 1,400 the receipt posted, so the difference the three-way
  match exists to surface is really there for Task 2.11 to find.
- `DEMO-SINV-DRAFT` — a draft carrying 10,000 freight and a 3,000 discount
  allocated across two lines, so the largest-remainder split is visible.
- `DEMO-SINV-REVERSED` — SINV-2026-000002, posted then reversed.

Counts after the invoices: stock movements **44** (unchanged — an invoice
moves none) and journals **30 → 33**. Supplier outstanding derives to 75,000
for DEMO-GROCERY-SUPPLIER and 0 for the other two. A second seed run changes
nothing.

Routes verified rendering for an authorised user (Django test client with
`force_login`, so no credential is read or typed): /procurement/invoices/,
/procurement/invoices/new/, /procurement/invoices/<pk>/. Commands are POST-only
— approve, return, post and reverse each answer 405 to a GET. HTMX verified:
the list returns a fragment only (2,993 bytes against 16,616 for the full page)
and `?status=POSTED` narrows to the two numbered rows with no draft chip.

RECONCILIATION_STATE: clean on `khan_mandi_dev` and on the fresh
`khan_mandi_p2_b4` across all four verifiers — `verify_procurement` (now
including `verify_supplier_invoice`, `verify_invoice_charges` and
`verify_supplier_payables`), `verify_organization`,
`verify_inventory_against_gl` and `verify_locations`.

BATCH 4 CERTIFICATION (Steps 10–12): **PASS** at 64d94f8. Complete project
suite 2053 passed, 0 failed. Fresh database `khan_mandi_p2_b4` migrated from
zero, roles and permissions seeded, both demo seeds run twice with identical
counts, every procurement route rendered, all four verifiers clean. Quality
gates: ruff, ruff format, mypy (224 files), manage.py check, makemigrations
--check, pre-commit 13 hooks — all pass.

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

- Task 2.10. **A supplier invoice carrying a goods line cannot post here.**
  Task 2.0 §9 posts the matched receipt value to GRNI and the difference to
  purchase price variance, and both come from a Task 2.11 match allocation.
  Posting the invoiced amount to GRNI instead would balance and be wrong.
  The whole document waits in APPROVED with `invoice_awaiting_matching`;
  half-posting would create a payable for part of what is owed.
- **Invoice-before-receipt is not supported**, and that is a reading of the
  spec rather than an omission: §15's account-role table contains no
  `UNRECEIVED_INVENTORY_CLEARING`, so the clearing route has no approved
  account behind it.
- Supplier invoice numbers are compared **case-insensitively**, which Task 2.0
  does not state either way. Stricter than specified and deliberate: the
  reference is stored exactly as the supplier wrote it and folded only for
  the uniqueness key, and paying the same invoice twice is the expensive
  direction to be wrong in. Leading zeros and internal spacing are preserved,
  so `INV-001` and `INV-0001` remain different documents.
- A direct account line lets the person entering the invoice **choose the
  account**. PRC-034 forbids a *posting service* naming an account, and none
  does — `SUPPLIER_PAYABLE` still resolves through an effective-dated role.
  Which expense a delivery charge belongs to is a judgement only the person
  holding the document can make; the alternative is a role per expense
  category, invented by us.
- Every expense account in the seeded chart sets `requires_cost_center`, so
  every demo and test expense line names one. That is the chart's policy
  working, not a workaround.
- `AccountRoleDomain` gains `PURCHASING`. The enum's own docstring invited it
  ("Purchases, Sales and Payroll add their own values when their posting
  rules arrive"), and a supplier payable filed under `INVENTORY` would make
  the domain column a label rather than a fact.
- Only `SUPPLIER_PAYABLE` of Task 2.0 §15's five new roles is seeded. A role
  with no posting rule behind it is a mapping an accounting manager can be
  asked to fill in for a workflow that does not exist — the mistake
  `import_opening_draft` records in inventory.
- Approval is **not** maker-checker at the database. Task 2.0 states that rule
  for the purchase request (PRC-010) and not for the invoice, so no
  `approved_by != created_by` constraint was invented. The role map achieves
  the separation instead: `ACCOUNTANT` creates and cannot approve;
  `ACCOUNTING_MANAGER` approves, posts and reverses, and cannot create.

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
| 12 Supplier invoices | **COMPLETE, PUSHED** | 64d94f8 | 91 tests, 4 demo invoices, payable derived, matching boundary held |
| **Batch 4 cert (10–12)** | **PASS** | 64d94f8 | 2053 project tests, fresh DB from zero, four verifiers clean |
| 13–20 | not started | — | — |
