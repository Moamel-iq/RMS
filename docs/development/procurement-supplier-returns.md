# Supplier returns (Task 2.13)

Goods going back out to the supplier they came from — the first procurement
event that sends stock *out*. This is the operator- and developer-facing
summary; the authority is Task 2.0 §10 as amended, ADR-022, and
`docs/invariants/procurement-invariants.md` rows 38–40e.

## The document

A `SupplierReturn` cites **one posted delivery** (`GoodsReceipt`), and every
line cites a line of that delivery. The delivery is the argument, not the
supplier: a return is always *of something that arrived*, which is what gives
it a book value, a lot, and a quantity bound. One return, one delivery,
because the credit note that follows will be about one delivery too.

Lifecycle: `DRAFT → POSTED → REVERSED`. A draft is edited on its detail
screen; posting needs an evidence reference and at least one line; a posted
return is frozen by a whole-row database trigger and corrected only by
reversal plus a new document. Only a draft can be deleted.

## The quantity bound

Per delivery line: **accepted quantity, less what standing returns have
already taken**. A draft consumes the bound the moment its line is added
(under a lock on the receipt line, so two drafts racing for one remainder
serialize); a reversed return releases it. A wholly rejected line never
entered stock and cannot be returned from it — rejection at the gate and
return after acceptance are different mechanisms, and the demo shows them
side by side on one delivery (`DEMO-GRN-REJECT` / `DEMO-SRET-CHICKEN`).

## The accounting

```
Dr  8-01-04-001  SUPPLIER_RETURN_CLEARING   the book value that left
    Cr           INVENTORY_CONTROL          the same figure
```

and nothing else. Stock leaves at the **standing moving average** (ADR-022
§1; a full depletion surrenders the whole remaining book value), so what
leaves the books and what the supplier will credit are different numbers on
purpose. **No variance, no payable and no GRNI move at the return** — at the
gate nobody knows what the supplier will credit, and a figure nobody agreed
must not reach the profit and loss. The clearing balance *is* the claim
outstanding; Task 2.14's credit note clears it and recognises the difference
in `PURCHASE_RETURN_VARIANCE` (`7-09-04-001`, seeded, deliberately unmapped
until then). `expected_credit_value` on a line is claim metadata for the
screen and the eventual comparison; it posts nothing.

The movement type is `RETURN_OUT` — outbound, kernel-owned, and **not**
inventory's `RETURN_IN` (PRC-047). An expired lot may be returned: refusing
would force wasting spoiled goods instead of claiming against the supplier.

## Permissions and separation of duties

All four warehouse-scoped (PRC-060): `view_supplierreturn`,
`create_supplier_return`, `post_supplier_return`, `reverse_supplier_return`.
The storekeeper records and posts — sending goods back is warehouse work —
and cannot reverse; undoing a posted movement reverses a journal as well as
stock and belongs to the manager or accounting manager. Cost columns are
omitted, never blanked, without `view_supplier_cost` (PRC-061).

## Surfaces

- Screens: `/procurement/returns/` (list, HTMX fragment on filter), `new/`,
  detail with the availability table, POST-only `post/` and `reverse/`
  command routes. The navigation entry "مرتجعات الموردين" points here — the
  entry inventory explicitly gave up.
- API: `/api/v1/procurement/supplier-returns/` — list, read, create, draft
  delete, line add/remove, `post/`, `reverse/`. Money and quantities are
  exact strings both ways.
- Admin: read-only, like every posted document (PRC-062).

## Guards worth knowing about

- A delivery cited by a standing (draft or posted) return cannot be
  reversed. The Task 2.9 guard walks the receipt's **header** relations as
  well as each line's — a return exists at the header before its first line
  does, in a separate transaction, and the header walk is what closes that
  race (found by `test_a_new_return_racing_the_receipt_reversal`).
- `verify_supplier_returns` (in `verify_procurement`) proves: posted value =
  line total = clearing debit = inventory credit per return; the bound holds;
  the variance account is empty; every movement is `RETURN_OUT`.

## Demo

`seed_procurement_demo` seeds three returns, one per state:
`DEMO-SRET-CHICKEN` (posted — 20 kg of the warm-chicken delivery),
`DEMO-SRET-DRAFT` (rice against the matched delivery — a live match does not
block a return), `DEMO-SRET-REVERSED` (meat, posted then reversed by the
manager). Idempotent by evidence reference; everything goes through the real
services.
