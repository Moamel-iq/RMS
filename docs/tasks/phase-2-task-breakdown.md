# Phase 2 — Procurement and AP: task breakdown and exit gates

Proposed 2026-08-11 by Task 2.0. The order is dependency-driven, and the
governing principle carries over from Phase 1: **nothing depends on a figure
until the figure is reconcilable.**

## Why this shape

Three ordering constraints do most of the work.

**Master data before documents.** A purchase request naming a supplier that
does not exist is not a document, and a catalogue that arrives after the orders
it was meant to inform is a data-entry exercise rather than a control.

**The receipt comes before the invoice, and the accounting comes with it.**
A goods receipt is the first procurement event that touches the ledger, and it
touches two of them at once. Building the invoice first would mean building the
payable against a GRNI balance nothing had yet created — so 2.8 posts stock and
2.9 immediately proves it reconciles, before anything is allowed to clear it.

**Matching comes after both sides exist, and variance after matching.** Three-
way matching is meaningless with two of the three documents, and variance is
meaningless without a match to differ from. Building them earlier means
building them against fixtures instead of against posted history.

One consequence is worth stating plainly: **returns (2.13) depend on 2.9, not
on the invoice.** Goods can go back before anyone has agreed a price, and the
task order has to admit that or the implementation will assume otherwise.

## The tasks

### Task 2.0 — Domain specification — **THIS TASK**

Specification, invariants, task breakdown, two proposed ADRs. No code, no
models, no migrations.

**Exit:** the specification is internally consistent with ADR-006 – ADR-021 and
with the Phase 1 implementation, and every decision it defers is named in §16.

---

### Task 2.1 — Supplier master

`Supplier` with organization-scoped canonical code, bilingual names, contact
details, payment terms, credit metadata, archive and reactivate. Services,
scope-safe selectors, permissions, command API, Arabic RTL list and form,
HTMX filters, read-only admin, audit, three demo suppliers.

Depends on: 2.0. **Visible route required.**

---

### Task 2.2 — Supplier item catalogue

`SupplierItem`: supplier SKU, purchase package, lead time, minimum order,
preferred flag, effective dating, versioning. Fixed and variable package
compatibility. Demo catalogue rows for the five existing items.

Depends on: 2.1. **Visible route required.**

---

### Task 2.3 — Purchase requests

`PurchaseRequest` and lines, the five statuses, maker-checker as a database
constraint, conversion snapshot at submission, no ledger effect of any kind.
Demo: a draft, a submitted and an approved request.

Depends on: 2.2.

---

### Task 2.4 — Supplier quotations

`SupplierQuotation` and lines, validity, freight and other charges, evidence
reference. Demo: quotations from two suppliers against one request.

Depends on: 2.3.

---

### Task 2.5 — Quotation comparison and award

Normalised base-quantity comparison, landed base unit price, the comparison
report and its Arabic screen, the award with actor and reason. No automatic
selection. Demo: an award with a recorded reason.

Depends on: 2.4.

---

### Task 2.6 — Purchase orders

`PurchaseOrder` and lines, source request and quotation, agreed price, terms
snapshot, `DRAFT → APPROVED → ISSUED`. No stock effect, no payable effect.

Depends on: 2.5. **Visible route required.**

---

### Task 2.7 — Purchase order change control

Draft editing, `PurchaseOrderVersion` for issued orders, revision reason,
cancellation, the received-quantity floor, the supplier-change prohibition,
immutable version history. Demo: a revised order and a cancelled one.

Depends on: 2.6.

---

### Task 2.8 — Goods receipt and inspection

`GoodsReceipt` and lines, optional PO link, delivered/accepted/rejected,
measured quantity for variable packages, lot and expiry, warehouse and
location, supplier delivery reference, partial receipt, the zero-tolerance
over-receipt refusal, posting through the inventory kernel, idempotency,
reversal. Demo: a receipt with an accepted and a rejected line.

Depends on: 2.6. **Visible route required. First task in Phase 2 that moves
stock.**

---

### Task 2.9 — GRNI accounting

`Dr Inventory control / Cr GRNI` at accepted value, grouped debits where items
resolve to different control accounts, effective-dated role resolution, period
validation, mapping lock, atomic rollback, reversal, reconciliation, journal
drill-down from the receipt.

Depends on: 2.8. **Certification boundary: run the affected-domain suite.**

---

### Task 2.10 — Supplier invoices and the payable

`SupplierInvoice` and lines, unique number per supplier, invoice and due dates,
PO and receipt references, item and account lines, freight and discount
allocated with `apps/core/allocation.py`, the four statuses, the payable
posting, no stock mutation. Demo: a matched and an unmatched invoice.

Depends on: 2.9. **Run the complete project suite at this boundary.**

**Delivered, with one boundary that was not obvious when this was written.**
"The payable posting" is complete for a **direct account** line:
`Dr` the chosen expense or asset account, `Cr SUPPLIER_PAYABLE`. It is *not*
complete for an **inventory** line, because §9 of Task 2.0 posts the *matched
receipt value* to GRNI and the difference to purchase price variance — and both
figures come from a match allocation, which is Task 2.11. An invoice carrying a
goods line therefore approves and holds, with `invoice_awaiting_matching` and a
screen that says why. Posting the invoiced amount to GRNI instead would balance
and be wrong: it would clear a variance nobody computed and leave 2.12 nothing
to recognise. Task 2.11 and 2.12 activate that path together, and
`TestTheMatchingBoundary` holds the line until they do.

---

### Task 2.11 — Three-way matching

`MatchAllocation` among PO line, receipt line and invoice line. Ordered,
accepted, invoiced, matched quantity and value; quantity and price variance;
derived exception status. Partial and multiple allocations. Over-allocation
impossible.

Depends on: 2.10.

---

### Task 2.12 — Price and quantity variance accounting

GRNI clearing, payable posting, the on-hand versus consumed split, deterministic
residual allocation, the explicit revaluation path, no historical movement
mutation, reconciliation. ADR-022 is written here.

Depends on: 2.11.

---

### Task 2.13 — Supplier returns

Source receipt and lot, return quantity, warehouse and location, outbound at
the standing average, negative-stock prevention, the distinct movement type,
the supplier-credit-expected state, accounting clearing, reversal.

Depends on: 2.9 — **not** on the invoice.

---

### Task 2.14 — Supplier credit notes

Supplier, invoice and return references, allocations, amount, reason, payable
reduction or standing credit, reversal, duplicate-document protection.

Depends on: 2.13.

---

### Task 2.15 — Supplier payments and allocations

Payment, cash or bank via effective-dated role, date, amount, invoice
allocations, partial payment, oldest-invoice default, unallocated advance, no
over-allocation, accounting, reversal. Demo: a partial payment leaving a
correct open balance.

Depends on: 2.10.

---

### Task 2.16 — Reports and reconciliation

Supplier aging, supplier statement, open POs, outstanding receipt quantity,
GRNI exceptions, invoice-without-receipt, matching exceptions, purchase spend,
price variance, return and credit status, payment allocations, and
`verify_procurement_accounting`. Scoped CSV, HTMX filters, pagination. No
repair button.

Depends on: 2.15. **Run the complete project suite at this boundary.**

---

### Task 2.17 — Imports, demo completion and hardening

Preview-first imports for supplier master, supplier-item catalogue, and
purchase-request drafts only. File security, atomic apply, idempotency,
cross-tenant tests, concurrency tests, export formula protection, admin
lockdown, the complete demo command, the visible route matrix, HTMX
verification.

Depends on: 2.16.

---

### Task 2.18 — Phase 2 exit gate

All fifty procurement invariants verified. Supplier subledger equals the
payable account. Accepted receipt quantity equals inventory. GRNI reconciles.
Matching allocations balance. No duplicate posting, no cross-tenant access, no
scope leak. Exact Decimals. Demo visible on every route. Fresh database
migrated from zero. Complete suite, all quality gates, zero pending migrations,
traceability citing real tests.

**Exit:** tag `phase-2-procurement-complete`. Not merged into `main`.

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

A full-project suite runs at the 2.10 and 2.16 boundaries, at 2.18, and
whenever a change reaches the inventory or accounting kernel — not after every
small step.
