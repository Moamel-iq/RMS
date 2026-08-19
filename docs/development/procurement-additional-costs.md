# Procurement additional costs

## Workspace

The Arabic RTL workspace is available at `/procurement/additional-costs/`.
Filters expose direct costs, landed costs, waiting for allocation, posted and
reversed records. A draft invoice links to Add Charge. Each charge has its own
detail and landed-cost allocation-preview route. Forms use HTMX and retain
normal POST/redirect fallbacks.

Cost visibility follows `procurement.view_supplier_cost`. Invoice authority
remains organization-scoped. Draft charge mutation uses the separate
`procurement.manage_supplier_invoice_charges` capability (granted to the
accountant role); manual shares use match authority; invoice posting and
reversal keep their existing commands.

## Posting shapes

Direct cost:

```text
Dr eligible direct account + cost centre       charge
    Cr supplier payable                        charge
```

Landed cost, grouped by the receipt's stored control account:

```text
Dr inventory control                           landed allocation total
    Cr supplier payable                        landed allocation total
```

The inventory kernel records the same amount as quantity `0.000` and value
`+allocated_amount`. No landed-cost amount passes through GRNI, purchase-price
variance, supplier-return variance or waste.

## Operational states

- **Draft:** charge structure can be added, edited or deleted.
- **Approved / waiting:** automatic bases preview once a READY match exists;
  manual shares can be entered and must balance exactly.
- **Posted:** charge and allocation evidence are immutable. Direct charges cite
  the journal; landed allocations cite receipt, match, stock key, movement and
  control account.
- **Reversed:** original and reversal journal/stock entries remain readable.

## Failure messages

- `landed_cost_waiting_for_match`: complete and freeze the invoice match.
- `mixed_quantity_dimensions`: use receipt-value allocation or split the
  commercial charge before approval.
- `manual_shares_do_not_balance`: make the positive shares equal the charge.
- `landed_cost_has_downstream_outbound`: change to direct expense, or reverse
  the downstream stock event first.
- `landed_cost_has_downstream_dependency`: reverse the later outbound before
  reversing the invoice.

## Verification

Run focused tests with:

```powershell
pytest apps/procurement/tests/test_variance_posting.py::TestStructuredAdditionalCosts -q
```

Then run the procurement regression, migration check, `verify_inventory` and
`verify_procurement`. The critical equalities are:

1. sum(charge allocations) = landed charge;
2. sum(value-only inventory effects) = landed charge;
3. inventory-control journal debits = landed charge;
4. payable = invoice line nets + direct charges + landed charges.
