# Supplier invoice operations

## One aggregate, four states

`SupplierInvoice` is the only supplier-invoice aggregate. Its lifecycle is
`DRAFT → APPROVED → POSTED → REVERSED`. A draft alone accepts header or line
changes. An approved document can return to draft only through the reasoned
command and only after any active match is cancelled. Posted documents are
corrected by exact reversal; their journals and posting generations remain
readable.

Release 1 stores `currency_code=IQD` and enforces it in the database. Approval
resolves the effective supplier credit-term version on the invoice date and
freezes UUID, version, label, days and due date. Supplier master changes never
restate an approved invoice.

## Lines and matching

- An `INVENTORY` line cites a posted receipt as evidence and posts only after a
  complete live three-way match. Posting clears the receipt's recorded GRNI,
  recognizes the exact price variance, and creates the payable. It never moves
  stock.
- An `ACCOUNT` line is for a direct expense or capitalized asset. The form
  removes role-owned accounts, and the service independently rejects inactive,
  non-postable, liability, equity, revenue, clearing, inventory-control, GRNI,
  payable, and other system-owned posting targets.

## Operator workflow

1. Open **Procurement → Supplier invoices** and use supplier, branch, lifecycle,
   matching, due/overdue, invoice-date or accounting-date filters.
2. Record a draft header, then add goods or direct-account lines. Supplier and
   branch form the document identity and cannot change on edit.
3. Review the detail evidence. Goods lines must show complete receipt/match
   coverage before posting becomes available.
4. Approve to freeze commercial terms. Use the reasoned return command if a
   correction is needed before posting.
5. Post to create the gapless system number, payable and journal atomically.
   Payments and supplier credits reduce the derived outstanding amount.
6. Reverse only with a reason. The detail screen retains original/reversal
   journals, actors, match release and the complete timeline.

Every write form and lifecycle command has a normal HTML fallback and an HTMX
fragment swap. Foreign organization identifiers resolve as 404; missing
authority inside scope is 403.

## API

The base is `/api/v1/procurement`:

- `GET/POST /supplier-invoices/`
- `GET/PATCH/DELETE /supplier-invoices/{id}/` (PATCH/DELETE are draft-only)
- `POST /supplier-invoices/{id}/lines/inventory/`
- `POST /supplier-invoices/{id}/lines/account/`
- `POST /supplier-invoices/{id}/approve/`
- `POST /supplier-invoices/{id}/return-to-draft/`
- `POST /supplier-invoices/{id}/post/`
- `POST /supplier-invoices/{id}/reverse/`

Money and quantities cross as exact strings. Without `view_supplier_cost`,
money and price keys are absent from raw JSON and HTML rather than present as
`null`. The list API supports the same supplier, branch, lifecycle, matching,
date, due, reference and system-number filters as the workspace.
