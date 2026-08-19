# ADR-033 — Structured supplier-invoice charges and conservative landed costs

- **Status:** Accepted
- **Date:** 2026-08-19
- **Scope:** Procurement completion, capability 2
- **Related:** ADR-012 (exact allocation), ADR-018 (inventory valuation),
  ADR-019 (posting accounts), ADR-023 (GRNI and invoice matching)

## Context

The original invoice header had one freight amount. It was proportionally
folded into invoice lines and therefore could not state whether the cost was a
direct expense or an inventory acquisition cost. It also had no category,
evidence reference, cost centre, allocation basis, receipt target or exact
reversal identity. Capitalising that field would route freight through GRNI and
purchase-price variance and would create inventory value without a stock-ledger
effect.

## Decision

New actual costs are `SupplierInvoiceCharge` rows. Categories are a closed set:
freight, delivery, handling, insurance, customs and other. Treatments are a
closed pair:

- `DIRECT_EXPENSE` names an eligible postable expense/asset account and a cost
  centre. It joins the invoice journal as its own debit.
- `LANDED_COST` names no account, item, warehouse or lot. Its targets come only
  from the same invoice's `READY` purchase-match allocations. The inventory
  account and stock key are stored receipt evidence.

The old freight header remains as immutable compatibility evidence on legacy
drafts and documents. New forms do not offer it. `charges_total` is the exact
sum of structured charge rows and the payable is:

`invoice line nets + direct charges + landed costs`.

## Allocation

The default basis is receipt allocated value. Base quantity is allowed only
when all target base units have the same physical dimension. Manual allocation
is entered after the invoice is approved and its match is ready; its positive
shares must equal the charge exactly.

All proportional allocation uses `apps.core.allocation.allocate`: `Decimal`,
explicit stable sequences, largest remainder and exact residual assignment.
The stored `SupplierInvoiceChargeAllocation` retains match/allocation UUIDs,
receipt line UUID, item, warehouse, lot, quantities, receipt value, allocated
amount, control account and stock movement.

## Inventory election and conservative policy

This ADR elects the narrow value-only inventory path that earlier procurement
work deliberately deferred. Procurement calls Inventory's public stock kernel
with `MANUAL_ADJUSTMENT + VALUE_ONLY`; Inventory does not import Procurement.
Each movement has quantity zero and value equal to its charge allocation. The
invoice journal debits exactly the movements' stored inventory-control accounts
and never GRNI, purchase-price variance, return variance or waste.

Release 1 capitalises only while every target receipt position has had no
downstream outbound movement since that receipt posted. An issue, transfer
dispatch, supplier return, waste, production consumption, negative adjustment
or receipt reversal refuses the whole landed charge with a message directing
the operator to direct-expense treatment. There is no partial split.

## Atomicity and reversal

Posting locks match, invoice, mappings, lines, charges, match allocations,
receipt lines and canonical stock keys. In one transaction it rechecks the
policy, posts one value-only stock entry, posts the complete supplier-invoice
journal, links them, freezes allocation evidence and changes invoice status.
Any failure rolls back all of them.

Reversal mirrors the original journal and calls the stock kernel's exact
reversal over the stored movements. It never resolves current mappings or
reallocates the charge. A downstream outbound after landed-cost posting is a
dependency and must be reversed first.

## Consequences

Inventory quantity is never changed by an invoice. Inventory value-only
effects equal the landed-cost journal debit and the stored allocation total,
fils for fils. Direct and landed costs remain separately reportable and the
correction path remains reversal plus replacement.
