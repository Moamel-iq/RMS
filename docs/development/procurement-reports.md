# Procurement reports and the GL tie-out

Task 2.16. Twelve reads and one verifier, and the rule that binds them:
**every figure is the derivation the reconciliation proves, never a second
formula.** A report that computed a supplier balance its own way would one
day disagree with `verify_procurement_accounting`, and both would be
plausible. Here they cannot disagree, because they are the same calls —
`outstanding_amount`, `unallocated_credit`, `advance_remainder`,
`settled_book_value_for`, and the GRNI clearing arithmetic.

## Built on the Phase 1 machinery, not beside it

`ProcurementReportView` subclasses the inventory report base
(`apps/inventory/report_views.py`), so the shared data-driven template, the
CSV export that is the same call as the screen, the formula neutralisation,
the UTF-8-BOM provenance header, the pagination and the HTMX fragment
fallback are inherited, not reimplemented. Three things change:

- **Entry permission**: `procurement.view_procurement_report`,
  organization-scoped, granted to manager, accounting manager, accountant
  and purchasing. Storekeepers and cashiers act on documents, not on
  module-wide totals.
- **Cost redaction**: `include_valuation` reads
  `procurement.view_supplier_cost` instead of `inventory.view_valuation`.
  Cost keys are **omitted, never blanked** (PRC-061) — the query services
  leave them out of the row dict, the template renders whatever keys exist,
  and the CSV writes whatever columns exist, so neither surface can leak
  what the other hides.
- **Filters**: `ProcurementReportFilters` subclasses the Phase 1
  `ReportFilters` and adds `supplier_id`, keeping the shared chrome (mode
  label, export querystring) working unchanged.

Scope is `organizations_with_permission` — the same reach
`visible_supplier_invoices` certifies (PRC-060): a post whose role carries
the permission names the organizations the caller reads. A permission
attached directly to a user names no post and therefore reaches no rows,
which the tests assert. A filter can narrow that scope and can never widen
it.

## The reports

| Report | Route (`/procurement/reports/…`) | Reads |
|---|---|---|
| Supplier aging | `supplier-aging/` | Open posted invoices bucketed by due date, plus standing credit and standing advances, plus the net position |
| Supplier statement | `supplier-statement/` | Every posted money document, running balance |
| Open purchase orders | `open-orders/` | Issued order lines with an undelivered remainder |
| Outstanding receipt quantity | `outstanding-receipts/` | The same, folded per item |
| GRNI exceptions | `grni-exceptions/` | Posted receipt lines no live posting has cleared, ageing |
| Invoice without receipt | `invoice-without-receipt/` | Approved/posted goods lines no match has ever covered |
| Matching exceptions | `matching-exceptions/` | Non-zero variances on standing matches **not yet behind a live posting** — pending decisions |
| Purchase spend | `purchase-spend/` | Posted invoice value by supplier and month |
| Price variance | `price-variance/` | Variances behind live postings — the parked balance, explained |
| Return and credit status | `return-credit-status/` | Book value, settled, open claim per posted return line |
| Payment allocations | `payment-allocations/` | What each posted payment covered; the advance that stands |
| Procurement to GL | `procurement-to-gl/` | The PRC-058 equalities as rows |

Aging and the statement disagree on purpose about what a payment's
remainder means, and both are right: the statement lowers the balance only
by the **allocated** share and shows the advance on the row, because an
advance is an asset, not a smaller debt (PRC-055); aging shows the advance
in its own column beside the buckets.

Same-day statement rows order **charges before settlements** (invoice,
credit note, payment), then by document number. The documents carry a
business date, not a time, so there is no intraday chronology to recover —
and this ordering shows a same-day debt arising and then being settled,
never a balance dipping spuriously negative in between.

The matching lifecycle moves one variance through three reports: unmatched,
the invoice sits in "invoice without receipt"; matched but unposted, it is a
pending decision in "matching exceptions"; posted, it leaves both and
becomes a line in "price variance", and the GRNI exception its delivery had
been raising disappears.

## `verify_procurement_accounting` (PRC-058)

The phase's proof obligation, composed into `verify_procurement`:

1. **Open supplier balances = the payable account balance.** Whole-account,
   because the payable account is procurement-exclusive.
2. **Uninvoiced accepted receipt value = procurement's GRNI contribution.**
   Delegates `verify_grni_clearing` — one derivation for verifier and
   report alike. Scoped, not whole-account: Task 1.4's uninvoiced stock
   receipts share the account and are not procurement's to explain.
3. **Every posted procurement journal traces to exactly one source
   document**, across all six source types (receipt, invoice posting,
   return, credit note, payment).
4. **No double posting, no allocation beyond its parent** — re-checked by
   the per-document verifiers `verify_procurement` runs alongside this one
   (`allocations_exceed_*`, the posting-generation model, and the database
   constraints beneath both).

**No repair path exists** (PRC-059). A planted journal is reported by the
verifier, shown as "غير مطابق" on the Procurement-to-GL screen, and left
exactly where it is.

## Tests

`apps/procurement/tests/test_procurement_reports.py`: the permission sweep
across all twelve routes, the scope arms (branch post reaches, hand-granted
permission does not), redaction at the row and the CSV, per-report
correctness on service-built scenarios, the matching-lifecycle walk, the
partial credit-note settlement walk, the clean and planted tie-outs, formula
neutralisation of a hostile supplier name, and the HTMX fragment fallback.
