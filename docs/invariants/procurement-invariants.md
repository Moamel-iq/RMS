# Procurement — enforced invariants

The checklist Phase 2 must satisfy. Each line is a test, not a guideline.
Nothing here is optional and none of it may be relaxed to make a suite pass.

These **extend** `docs/invariants/inventory-invariants.md` and
`docs/specs/accounting-kernel-invariants.md` rather than replace them. A
procurement posting that breaks an inventory invariant is broken twice, and a
receipt is an inventory posting before it is anything else.

**Status: proposed by Task 2.0, 2026-08-11.** The "Delivered by" column names
the task that makes each one true. Invariants 1–3 landed with Task 2.1,
5–8 with Task 2.2, 9–12 with Task 2.3, 13 with Task 2.4, 14–15 with
Task 2.5 16 with Task 2.6, 17–19 with Task 2.7
(18–19 activated by Task 2.8), 20–24 with Task 2.8, 25–31 with Task 2.9,
32–34 with Task 2.10, 35–36 with Task 2.11, and 37 with Task 2.12.
The rest are still statements of intent, and the
traceability matrix rather than this table is where the evidence lives.

## The forty

| # | Invariant | Enforced where | Delivered by |
|---|---|---|---|
| 1 | A supplier code is canonical uppercase, unique per organization; an archived code stays reserved | `strip().upper()` in the service + `UniqueConstraint` | 2.1 |
| 2 | A supplier is archived, never deleted | `on_delete=PROTECT` on every reference | 2.1 |
| 3 | `Supplier` carries no balance field; every balance is derived from posted documents | Model shape + a test asserting the field's absence | 2.1 |
| 4 | Payment terms are snapshotted onto each document, never read live from the supplier | Non-null `payment_terms_days` on order and invoice | 2.6 / 2.10 |
| 5 | A catalogue price values nothing; no posting service reads `SupplierItem` | Import-boundary test over the AST | 2.2 |
| 6 | At most one preferred supplier per item, and one preferred catalogue row per (supplier, item), effective at a date | Partial unique index | 2.2 |
| 7 | Effective catalogue periods for one (supplier, item) cannot overlap | `EXCLUDE USING gist` | 2.2 |
| 8 | A catalogue package must be one the item has a conversion for | Service guard | 2.2 |
| 9 | A purchase request has no stock effect and no accounting effect in any status | No movement, no journal; asserted per status | 2.3 |
| 10 | A request approver is never its submitter | `CheckConstraint`, not only a service check | 2.3 |
| 11 | Only a `DRAFT` request is editable; a submitted one is frozen | Service guard + trigger allowlist | 2.3 |
| 12 | Every request line stores conversion, version, factor and base quantity at submission | Non-null columns | 2.3 |
| 13 | A quotation has no stock effect and no accounting effect | As invariant 9 | 2.4 |
| 14 | Comparison normalises to base quantity and base unit price before comparing | Comparison service + test over two package sizes | 2.5 |
| 15 | No quotation is awarded automatically; an award names an actor and a reason | Service requires both; no auto-select path exists | 2.5 |
| 16 | A purchase order creates no stock and no payable, including when `ISSUED` | Asserted per status | 2.6 |
| 17 | Issued commercial terms are immutable on the row; a change creates a version | Allowlist trigger + `PurchaseOrderVersion` | 2.7 |
| 18 | A revision cannot reduce an ordered quantity below what has been received | Service guard under a row lock | 2.7 |
| 19 | The supplier cannot change once a receipt exists | Service guard | 2.7 |
| 20 | `delivered = accepted + rejected` on every receipt line | `CheckConstraint` | 2.8 |
| 21 | Only the accepted quantity increases stock; rejected quantity posts nothing | Posting service + test asserting no movement for rejects | 2.8 |
| 22 | A `VARIABLE` package line requires its measured quantity | Reuses the inventory guard; no procurement bypass | 2.8 |
| 23 | Lot and expiry follow the item's own rules, unchanged | Reuses `_validate_lot` | 2.8 |
| 24 | Cumulative accepted quantity may not exceed the ordered base quantity | Service guard under a lock | 2.8 |
| 25 | A receipt posts through the inventory kernel; procurement has no second posting path | `post_stock_entry` + `post_entry` are the only calls; a test asserts no stock-only service exists | 2.8, 2.9 |
| 26 | A posted receipt is immutable; correction is reversal plus replacement | Whole-row allowlist trigger over the receipt and a freeze over its lines | 2.9 |
| 27 | A receipt reversal respects stock availability | Reuses the kernel check | 2.9 |
| 28 | Receipt journal value equals receipt stock value, per line, to 3 dp | `verify_goods_receipt`, plus a trigger asserting the stored value is its own quantity at its own cost | 2.9 |
| 29 | No procurement service names an account, id or code; all come from effective-dated roles | Role resolution + missing-mapping rollback test + a mapping-mutation race | 2.9 |
| 30 | Document, movement, journal, location and status commit or roll back together | `transaction.atomic()` + a forced journal failure | 2.9 |
| 31 | Source identity is complete or absent, never partial (ADR-017) | Reuses the accounting guard; `PROCUREMENT_GOODS_RECEIPT` + `public_id` + event | 2.9 |
| 32 | A supplier invoice number is unique per supplier over non-reversed invoices | Partial unique index over a stored normalized key; whitespace and case fold, leading zeros do not | 2.10 |
| 33 | A supplier invoice never mutates stock | No inventory call exists in the path; movement and location counts asserted either side of a posting | 2.10 |
| 34 | An invoice total is the sum of its posted lines, never independently rounded | `apps/core/allocation.py` for freight and discount; a trigger states each line's net arithmetic | 2.10 |
| 34a | An invoice line has exactly one economic type — goods or a direct account, never both and never neither | `CheckConstraint` over the whole line shape | 2.10 |
| 34b | An invoice posts only where every line has a complete approved accounting route; a goods line waits for matching | Service guard `invoice_awaiting_matching`; the whole document waits, never half of it | 2.10 |
| 35 | Matched quantity may not exceed the receipt line's accepted quantity, nor the invoice line's quantity, nor the order line's | Availability derived from active allocation rows, checked under a documented lock order; verifier `verify_matching` | 2.11 |
| 36 | Matching status is derived, never stored as a mutable flag | `matching.invoice_line_match_state`; no status column exists on any line | 2.11 |
| 36a | A matched value is a share of what the delivery **posted**, never of today's moving average | `_share` over the receipt line's own `posted_value`; the final allocation takes the exact remainder | 2.11 |
| 36b | Price variance is `invoice_allocated_value - receipt_allocated_value`, asserted by the database | `CheckConstraint` over the three columns; no path can store a difference its components do not support | 2.11 |
| 36c | A `READY` match is immutable except for its cancellation; a `CANCELLED` match consumes nothing | Whole-row allowlist triggers on both tables; availability counts only draft and ready rows | 2.11 |
| 36d | Matching moves no stock and posts no journal | Asserted either side of a readiness: movement, location-movement and journal counts unchanged | 2.11 |
| 37 | Price variance never restates a posted movement or a closed period | Recognised where discovered; stock quantity, value and average asserted unchanged either side of a posting | 2.12 |
| 37a | An invoice posts for the whole document or not at all; every goods line needs a `READY` match covering it in full | `invoice_awaiting_matching`, `match_not_ready`, `invoice_partly_matched` | 2.12 |
| 37b | The payable is recognised exactly once per invoice, and the entry balances by construction | One live posting generation per invoice, enforced by a partial unique index; `Dr A + R + D = Cr A + V` | 2.12 |
| 37c | A price difference is parked in a clearing account, never classified as cost of sales | `PURCHASE_PRICE_VARIANCE` maps to a CLEARING account needing no cost centre (ADR-022, amended) | 2.12 |
| 37d | GRNI is debited at the account each delivery actually credited, never re-resolved | `GoodsReceiptLine.contra_account`, grouped; survives a mapping supersession | 2.12 |
| 37e | A posted generation is immutable except for its own reversal, and is never deleted | Whole-row allowlist trigger | 2.12 |
| 37f | A match backing a live posting cannot be cancelled; a reversed generation blocks nothing | Service guard plus a database trigger; `live_dependency` is `status=LIVE` | 2.12 |
| 37g | Procurement's own GRNI movement equals its accepted delivery value no posted invoice covers | `verify_grni_clearing` (invariant 47, scoped to this module's entries) | 2.12 |
| 38 | A supplier return is not an inventory `RETURN_IN` and uses a distinct movement type | `MovementType.RETURN_OUT`, outbound; `TestTheSupplierReturnMovementType` asserts the directions differ | 2.13 |
| 39 | A return leaves stock at the standing moving average; a full depletion surrenders its whole remaining value | Reuses the kernel; ADR-022 §1's worked example is `test_it_leaves_at_the_standing_average_not_the_receipt_price` | 2.13 |
| 40 | Negative stock is refused on a return, with no procurement bypass | Reuses `_require_available`; race asserted in `test_a_return_racing_an_issue_of_the_same_stock` | 2.13 |
| 40a | Returned quantity per delivery line never exceeds what that line accepted; standing returns consume the bound, reversed ones release it | `return_availability` under a receipt-line lock, re-checked at posting; `TestTheQuantityBound` + the two-drafts race | 2.13 |
| 40b | A return posts `Dr SUPPLIER_RETURN_CLEARING / Cr INVENTORY_CONTROL` and nothing else — no payable, no GRNI, no variance at the return | ADR-022 §2 as amended; `TestTheCreditNoteBoundary` is the negative half Task 2.14 must overturn deliberately | 2.13 |
| 40c | A posted return is immutable except for its reversal; only a draft can be deleted; lines freeze with their return | Whole-row allowlist triggers (migration 0023) | 2.13 |
| 40d | A delivery cited by a standing return cannot be reversed, at the header from the moment the draft exists | `live_dependency` on `SupplierReturn` and its line; the receipt guard walks header and line relations both | 2.13 |
| 40e | The clearing balance equals the posted value of standing returns, fils for fils, and the variance account is empty | `verify_supplier_returns` | 2.13 |
| 41 | A credit note reduces the payable or stands as unallocated credit; it moves no stock | Posting service + test | 2.14 |
| 42 | A credit note's supplier document number is unique per supplier over non-reversed notes | Partial unique index | 2.14 |
| 43 | Payment allocations may not exceed the invoice total nor the payment amount | Service under a lock + reconciliation invariant | 2.15 |
| 44 | An unallocated payment remainder is a supplier advance, never a negative payable | Posting service; asserted on the aging report | 2.15 |
| 45 | Oldest-invoice allocation is a visible default, never applied silently | UI default + a test that the API requires explicit allocations | 2.15 |
| 46 | Open supplier balances sum to the supplier payable account balance | `verify_procurement_accounting` | 2.16 |
| 47 | Accepted-and-unmatched receipt value sums to the GRNI account balance | Same verifier | 2.16 |
| 48 | Every posted procurement journal traces to exactly one source document | Same verifier | 2.16 |
| 49 | Verification reports and refuses to repair | No repair mode exists; asserted | 2.16 |
| 50 | Every report names its cutoff semantics — effective-date or posted-as-of | Report contract, inherited from Phase 1 | 2.16 |

## Rules that carry over unchanged

Not restated as procurement invariants, because they already hold and
procurement must not weaken them:

- Posted journal entries are immutable on **every** column
  (migration `accounting/0005`).
- Idempotency keys are unique per organization and matched against a request
  fingerprint; a key reused with a changed payload is a conflict (ADR-017).
- Authorization is a permission **plus** a scope; out of scope is 404, in scope
  without authority is 403 (ADR-016).
- A submitted id never widens access — resolve with the caller, never
  fetch-then-check.
- Decimal only. No float touches a quantity, price, discount, freight, cost,
  allocation or money value.
- API money and quantities are exact strings in both directions.
- Posting requires an OPEN period; SOFT_CLOSED and CLOSED refuse.
- Audit `previous_state` is re-read from the database, never snapshotted from a
  form-bound instance.

## Deliberate non-invariants

Things a reader might expect to find here, and the reason each is absent:

- **"A purchase order must exist before a receipt."** It must not. Buying meat
  from the market without raising an order first is normal for this business,
  and forcing an order would produce fictional orders written after the fact.
- **"An invoice must match a receipt."** Invoicing without a receipt is an
  exception to *report*, not an error to refuse — refusing it would leave a
  real liability unrecorded.
- **"A supplier balance may be cached."** Deliberately never. See invariant 3.
- **"Freight is capitalised into inventory value."** Not in Release 1. Task 2.0
  §16.2 records why, and the fields exist for when it is approved.
- **"A tax amount is recorded."** No tax behaviour is invented where the
  requirements define none. Task 2.0 §16.1.
- **"Prices are hidden from anyone who cannot see them."** Cost columns are
  **omitted, not blanked** — a blanked column tells the reader a number exists
  and that they are not trusted with it, which is a different statement from
  the one intended.
