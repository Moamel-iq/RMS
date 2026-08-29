# One-page supplier invoice drafts

`/procurement/invoices/new/` accepts the invoice header and inventory rows in
one RTL screen. Saving creates a `DRAFT` only; approval, posting, matching and
payment remain separate commands.

## Invalid rows

`SupplierInvoiceLine` remains strictly constrained. A submitted row that
cannot satisfy those constraints is stored in
`SupplierInvoiceDraftLineIssue` as the original strings plus field-specific
errors. It has no effect on invoice totals, stock or accounting and blocks the
posting action. Correcting it creates one normal invoice line and marks the
issue resolved; deleting an open issue is audited.

All mutations use procurement invoice services inside atomic transactions.
Supplier, branch, item and invoice access are organization scoped on the
server. Browser calculations are a convenience only; stored amounts are
recalculated with `Decimal` by the existing invoice services.

## Migration and rollback

Migration `0046_supplier_invoice_draft_line_issue` only adds the staging
table, its foreign keys, check constraint and lookup index. It does not rewrite
existing invoices or lines. Before rolling it back, resolve or export any open
issues because reversing the migration drops their entered values and errors.
